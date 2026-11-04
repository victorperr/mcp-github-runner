"""Approval gating: runner API enforcement, broker behaviour, GitHub environment checks."""

import hashlib
import json

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.calls import CallBroker
from app.dispatch import DispatchError, GitHubAppDispatcher, RunInfo
from app.policy import Limits
from app.store import ServerRecord, Store

from .conftest import RecordingDispatcher

TOKEN_HASH = hashlib.sha256(b"client-secret").hexdigest()
G1 = "gated-call-000000000000"
U1 = "plain-call-000000000000"
TOOLS = [
    {"name": "add", "inputSchema": {"type": "object"}},
    {"name": "restart_service", "inputSchema": {"type": "object"}},
]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def register(client, sign, **extra):
    body = {"tools": TOOLS, "client_token_sha256": TOKEN_HASH,
            "approval_required": ["restart_service"], "approval_environment": "mcp-approval", **extra}
    return client.put("/runner/manifest", json=body, headers=auth(sign("push")))


# -- runner API ---------------------------------------------------------------

def test_manifest_rejects_unknown_approval_tool(client, sign):
    assert register(client, sign, approval_required=["nope"]).status_code == 422


def test_gated_call_needs_environment_claim(client, sign, app):
    assert register(client, sign).status_code == 200
    server = app.state.store.get_server("1")
    assert server.approval_environment_for("restart_service") == "mcp-approval"
    assert server.approval_environment_for("add") is None

    app.state.store.create_call(G1, "1", "restart_service", {}, "mcp-approval")
    no_env = sign("workflow_dispatch", run_id="5")
    r = client.get(f"/runner/calls/{G1}", headers=auth(no_env))
    assert r.status_code == 403 and "requires approval" in r.json()["detail"]
    wrong_env = sign("workflow_dispatch", run_id="5", environment="staging")
    assert client.get(f"/runner/calls/{G1}", headers=auth(wrong_env)).status_code == 403

    approved = sign("workflow_dispatch", run_id="5", environment="mcp-approval")
    assert client.get(f"/runner/calls/{G1}", headers=auth(approved)).status_code == 200
    result = {"result": {"content": [{"type": "text", "text": "ok"}]}}
    assert client.post(f"/runner/calls/{G1}/result", json=result, headers=auth(no_env)).status_code == 403
    assert client.post(f"/runner/calls/{G1}/result", json=result, headers=auth(approved)).status_code == 200


def test_ungated_call_works_without_environment(client, sign, app):
    register(client, sign)
    app.state.store.create_call(U1, "1", "add", {})
    assert client.get(f"/runner/calls/{U1}", headers=auth(sign("workflow_dispatch"))).status_code == 200


# -- broker -------------------------------------------------------------------

def _server(approval=True):
    manifest = {"tools": TOOLS, "approval_required": ["restart_service"] if approval else [],
                "approval_environment": "mcp-approval"}
    return ServerRecord("1", "me/demo", ".github/workflows/mcp.yml", "refs/heads/main", TOKEN_HASH, manifest)


def _broker(tmp_path, dispatcher, timeout=5, approval_timeout=5, max_pending=5):
    ceilings = Limits(call_timeout_seconds=timeout, approval_timeout_seconds=approval_timeout,
                      max_concurrent_calls=max_pending)
    return CallBroker(Store(str(tmp_path / "b.db")), dispatcher, ceilings=ceilings,
                      heartbeat_interval=0.05, status_poll_interval=0)


async def test_broker_passes_approval_env_and_reports_waiting(tmp_path):
    url = "https://github.com/me/demo/actions/runs/9"
    d = RecordingDispatcher(statuses=[RunInfo("waiting", None, url)] * 3 + [RunInfo("completed", "failure", url)])
    broker = _broker(tmp_path, d)
    messages = []

    async def hb(elapsed, msg):
        messages.append(msg)

    result = await broker.run(_server(), "restart_service", {"name": "x"}, hb)
    assert d.calls[0][1] == "mcp-approval"
    assert "needs approval" in messages[0]
    assert any("Waiting for approval" in m and url in m for m in messages)
    # Rejected approval ends the run: fail fast instead of waiting for the timeout.
    assert result["isError"] and "Was the approval rejected?" in result["content"][0]["text"]


async def test_broker_ungated_tool_has_no_approval_env(tmp_path):
    d = RecordingDispatcher(statuses=[RunInfo("completed", "success", "u")])
    broker = _broker(tmp_path, d)
    result = await broker.run(_server(), "add", {}, None)
    assert d.calls[0][1] is None
    assert result["isError"] and "without returning a result" in result["content"][0]["text"]


async def test_broker_hints_when_no_runner(tmp_path, monkeypatch):
    import app.calls as calls

    monkeypatch.setattr(calls, "NO_RUNNER_HINT_AFTER", 0)
    d = RecordingDispatcher(statuses=[RunInfo("queued", None, "u")] * 100)
    broker = _broker(tmp_path, d, timeout=0.3)
    messages = []

    async def hb(elapsed, msg):
        messages.append(msg)

    result = await broker.run(_server(approval=False), "add", {}, hb)
    assert result["isError"] and "No result after" in result["content"][0]["text"]
    assert any("Is a runner with the right labels online?" in m for m in messages)


async def test_broker_uses_approval_timeout(tmp_path):
    import time

    broker = _broker(tmp_path, RecordingDispatcher(), timeout=0.1, approval_timeout=0.6)

    t0 = time.monotonic()
    plain = await broker.run(_server(), "add", {}, None)
    plain_wait = time.monotonic() - t0

    t0 = time.monotonic()
    gated = await broker.run(_server(), "restart_service", {}, None)
    gated_wait = time.monotonic() - t0

    assert plain["isError"] and gated["isError"]
    assert plain_wait < 0.4 <= gated_wait


async def test_broker_result_wins_over_completed_status(tmp_path):
    d = RecordingDispatcher()
    broker = _broker(tmp_path, d)

    async def status(server, call_id):
        broker.complete(call_id, {"content": [{"type": "text", "text": "done"}]})
        return RunInfo("completed", "success", "u")

    d.run_status = status
    result = await broker.run(_server(approval=False), "add", {}, None)
    assert result == {"content": [{"type": "text", "text": "done"}]}


# -- GitHub App dispatcher ------------------------------------------------------

API = "https://api.github.test"


@pytest.fixture
def gh():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    with respx.mock(base_url=API, assert_all_called=False) as mock:
        mock.get("/repos/me/demo/installation").respond(json={"id": 42})
        mock.post("/app/installations/42/access_tokens").respond(json={"token": "inst", "expires_at": "x"})
        yield GitHubAppDispatcher("123", pem, API), mock


async def test_dispatch_refuses_missing_environment(gh):
    d, mock = gh
    mock.get("/repos/me/demo/environments/mcp-approval").respond(404)
    with pytest.raises(DispatchError, match="no 'mcp-approval' environment"):
        await d.dispatch(_server(), "c1", "mcp-approval")


async def test_dispatch_refuses_environment_without_reviewers(gh):
    d, mock = gh
    mock.get("/repos/me/demo/environments/mcp-approval").respond(
        json={"protection_rules": [{"type": "wait_timer", "wait_timer": 5}]})
    with pytest.raises(DispatchError, match="no required reviewers"):
        await d.dispatch(_server(), "c1", "mcp-approval")


PROTECTED = {"can_admins_bypass": False,
             "protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "User"}]}]}


async def test_dispatch_refuses_environment_admins_can_bypass(gh):
    d, mock = gh
    route = mock.get("/repos/me/demo/environments/mcp-approval")
    for body in ({**PROTECTED, "can_admins_bypass": True}, {"protection_rules": PROTECTED["protection_rules"]}):
        route.respond(json=body)  # explicit true, or missing (GitHub's default is true)
        with pytest.raises(DispatchError, match="bypass"):
            await d.dispatch(_server(), "c1", "mcp-approval")


async def test_environment_name_is_url_encoded(gh):
    d, mock = gh
    route = mock.get("/repos/me/demo/environments/prod%20approvers").respond(json=PROTECTED)
    mock.post("/repos/me/demo/actions/workflows/mcp.yml/dispatches").respond(204)
    await d.dispatch(_server(), "c1", "prod approvers")
    assert route.called


def test_dot_only_environment_names_are_rejected(client, sign):
    assert register(client, sign, approval_environment="..").status_code == 422
    assert register(client, sign, approval_environment=".").status_code == 422
    assert register(client, sign, approval_environment="prod.approvers").status_code == 200


async def test_dispatch_with_approval(gh):
    d, mock = gh
    mock.get("/repos/me/demo/environments/mcp-approval").respond(json=PROTECTED)
    route = mock.post("/repos/me/demo/actions/workflows/mcp.yml/dispatches").respond(204)
    await d.dispatch(_server(), "c1", "mcp-approval")
    body = json.loads(route.calls.last.request.content)
    assert body == {"ref": "main", "inputs": {"call_id": "c1", "approval": "true"}}
    assert route.calls.last.request.headers["Authorization"] == "Bearer inst"


async def test_dispatch_without_approval_skips_env_check(gh):
    d, mock = gh
    env = mock.get("/repos/me/demo/environments/mcp-approval")
    route = mock.post("/repos/me/demo/actions/workflows/mcp.yml/dispatches").respond(204)
    await d.dispatch(_server(), "c2", None)
    assert not env.called
    assert json.loads(route.calls.last.request.content)["inputs"]["approval"] == "false"


async def test_run_status_finds_run_by_title(gh):
    d, mock = gh
    listing = mock.get("/repos/me/demo/actions/workflows/mcp.yml/runs").respond(json={"workflow_runs": [
        {"id": 1, "display_title": "MCP call other", "status": "queued", "html_url": "u1"},
        {"id": 2, "display_title": "MCP call c9", "status": "waiting", "conclusion": None, "html_url": "u2"},
    ]})
    mock.get("/repos/me/demo/actions/runs/2").respond(
        json={"id": 2, "status": "completed", "conclusion": "failure", "html_url": "u2"})
    assert await d.run_status(_server(), "c9") == RunInfo("waiting", None, "u2")
    params = listing.calls.last.request.url.params
    assert params["branch"] == "main" and params["created"].startswith(">=") and params["per_page"] == "100"
    assert await d.run_status(_server(), "c9") == RunInfo("completed", "failure", "u2")
    assert await d.run_status(_server(), "missing") is None
    d.forget("c9")
    assert "c9" not in d._run_ids


async def test_installation_tokens_are_per_repo_and_refreshed_once(gh):
    import asyncio

    d, mock = gh
    tokens = mock.post("/app/installations/42/access_tokens").respond(json={"token": "inst"})
    mock.get("/repos/me/other/installation").respond(json={"id": 42})
    other = ServerRecord("2", "me/other", ".github/workflows/mcp.yml", "refs/heads/main", None, None)
    await asyncio.gather(*(d._installation_token(_server()) for _ in range(5)))
    assert tokens.call_count == 1  # concurrent callers share one refresh
    await d._installation_token(other)
    assert tokens.call_count == 2  # same installation, different repo: its own scoped token
    assert json.loads(tokens.calls.last.request.content) == {"repository_ids": [2]}


async def test_broker_forgets_call_state(tmp_path):
    d = RecordingDispatcher(statuses=[RunInfo("completed", "success", "u")])
    await _broker(tmp_path, d).run(_server(), "add", {}, None)
    assert d.forgotten == [d.calls[0][0]]


async def test_run_status_swallows_api_errors(gh):
    d, mock = gh
    mock.get("/repos/me/demo/actions/workflows/mcp.yml/runs").mock(side_effect=httpx.ConnectError("down"))
    assert await d.run_status(_server(), "c9") is None


def _run(**over):
    run = {"id": 55, "display_title": "MCP call c9", "event": "workflow_dispatch",
           "path": ".github/workflows/mcp.yml", "repository": {"id": 1}}
    run.update(over)
    return run


async def test_verify_run_accepts_only_the_run_started_for_the_call(gh):
    d, mock = gh
    route = mock.get("/repos/me/demo/actions/runs/55")
    route.respond(json=_run())
    assert await d.verify_run(_server(), "c9", "55") is True
    for bad in (_run(display_title="MCP call other"), _run(event="push"),
                _run(path=".github/workflows/x.yml"), _run(repository={"id": 2})):
        route.respond(json=bad)
        assert await d.verify_run(_server(), "c9", "55") is False


async def test_verify_manifest_run_needs_manifest_title(gh):
    d, mock = gh
    route = mock.get("/repos/me/demo/actions/runs/55")
    route.respond(json=_run(display_title="MCP manifest"))
    assert await d.verify_manifest_run(_server(), "55") is True
    route.respond(json=_run())  # a tool-call run
    assert await d.verify_manifest_run(_server(), "55") is False


async def test_is_installed(gh):
    d, mock = gh
    assert await d.is_installed(_server()) is True
    mock.get("/repos/other/repo/installation").respond(404)
    other = ServerRecord("2", "other/repo", ".github/workflows/mcp.yml", "refs/heads/main", None, None)
    assert await d.is_installed(other) is False


async def test_verify_run_fails_closed(gh):
    d, mock = gh
    assert await d.verify_run(_server(), "c9", "local-1") is False  # not a GitHub run id
    mock.get("/repos/me/demo/actions/runs/55").respond(404)
    assert await d.verify_run(_server(), "c9", "55") is False
    mock.get("/repos/me/demo/actions/runs/56").mock(side_effect=httpx.ConnectError("down"))
    assert await d.verify_run(_server(), "c9", "56") is False


async def test_approval_failures_have_their_own_code(tmp_path):
    class NoEnv(RecordingDispatcher):
        async def dispatch(self, server, call_id, approval_env=None):
            raise DispatchError("no reviewers", approval=True)

    result = await _broker(tmp_path, NoEnv()).run(_server(), "restart_service", {}, None)
    assert result["_meta"]["mcp-github-runner/denial"]["code"] == -32006
