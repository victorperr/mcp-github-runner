"""CLI used by action.yml.

    python -m mcp_runner manifest --server server.py:mcp --gateway URL
    python -m mcp_runner call --server server.py:mcp --gateway URL --call-id ID

This process is the supervisor: it holds the runner's credentials and talks to
the gateway, but never imports the repo's code. The server is loaded and run in
a separate tool process (``worker.py``) whose environment has no credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .gateway_client import GatewayClient, GatewayError, default_audience
from .loader import LoadError
from .manifest import DEFAULT_APPROVAL_ENVIRONMENT, hash_client_token
from .policy import DEFAULT_POLICY_FILE, PolicyError
from .results import RUNNER_RESULT_TOO_LARGE, error_result, refusal
from .security import (
    OIDCSource,
    Redactor,
    SecurityError,
    check_client_token,
    harden_process,
    read_credentials,
    scrub_environment,
    unverified_claims,
    validate_call_id,
)

# Folder that holds the mcp_runner package, put on the tool process's PYTHONPATH.
PACKAGE_ROOT = str(Path(__file__).resolve().parent.parent)


class ToolProcessError(Exception):
    pass


def _write_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def _secure_start(audience: str) -> tuple[OIDCSource, Redactor, str]:
    """Take credentials out of the environment before any tool process starts."""
    redactor = Redactor()  # snapshot secret-looking values while they're still visible
    client_token = ""
    if os.environ.pop("MCP_RUNNER_CREDENTIALS", "") == "stdin":
        # action.yml: credentials arrive on stdin and were never in our environment.
        creds = read_credentials()
        oidc = OIDCSource(audience, url=creds.oidc_request_url, request_token=creds.oidc_request_token)
        client_token = creds.client_token
    else:
        oidc = OIDCSource(audience)  # local runs and tests: credentials in the environment
    removed = scrub_environment()
    client_token = client_token or removed.get("MCP_CLIENT_TOKEN", "")
    for value in [*oidc.secrets, client_token, *removed.values()]:
        redactor.add(value)
    harden_process()
    return oidc, redactor, client_token


def _run_tool_process(mode: str, args: argparse.Namespace, extra: dict[str, Any]) -> dict[str, Any]:
    """Run worker.py in a fresh process and return its response."""
    request = {
        "server": args.server,
        "policy": args.policy,
        "approval_environment": args.approval_environment,
        **extra,
    }
    env = dict(os.environ)  # already scrubbed by _secure_start
    env["PYTHONPATH"] = os.pathsep.join(p for p in (PACKAGE_ROOT, env.get("PYTHONPATH")) if p)
    # mkdtemp gives a folder only this user can read; arguments may be sensitive.
    with tempfile.TemporaryDirectory(prefix="mcp-runner-", dir=os.environ.get("RUNNER_TEMP") or None) as tmp:
        io = Path(tmp)
        (io / "request.json").write_text(json.dumps(request), encoding="utf-8")
        proc = subprocess.run(  # noqa: S603 - fixed command, no shell
            [sys.executable, "-P", "-m", "mcp_runner", "worker", mode, tmp],
            env=env,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        response = io / "response.json"
        if not response.is_file():
            raise ToolProcessError(f"The tool process exited with code {proc.returncode} without a response")
        out = json.loads(response.read_text(encoding="utf-8"))
    if not out.get("ok"):
        raise LoadError(out.get("error") or "The tool process failed")
    return out


def cmd_manifest(args: argparse.Namespace) -> int:
    oidc, _, client_token = _secure_start(args.audience)
    manifest = _run_tool_process("manifest", args, {})["manifest"]
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


def _execute(args: argparse.Namespace, call: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    """Run the call in the tool process. Guards run there, before the tool (see worker._call)."""
    try:
        return _run_tool_process("call", args, {
            "tool": call.get("tool"),
            "arguments": call.get("arguments"),
            # From our own OIDC token, not from the gateway.
            "environment": claims.get("environment"),
        })["result"]
    except (LoadError, PolicyError, ToolProcessError) as exc:
        return error_result(str(exc))


def cmd_call(args: argparse.Namespace) -> int:
    call_id = validate_call_id(args.call_id)
    oidc, redactor, _ = _secure_start(args.audience)
    if os.name == "nt" and os.environ.get("GITHUB_ACTIONS") == "true":
        print("::warning::On Windows runners, tool code runs as the same user as the runner and can read "
              "other processes' memory. Prefer Linux runners for tools that handle untrusted input.")
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
        result = _execute(args, call, claims)

        result, replaced = redactor.redact(result)
        if replaced:
            print(f"::warning::Redacted {replaced} secret value(s) from the result")
            result.setdefault("_meta", {})["mcp-github-runner/redactions"] = replaced

        limit = int(call.get("max_result_bytes") or 0) or 4 * 1024 * 1024
        size = len(json.dumps(result).encode())
        if size > limit:
            result = refusal(RUNNER_RESULT_TOO_LARGE, f"result is {size} bytes; the limit is {limit}")

        gw.post_result(call_id, {"result": result})
        print("Result sent" + (" (tool reported an error)" if result.get("isError") else ""))
    finally:
        gw.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["worker"]:
        from .worker import main as worker_main

        return worker_main(argv[1:])

    parser = argparse.ArgumentParser(prog="mcp_runner")
    sub = parser.add_subparsers(dest="mode", required=True)
    for name in ("manifest", "call"):
        p = sub.add_parser(name)
        p.add_argument("--server", default=os.environ.get("MCP_SERVER", "server.py"))
        p.add_argument("--gateway", default=os.environ.get("MCP_GATEWAY_URL", ""))
        p.add_argument("--audience", default=os.environ.get("MCP_GATEWAY_AUDIENCE", ""),
                       help="OIDC audience the gateway expects (default: the gateway URL)")
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
    args.audience = args.audience or default_audience(args.gateway)

    try:
        return cmd_manifest(args) if args.mode == "manifest" else cmd_call(args)
    except (LoadError, GatewayError, PolicyError, SecurityError, ToolProcessError) as exc:
        print(f"::error::{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
