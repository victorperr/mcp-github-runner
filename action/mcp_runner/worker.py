"""The tool process: the only process that imports and runs the repo's code.

The supervisor (``__main__``) holds the runner's credentials and talks to the
gateway. It starts this process with an environment that has no credentials,
passes the request in a private temp folder, and reads the response back from
it. Tool code therefore never shares memory or environment with the process
that can mint OIDC tokens.

    python -P -m mcp_runner worker {manifest|call} <io-dir>
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .loader import LoadError, load_server
from .manifest import build_manifest
from .policy import PolicyError, load_policy
from .results import RUNNER_APPROVAL_MISSING, RUNNER_TOOL_NOT_ALLOWED, refusal
from .security import scrub_environment


async def _manifest(req: dict[str, Any]) -> dict[str, Any]:
    policy = load_policy(req["policy"])
    server = load_server(req["server"])
    return await build_manifest(server, req["approval_environment"], policy)


async def _call(req: dict[str, Any]) -> dict[str, Any]:
    """Runner-side guards, in order. They don't trust anything the gateway decided."""
    from .call import run_tool

    policy = load_policy(req["policy"])
    server = load_server(req["server"])
    manifest = await build_manifest(server, req["approval_environment"], policy)

    tool = req.get("tool")
    published = {t["name"] for t in manifest["tools"]}
    # 1. The tool must be one this repo publishes (exists and passes tools.allow).
    if tool not in published:
        return refusal(RUNNER_TOOL_NOT_ALLOWED, f"tool '{tool}' is not published by this server")
    # 2. Gated tools only run in a job that passed the approval environment.
    #    The environment comes from the supervisor's own OIDC token.
    if tool in manifest["approval_required"]:
        env = (req.get("environment") or "").lower()
        if env != req["approval_environment"].lower():
            return refusal(
                RUNNER_APPROVAL_MISSING,
                f"tool '{tool}' requires approval, but this job did not run in the "
                f"'{req['approval_environment']}' environment",
            )
    return await run_tool(server, tool, req.get("arguments"))


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("manifest", "call"):
        print("usage: python -m mcp_runner worker {manifest|call} <io-dir>")
        return 2
    mode, io_dir = argv
    scrub_environment()  # the supervisor already did; this is a second guard
    io = Path(io_dir)
    req = json.loads((io / "request.json").read_text(encoding="utf-8"))
    try:
        if mode == "manifest":
            out = {"ok": True, "manifest": asyncio.run(_manifest(req))}
        else:
            out = {"ok": True, "result": asyncio.run(_call(req))}
    except (LoadError, PolicyError) as exc:
        out = {"ok": False, "error": str(exc)}
    (io / "response.json").write_text(json.dumps(out), encoding="utf-8")
    return 0
