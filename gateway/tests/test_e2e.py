"""Full loop without GitHub: real gateway over HTTP, runner CLI as a subprocess,
FastMCP client as the external MCP client."""

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from fastmcp import Client

from app.main import create_app

from .conftest import TEMPLATE_DIR, RecordingDispatcher, make_settings

CLIENT_TOKEN = "e2e-client-token-0123456789abcdefghijkl"
ACTION_DIR = Path(__file__).resolve().parents[2] / "action"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveGateway:
    def __init__(self, tmp_path, *, local=True, **overrides):
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        # Starting a Python subprocess that imports fastmcp takes a few seconds.
        overrides.setdefault("call_timeout", 60)
        settings = make_settings(tmp_path, public_url=self.url, **overrides)
        dispatcher = None if local else RecordingDispatcher()
        if local:
            settings.dispatcher = "local"
            settings.local_server_dir = str(TEMPLATE_DIR)
            settings.local_server_spec = "server.py:mcp"
        self.app = create_app(settings, dispatcher=dispatcher)
        self.server = uvicorn.Server(uvicorn.Config(self.app, port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("gateway did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(5)

    def register(self, client_token=CLIENT_TOKEN):
        token = self.app.state.dev_signer.sign(
            audience=self.app.state.settings.oidc_audience,
            repository="me/demo", repository_id="1", event_name="push",
        )
        env = {**os.environ, "MCP_RUNNER_OIDC_TOKEN": token, "MCP_CLIENT_TOKEN": client_token,
               "PYTHONPATH": str(ACTION_DIR)}
        out = subprocess.run(
            [sys.executable, "-m", "mcp_runner", "manifest", "--gateway", self.url, "--server", "server.py:mcp"],
            cwd=TEMPLATE_DIR, env=env, capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stdout + out.stderr

    @property
    def mcp_url(self):
        return f"{self.url}/s/me/demo/mcp"


async def test_list_and_call_through_gateway(tmp_path):
    with LiveGateway(tmp_path) as gw:
        gw.register()
        async with Client(gw.mcp_url, auth=CLIENT_TOKEN) as client:
            tools = {t.name for t in await client.list_tools()}
            assert tools == {"add", "runner_info", "internal_api_get", "restart_service"}

            progress = []

            async def on_progress(p, total, message):
                progress.append(message)

            result = await client.call_tool("add", {"a": 20, "b": 22}, progress_handler=on_progress)
            assert result.data == 42

            # Bad arguments are stopped by the gateway's schema check: no run is started.
            bad = await client.call_tool("add", {"a": "x", "b": 1}, raise_on_error=False)
            assert bad.is_error and "arguments_invalid" in bad.content[0].text

            # Needs approval: the local dispatcher simulates an approved run,
            # so this also checks the environment claim reaches the gateway.
            result = await client.call_tool("restart_service", {"name": "billing"})
            assert result.data == "(example) would restart billing"

        rows = gw.app.state.store._db.execute("SELECT tool, status, approval_env FROM calls").fetchall()
        assert [tuple(r) for r in rows] == [
            ("add", "done", None),
            ("restart_service", "done", "mcp-approval"),
        ]


async def test_wrong_client_token_is_rejected(tmp_path):
    with LiveGateway(tmp_path) as gw:
        gw.register()
        r = httpx.post(gw.mcp_url, json={}, headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401
        r = httpx.post(f"{gw.url}/s/someone/else/mcp", json={}, headers={"Authorization": f"Bearer {CLIENT_TOKEN}"})
        assert r.status_code == 401


async def test_timeout_returns_tool_error_with_heartbeats(tmp_path):
    # Dispatcher that never runs anything: the call must time out cleanly.
    with LiveGateway(tmp_path, local=False, call_timeout=1.0, heartbeat_interval=0.3) as gw:
        gw.register()
        progress = []

        async def on_progress(p, total, message):
            progress.append(message)

        async with Client(gw.mcp_url, auth=CLIENT_TOKEN) as client:
            result = await client.call_tool("add", {"a": 1, "b": 2}, raise_on_error=False,
                                            progress_handler=on_progress)
        assert result.is_error
        assert "No result after" in result.content[0].text
        assert progress and "Waiting for GitHub to start the run" in progress[0]
