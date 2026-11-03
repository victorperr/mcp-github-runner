"""CLI used by action.yml.

    python -m mcp_runner manifest --server server.py:mcp --gateway URL
    python -m mcp_runner call --server server.py:mcp --gateway URL --call-id ID
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from .call import error_result, run_tool
from .gateway_client import DEFAULT_AUDIENCE, GatewayClient, GatewayError
from .loader import LoadError, load_server
from .manifest import DEFAULT_APPROVAL_ENVIRONMENT, build_manifest, hash_client_token
from .policy import DEFAULT_POLICY_FILE, PolicyError, load_policy
from .security import (
    OIDCSource,
    Redactor,
    SecurityError,
    check_client_token,
    scrub_environment,
    unverified_claims,
    validate_call_id,
)

# Denial codes used by the runner. The gateway uses the same numbering (see gateway/app/policy.py).
RUNNER_TOOL_NOT_ALLOWED = -32011
RUNNER_APPROVAL_MISSING = -32012
RUNNER_RESULT_TOO_LARGE = -32013


def _write_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def _refusal(code: int, message: str) -> dict[str, Any]:
    result = error_result(f"Refused by the runner: {message}")
    result["_meta"] = {"mcp-github-runner/denial": {"code": code, "by": "runner", "reason": message}}
    return result


def _secure_start(audience: str) -> tuple[OIDCSource, Redactor, str]:
    """Take credentials out of the environment before any user code is imported."""
    redactor = Redactor()  # snapshot secret-looking values while they're still visible
    oidc = OIDCSource(audience)
    removed = scrub_environment()
    for value in [*oidc.secrets, *removed.values()]:
        redactor.add(value)
    return oidc, redactor, removed.get("MCP_CLIENT_TOKEN", "")


def cmd_manifest(args: argparse.Namespace) -> int:
    oidc, _, client_token = _secure_start(args.audience)
    policy = load_policy(args.policy)
    server = load_server(args.server)
    manifest = asyncio.run(build_manifest(server, args.approval_environment, policy))
    print(json.dumps(manifest, indent=2))

    if args.dry_run:
        return 0

    if not client_token:
        print("::warning::MCP_CLIENT_TOKEN is not set; the gateway keeps the previous client token.")
    else:
        check_client_token(client_token)
        manifest["client_token_sha256"] = hash_client_token(client_token)

    gw = GatewayClient(args.gateway, oidc.token)
    try:
        resp = gw.put_manifest(manifest)
    finally:
        gw.close()

    for warning in resp.get("warnings") or []:
        print(f"::warning::{warning}")
    url = resp.get("mcp_url", "")
    names = ", ".join(t["name"] for t in manifest["tools"]) or "(none)"
    gated = ", ".join(manifest["approval_required"]) or "(none)"
    print(f"Registered {len(manifest['tools'])} tool(s): {names}")
    print(f"Need approval in environment '{manifest['approval_environment']}': {gated}")
    _write_summary(
        f"### MCP server registered\n\n- URL: `{url}`\n- Tools: {names}\n"
        f"- Need approval (`{manifest['approval_environment']}` environment): {gated}\n"
    )
    return 0


async def _execute(args: argparse.Namespace, call: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    """Runner-side guards, in order. They don't trust anything the gateway decided."""
    try:
        policy = load_policy(args.policy)
        server = load_server(args.server)
        manifest = await build_manifest(server, args.approval_environment, policy)
    except (LoadError, PolicyError) as exc:
        return error_result(str(exc))

    tool = call.get("tool")
    published = {t["name"] for t in manifest["tools"]}
    # 1. The tool must be one this repo publishes (exists and passes tools.allow).
    if tool not in published:
        return _refusal(RUNNER_TOOL_NOT_ALLOWED, f"tool '{tool}' is not published by this server")
    # 2. Gated tools only run in a job that passed the approval environment.
    if tool in manifest["approval_required"]:
        env = (claims.get("environment") or "").lower()
        if env != args.approval_environment.lower():
            return _refusal(
                RUNNER_APPROVAL_MISSING,
                f"tool '{tool}' requires approval, but this job did not run in the "
                f"'{args.approval_environment}' environment",
            )
    return await run_tool(server, tool, call.get("arguments"))


def cmd_call(args: argparse.Namespace) -> int:
    call_id = validate_call_id(args.call_id)
    oidc, redactor, _ = _secure_start(args.audience)
    first_token = oidc.token()
    claims = unverified_claims(first_token)
    tokens = iter([first_token])

    def token() -> str:
        # Reuse the first token once, then mint fresh ones (long calls can outlive a token).
        return next(tokens, None) or oidc.token()

    gw = GatewayClient(args.gateway, token)
    try:
        call = gw.get_call(call_id)
        # Tool name is logged; arguments are not, since they may be sensitive.
        print(f"Running tool '{call.get('tool')}' for call {call_id}")
        result = asyncio.run(_execute(args, call, claims))

        result, replaced = redactor.redact(result)
        if replaced:
            print(f"::warning::Redacted {replaced} secret value(s) from the result")
            result.setdefault("_meta", {})["mcp-github-runner/redactions"] = replaced

        limit = int(call.get("max_result_bytes") or 0) or 4 * 1024 * 1024
        size = len(json.dumps(result).encode())
        if size > limit:
            result = _refusal(RUNNER_RESULT_TOO_LARGE, f"result is {size} bytes; the limit is {limit}")

        gw.post_result(call_id, {"result": result})
        print("Result sent" + (" (tool reported an error)" if result.get("isError") else ""))
    finally:
        gw.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp_runner")
    sub = parser.add_subparsers(dest="mode", required=True)
    for name in ("manifest", "call"):
        p = sub.add_parser(name)
        p.add_argument("--server", default=os.environ.get("MCP_SERVER", "server.py"))
        p.add_argument("--gateway", default=os.environ.get("MCP_GATEWAY_URL", ""))
        p.add_argument("--audience", default=os.environ.get("MCP_GATEWAY_AUDIENCE", DEFAULT_AUDIENCE))
        p.add_argument("--policy", default=os.environ.get("MCP_POLICY_FILE") or DEFAULT_POLICY_FILE)
        p.add_argument(
            "--approval-environment",
            default=os.environ.get("MCP_APPROVAL_ENVIRONMENT") or DEFAULT_APPROVAL_ENVIRONMENT,
        )
        if name == "manifest":
            p.add_argument("--dry-run", action="store_true", help="print the manifest only")
        else:
            p.add_argument("--call-id", required=True)
    args = parser.parse_args(argv)

    if not args.gateway and not getattr(args, "dry_run", False):
        parser.error("--gateway (or MCP_GATEWAY_URL) is required")

    try:
        return cmd_manifest(args) if args.mode == "manifest" else cmd_call(args)
    except (LoadError, GatewayError, PolicyError, SecurityError) as exc:
        print(f"::error::{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
