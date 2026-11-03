"""Dev helper: register a local FastMCP server with a dev-mode gateway.

    python -m app.dev register --dir ../template --repo me/demo --client-token devtoken

It signs a fake push-event OIDC token with the gateway's dev key and runs
``mcp_runner manifest`` exactly as the Action would on GitHub.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from .config import Settings
from .oidc import DevSigner

ACTION_DIR = Path(__file__).resolve().parents[2] / "action"


def register(args: argparse.Namespace) -> int:
    settings = Settings()
    signer = DevSigner(settings.dev_key_path)
    token = signer.sign(
        audience=settings.oidc_audience,
        repository=args.repo,
        repository_id=args.repo_id,
        event_name="push",
    )
    env = {
        **os.environ,
        "MCP_RUNNER_OIDC_TOKEN": token,
        "MCP_CLIENT_TOKEN": args.client_token,
        "PYTHONPATH": str(ACTION_DIR),
    }
    return subprocess.call(
        [sys.executable, "-m", "mcp_runner", "manifest", "--gateway", args.gateway, "--server", args.server],
        cwd=args.dir,
        env=env,
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.dev")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("register")
    p.add_argument("--dir", required=True, help="folder holding the FastMCP server")
    p.add_argument("--server", default="server.py")
    p.add_argument("--repo", default="me/demo")
    p.add_argument("--repo-id", default="1")
    p.add_argument("--client-token", default="devtoken")
    p.add_argument("--gateway", default=Settings().public_url)
    args = parser.parse_args()
    return register(args)


if __name__ == "__main__":
    sys.exit(main())
