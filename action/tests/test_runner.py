import base64
import io
import json
import os
import sys
from pathlib import Path

import pytest
import respx

from mcp_runner.__main__ import main
from mcp_runner.call import run_tool
from mcp_runner.loader import LoadError, load_server
from mcp_runner.manifest import build_manifest, hash_client_token

TEMPLATE = Path(__file__).resolve().parents[2] / "template"
GW = "https://gw.test"
CALL_ID = "c1c1c1c1c1c1c1c1c1c1"
CLIENT_TOKEN = "x" * 40


def fake_jwt(**claims) -> str:
    """Unsigned token shaped like the runner's OIDC token (only the payload is read)."""
    def enc(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{enc({'alg': 'RS256'})}.{enc({'repository': 'me/demo', **claims})}.sig"


@pytest.fixture
def server():
    return load_server("server.py:mcp", TEMPLATE)


def test_loader_finds_default_attr():
    assert load_server("server.py", TEMPLATE).name == "example-server"


def test_loader_errors(tmp_path):
    with pytest.raises(LoadError, match="not found"):
        load_server("missing.py", tmp_path)
    (tmp_path / "s.py").write_text("x = 1\n")
    with pytest.raises(LoadError, match="No FastMCP instance"):
        load_server("s.py", tmp_path)
    with pytest.raises(LoadError, match="not a FastMCP"):
        load_server("s.py:x", tmp_path)


async def test_manifest(server):
    manifest = await build_manifest(server)
    names = {t["name"] for t in manifest["tools"]}
    assert names == {"add", "runner_info", "internal_api_get", "restart_service"}
    add = next(t for t in manifest["tools"] if t["name"] == "add")
    assert add["inputSchema"]["properties"].keys() == {"a", "b"}
    assert manifest["server_name"] == "example-server"
    assert manifest["approval_required"] == ["restart_service"]
    assert manifest["approval_environment"] == "mcp-approval"
    assert manifest["policy"]["limits"]["max_calls_per_hour"] == 120


async def test_manifest_custom_approval_environment(server):
    manifest = await build_manifest(server, "prod-approvers")
    assert manifest["approval_environment"] == "prod-approvers"


async def test_run_tool_success(server):
    result = await run_tool(server, "add", {"a": 2, "b": 3})
    assert result.get("isError") is not True
    assert result["content"][0]["text"] == "5"
    assert result["structuredContent"] == {"result": 5}


async def test_run_tool_error_is_reported(server):
    result = await run_tool(server, "add", {"a": "nope"})
    assert result["isError"] is True


async def test_run_tool_unknown_tool(server):
    result = await run_tool(server, "nope", {})
    assert result["isError"] is True


async def test_internal_api_tool_needs_config(server, monkeypatch):
    monkeypatch.delenv("INTERNAL_API_URL", raising=False)
    result = await run_tool(server, "internal_api_get", {"path": "/status"})
    assert result["isError"] is True
    assert "INTERNAL_API_URL" in result["content"][0]["text"]


@respx.mock
async def test_internal_api_tool_calls_private_service(server, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_URL", "http://internal.corp:8080")
    monkeypatch.setenv("INTERNAL_API_TOKEN", "t0k")
    route = respx.get("http://internal.corp:8080/status").respond(text="ok")
    result = await run_tool(server, "internal_api_get", {"path": "/status"})
    assert result["structuredContent"] == {"status": 200, "body": "ok"}
    assert route.calls.last.request.headers["Authorization"] == "Bearer t0k"
    bad = await run_tool(server, "internal_api_get", {"path": "http://elsewhere/"})
    assert bad["isError"] is True


# -- CLI: call mode -------------------------------------------------------------

def _call(monkeypatch, tool, arguments, claims=None, extra_env=None):
    """Run `mcp_runner call` against a mocked gateway; return the posted result."""
    monkeypatch.chdir(TEMPLATE)
    monkeypatch.setenv("MCP_RUNNER_OIDC_TOKEN", fake_jwt(**(claims or {})))
    for k, v in (extra_env or {}).items():
        monkeypatch.setenv(k, v)
    with respx.mock:
        respx.get(f"{GW}/runner/calls/{CALL_ID}").respond(
            json={"call_id": CALL_ID, "tool": tool, "arguments": arguments, "max_result_bytes": 1_000_000})
        posted = respx.post(f"{GW}/runner/calls/{CALL_ID}/result").respond(json={"ok": True})
        code = main(["call", "--gateway", GW, "--call-id", CALL_ID, "--server", "server.py:mcp"])
        assert code == 0
        return json.loads(posted.calls.last.request.content)["result"], posted.calls.last.request


def test_cli_call_round_trip(monkeypatch):
    result, req = _call(monkeypatch, "add", {"a": 1, "b": 41})
    assert result["structuredContent"] == {"result": 42}
    assert req.headers["Authorization"].startswith("Bearer ")


def test_runner_refuses_gated_tool_without_approval(monkeypatch):
    # A compromised gateway asks for a gated tool in a job that skipped approval.
    result, _ = _call(monkeypatch, "restart_service", {"name": "db"}, claims={})
    assert result["isError"] is True
    assert result["_meta"]["mcp-github-runner/denial"]["code"] == -32012


def test_runner_runs_gated_tool_after_approval(monkeypatch):
    result, _ = _call(monkeypatch, "restart_service", {"name": "db"}, claims={"environment": "mcp-approval"})
    assert result.get("isError") is not True
    assert "would restart db" in result["content"][0]["text"]


def test_runner_refuses_tool_not_published(monkeypatch, tmp_path):
    (tmp_path / "server.py").write_text((TEMPLATE / "server.py").read_text())
    (tmp_path / "mcp-runner.toml").write_text('[tools]\nallow = ["add"]\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_RUNNER_OIDC_TOKEN", fake_jwt())
    with respx.mock:
        respx.get(f"{GW}/runner/calls/{CALL_ID}").respond(json={"tool": "runner_info", "arguments": {}})
        posted = respx.post(f"{GW}/runner/calls/{CALL_ID}/result").respond(json={"ok": True})
        assert main(["call", "--gateway", GW, "--call-id", CALL_ID]) == 0
    result = json.loads(posted.calls.last.request.content)["result"]
    assert result["_meta"]["mcp-github-runner/denial"]["code"] == -32011


def test_runner_scrubs_credentials_before_user_code(monkeypatch, tmp_path):
    # The tool returns its whole environment: credentials must not be in it.
    (tmp_path / "server.py").write_text(
        "import os\nfrom fastmcp import FastMCP\nmcp = FastMCP('t')\n"
        "@mcp.tool\ndef env() -> dict:\n    return dict(os.environ)\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_RUNNER_OIDC_TOKEN", fake_jwt())
    monkeypatch.setenv("MCP_CLIENT_TOKEN", CLIENT_TOKEN)
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", "runtime-token-value")
    with respx.mock:
        respx.get(f"{GW}/runner/calls/{CALL_ID}").respond(json={"tool": "env", "arguments": {}})
        posted = respx.post(f"{GW}/runner/calls/{CALL_ID}/result").respond(json={"ok": True})
        assert main(["call", "--gateway", GW, "--call-id", CALL_ID]) == 0
    env = json.loads(posted.calls.last.request.content)["result"]["structuredContent"]
    for name in ("MCP_RUNNER_OIDC_TOKEN", "MCP_CLIENT_TOKEN", "ACTIONS_RUNTIME_TOKEN"):
        assert name not in env


def test_tool_code_runs_in_a_separate_process(monkeypatch, tmp_path):
    # The supervisor holds the OIDC credentials; tool code must not share its memory.
    (tmp_path / "server.py").write_text(
        "import os\nfrom fastmcp import FastMCP\nmcp = FastMCP('t')\n"
        "@mcp.tool\ndef pid() -> int:\n    return os.getpid()\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_RUNNER_OIDC_TOKEN", fake_jwt())
    with respx.mock:
        respx.get(f"{GW}/runner/calls/{CALL_ID}").respond(json={"tool": "pid", "arguments": {}})
        posted = respx.post(f"{GW}/runner/calls/{CALL_ID}/result").respond(json={"ok": True})
        assert main(["call", "--gateway", GW, "--call-id", CALL_ID]) == 0
    result = json.loads(posted.calls.last.request.content)["result"]
    assert result["structuredContent"]["result"] != os.getpid()


def test_credentials_from_stdin_never_reach_tool_code(monkeypatch, tmp_path):
    # action.yml mode: credentials arrive on stdin, not in the environment.
    (tmp_path / "server.py").write_text(
        "import os\nfrom fastmcp import FastMCP\nmcp = FastMCP('t')\n"
        "@mcp.tool\ndef env() -> str:\n    return repr(dict(os.environ))\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_RUNNER_CREDENTIALS", "stdin")
    monkeypatch.setattr(sys, "stdin", io.StringIO("https://oidc.test/token?api-version=2.0\nreq-secret-123\n\n"))
    with respx.mock:
        oidc = respx.get("https://oidc.test/token").respond(json={"value": fake_jwt()})
        respx.get(f"{GW}/runner/calls/{CALL_ID}").respond(json={"tool": "env", "arguments": {}})
        posted = respx.post(f"{GW}/runner/calls/{CALL_ID}/result").respond(json={"ok": True})
        assert main(["call", "--gateway", GW, "--call-id", CALL_ID]) == 0
    assert oidc.calls.last.request.headers["Authorization"] == "bearer req-secret-123"
    assert oidc.calls.last.request.url.params["audience"] == GW  # one audience per gateway
    text = json.loads(posted.calls.last.request.content)["result"]["content"][0]["text"]
    assert "req-secret-123" not in text and "oidc.test" not in text
    assert "MCP_RUNNER_CREDENTIALS" not in text


def test_read_credentials_pads_missing_lines():
    from mcp_runner.security import Credentials, read_credentials

    assert read_credentials(io.StringIO("u\nt")) == Credentials("u", "t", "")
    assert read_credentials(io.StringIO("u\nt\nclient\n")) == Credentials("u", "t", "client")


def test_tool_process_load_error_is_reported(monkeypatch, tmp_path):
    (tmp_path / "server.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_RUNNER_OIDC_TOKEN", fake_jwt())
    with respx.mock:
        respx.get(f"{GW}/runner/calls/{CALL_ID}").respond(json={"tool": "add", "arguments": {}})
        posted = respx.post(f"{GW}/runner/calls/{CALL_ID}/result").respond(json={"ok": True})
        assert main(["call", "--gateway", GW, "--call-id", CALL_ID]) == 0
    result = json.loads(posted.calls.last.request.content)["result"]
    assert result["isError"] and "No FastMCP instance" in result["content"][0]["text"]


def test_tool_process_crash_is_reported(monkeypatch, tmp_path):
    (tmp_path / "server.py").write_text("import os\nos._exit(3)\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_RUNNER_OIDC_TOKEN", fake_jwt())
    with respx.mock:
        respx.get(f"{GW}/runner/calls/{CALL_ID}").respond(json={"tool": "add", "arguments": {}})
        posted = respx.post(f"{GW}/runner/calls/{CALL_ID}/result").respond(json={"ok": True})
        assert main(["call", "--gateway", GW, "--call-id", CALL_ID]) == 0
    result = json.loads(posted.calls.last.request.content)["result"]
    assert result["isError"] and "exited with code 3" in result["content"][0]["text"]


def test_worker_usage_error():
    assert main(["worker", "bogus"]) == 2


def test_runner_redacts_secrets_in_results(monkeypatch, tmp_path):
    (tmp_path / "server.py").write_text(
        "import os\nfrom fastmcp import FastMCP\nmcp = FastMCP('t')\n"
        "@mcp.tool\ndef leak() -> str:\n    return 'key=' + os.environ['DEPLOY_TOKEN'] + ' ok'\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_RUNNER_OIDC_TOKEN", fake_jwt())
    monkeypatch.setenv("DEPLOY_TOKEN", "super-secret-deploy-token")
    with respx.mock:
        respx.get(f"{GW}/runner/calls/{CALL_ID}").respond(json={"tool": "leak", "arguments": {}})
        posted = respx.post(f"{GW}/runner/calls/{CALL_ID}/result").respond(json={"ok": True})
        assert main(["call", "--gateway", GW, "--call-id", CALL_ID]) == 0
    body = posted.calls.last.request.content.decode()
    assert "super-secret-deploy-token" not in body
    result = json.loads(body)["result"]
    assert "key=*** ok" in result["content"][0]["text"]
    assert result["_meta"]["mcp-github-runner/redactions"] >= 1


def test_runner_caps_result_size(monkeypatch, tmp_path):
    (tmp_path / "server.py").write_text(
        "from fastmcp import FastMCP\nmcp = FastMCP('t')\n@mcp.tool\ndef big() -> str:\n    return 'a' * 5000\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_RUNNER_OIDC_TOKEN", fake_jwt())
    with respx.mock:
        respx.get(f"{GW}/runner/calls/{CALL_ID}").respond(json={"tool": "big", "arguments": {}, "max_result_bytes": 1000})
        posted = respx.post(f"{GW}/runner/calls/{CALL_ID}/result").respond(json={"ok": True})
        assert main(["call", "--gateway", GW, "--call-id", CALL_ID]) == 0
    result = json.loads(posted.calls.last.request.content)["result"]
    assert result["_meta"]["mcp-github-runner/denial"]["code"] == -32013


def test_cli_rejects_bad_call_id(monkeypatch):
    monkeypatch.chdir(TEMPLATE)
    assert main(["call", "--gateway", GW, "--call-id", "../../etc"]) == 1


def test_cli_rejects_plain_http_gateway(monkeypatch):
    monkeypatch.chdir(TEMPLATE)
    monkeypatch.setenv("MCP_RUNNER_OIDC_TOKEN", fake_jwt())
    assert main(["call", "--gateway", "http://evil.example", "--call-id", CALL_ID]) == 1


@respx.mock
def test_cli_gateway_error_exits_nonzero(monkeypatch):
    monkeypatch.chdir(TEMPLATE)
    monkeypatch.setenv("MCP_RUNNER_OIDC_TOKEN", fake_jwt())
    respx.get(f"{GW}/runner/calls/{CALL_ID}").respond(status_code=403, json={"detail": "wrong repo"})
    assert main(["call", "--gateway", GW, "--call-id", CALL_ID]) == 1


# -- CLI: manifest mode ---------------------------------------------------------

@respx.mock
def test_cli_manifest_sends_token_hash(monkeypatch):
    monkeypatch.chdir(TEMPLATE)
    monkeypatch.setenv("MCP_RUNNER_OIDC_TOKEN", fake_jwt())
    monkeypatch.setenv("MCP_CLIENT_TOKEN", CLIENT_TOKEN)
    put = respx.put(f"{GW}/runner/manifest").respond(json={"mcp_url": f"{GW}/s/o/r/mcp"})

    assert main(["manifest", "--gateway", GW]) == 0

    body = json.loads(put.calls.last.request.content)
    assert body["client_token_sha256"] == hash_client_token(CLIENT_TOKEN)
    assert CLIENT_TOKEN not in put.calls.last.request.content.decode()


@respx.mock
def test_cli_manifest_rejects_weak_client_token(monkeypatch):
    monkeypatch.chdir(TEMPLATE)
    monkeypatch.setenv("MCP_RUNNER_OIDC_TOKEN", fake_jwt())
    monkeypatch.setenv("MCP_CLIENT_TOKEN", "short")
    put = respx.put(f"{GW}/runner/manifest").respond(json={})
    assert main(["manifest", "--gateway", GW]) == 1
    assert not put.called


def test_oidc_source_removes_request_credentials():
    from mcp_runner.security import OIDCSource

    env = {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.test/token?api-version=2.0",
           "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req"}
    source = OIDCSource("aud", env)
    assert env == {}  # gone before user code runs
    with respx.mock:
        route = respx.get("https://oidc.test/token").respond(json={"value": "jwt"})
        assert source.token() == "jwt"
        assert route.calls.last.request.url.params["audience"] == "aud"
        assert route.calls.last.request.url.params["api-version"] == "2.0"
        assert route.calls.last.request.headers["Authorization"] == "bearer req"
