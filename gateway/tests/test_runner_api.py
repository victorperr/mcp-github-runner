import hashlib

import httpx

from app.oidc import DevSigner

TOKEN_HASH = hashlib.sha256(b"client-secret").hexdigest()
ADD_TOOL = {"name": "add", "inputSchema": {"type": "object", "properties": {"a": {"type": "integer"}}}}
C1 = "call-one-0000000000000"
C2 = "call-two-0000000000000"


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def register(client, sign, body_extra=None, **kw):
    body = {"server_name": "demo", "tools": [ADD_TOOL], "client_token_sha256": TOKEN_HASH, **(body_extra or {})}
    return client.put("/runner/manifest", json=body, headers=auth(sign("push", **kw)))


def test_register_manifest(client, sign, app):
    r = register(client, sign)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mcp_url"] == "http://gw.test/s/me/demo/mcp" and body["tools"] == 1 and body["warnings"] == []
    server = app.state.store.get_server("1")
    assert server.workflow_path == ".github/workflows/mcp.yml"
    assert server.token_hash == TOKEN_HASH
    assert "client_token_sha256" not in server.manifest


def test_first_registration_needs_token(client, sign):
    r = client.put("/runner/manifest", json={"tools": []}, headers=auth(sign("push")))
    assert r.status_code == 422


def test_manifest_rejects_other_events_and_refs(client, sign):
    r = client.put("/runner/manifest", json={"tools": [], "client_token_sha256": TOKEN_HASH},
                   headers=auth(sign("pull_request")))
    assert r.status_code == 403
    assert register(client, sign).status_code == 200
    assert register(client, sign, ref="refs/heads/feature").status_code == 409


def test_manifest_pins_workflow_file(client, sign, app):
    # Another workflow with id-token: write on the same branch can't take the server over.
    assert register(client, sign).status_code == 200
    r = register(client, sign, {"client_token_sha256": "0" * 64}, workflow_path=".github/workflows/deploy.yml")
    assert r.status_code == 409
    assert app.state.store.get_server("1").token_hash == TOKEN_HASH


def test_tool_call_run_cannot_upload_manifest(client, sign, app, dispatcher):
    # A token from a tool-call job (also workflow_dispatch) must not re-register the
    # server with its own client token.
    assert register(client, sign).status_code == 200
    body = {"tools": [ADD_TOOL], "client_token_sha256": "0" * 64}
    tok = sign("workflow_dispatch", run_id="77")
    r = client.put("/runner/manifest", json=body, headers=auth(tok))
    assert r.status_code == 403
    assert app.state.store.get_server("1").token_hash == TOKEN_HASH

    # A manual manifest run (confirmed by GitHub) is still allowed.
    dispatcher.verify_manifest = True
    assert client.put("/runner/manifest", json=body, headers=auth(tok)).status_code == 200
    assert app.state.store.get_server("1").token_hash == "0" * 64


def test_first_registration_needs_app_installed(client, sign, dispatcher):
    dispatcher.installed = False
    assert register(client, sign).status_code == 403
    dispatcher.installed = httpx.ConnectError("down")
    assert register(client, sign).status_code == 503
    dispatcher.installed = True
    assert register(client, sign).status_code == 200


def test_owner_allowlist(client, sign, app):
    app.state.settings.allowed_owners = frozenset({"acme"})
    assert register(client, sign).status_code == 403  # me/demo
    assert register(client, sign, repository="ACME/tools", repository_id="9").status_code == 200


def test_manifest_is_strict(client, sign):
    # Unknown fields, bad tool names, duplicates and bad limits are all rejected, never ignored.
    assert register(client, sign, {"surprise": True}).status_code == 422
    assert register(client, sign, {"tools": [{"description": "no name"}]}).status_code == 422
    assert register(client, sign, {"tools": [{"name": "bad name!", "inputSchema": {"type": "object"}}]}).status_code == 422
    assert register(client, sign, {"tools": [ADD_TOOL, ADD_TOOL]}).status_code == 422
    assert register(client, sign, {"policy": {"limits": {"max_calls_per_hour": 0}}}).status_code == 422
    assert register(client, sign, {"policy": {"limits": {"max_calls_per_hour": True}}}).status_code == 422
    assert register(client, sign, {"policy": {"limits": {"unlimited": 1}}}).status_code == 422
    assert register(client, sign, {"policy": {"rules": {}}}).status_code == 422


def test_limits_above_gateway_caps_are_clamped(client, sign, app):
    r = register(client, sign, {"policy": {"limits": {"call_timeout_seconds": 3600, "max_calls_per_hour": 10}}})
    assert r.status_code == 200
    body = r.json()
    assert body["limits"]["call_timeout_seconds"] == app.state.broker.ceilings.call_timeout_seconds
    assert body["limits"]["max_calls_per_hour"] == 10  # lowering is always allowed
    assert any("call_timeout_seconds" in w for w in body["warnings"])


def test_bad_oidc_tokens(client, app, tmp_path):
    assert client.put("/runner/manifest", json={}).status_code == 401
    assert client.put("/runner/manifest", json={}, headers=auth("garbage")).status_code == 401
    other_key = DevSigner(str(tmp_path / "other.pem"))
    forged = other_key.sign(audience=app.state.settings.oidc_audience, repository="me/demo",
                            repository_id="1", event_name="push")
    assert client.put("/runner/manifest", json={}, headers=auth(forged)).status_code == 401
    wrong_aud = app.state.dev_signer.sign(audience="someone-else", repository="me/demo",
                                          repository_id="1", event_name="push")
    assert client.put("/runner/manifest", json={}, headers=auth(wrong_aud)).status_code == 401
    expired = app.state.dev_signer.sign(audience=app.state.settings.oidc_audience, repository="me/demo",
                                        repository_id="1", event_name="push", ttl=-10)
    assert client.put("/runner/manifest", json={}, headers=auth(expired)).status_code == 401


def _pending_call(app, call_id=C1):
    app.state.store.create_call(call_id, "1", "add", {"a": 1})


def test_call_fetch_and_result(client, sign, app):
    register(client, sign)
    _pending_call(app)
    tok = sign("workflow_dispatch", run_id="77")

    r = client.get(f"/runner/calls/{C1}", headers=auth(tok))
    assert r.status_code == 200
    assert r.json() == {"call_id": C1, "tool": "add", "arguments": {"a": 1}, "max_result_bytes": 1024 * 1024}
    assert app.state.store.get_call(C1).arguments is None  # dropped once read

    assert client.get(f"/runner/calls/{C1}", headers=auth(tok)).status_code == 409  # no replay

    other_run = sign("workflow_dispatch", run_id="78")
    result = {"result": {"content": [{"type": "text", "text": "2"}]}}
    assert client.post(f"/runner/calls/{C1}/result", json=result, headers=auth(other_run)).status_code == 409

    r = client.post(f"/runner/calls/{C1}/result", json=result, headers=auth(tok))
    assert r.status_code == 200
    assert app.state.store.get_call(C1).status == "done"
    assert client.post(f"/runner/calls/{C1}/result", json=result, headers=auth(tok)).status_code == 409


def test_call_must_be_claimed_by_its_own_run(client, sign, app, dispatcher):
    # A valid token from another run of the same workflow (e.g. leaked) can't claim the call.
    register(client, sign)
    _pending_call(app)
    dispatcher.verify = False
    r = client.get(f"/runner/calls/{C1}", headers=auth(sign("workflow_dispatch", run_id="99")))
    assert r.status_code == 403 and "not started for this call" in r.json()["detail"]
    assert app.state.store.get_call(C1).status == "pending"


def test_call_isolation_between_repos(client, sign, app):
    register(client, sign)
    register(client, sign, repository="evil/repo", repository_id="2")
    _pending_call(app)
    r = client.get(f"/runner/calls/{C1}", headers=auth(sign("workflow_dispatch", repository="evil/repo", repository_id="2")))
    assert r.status_code == 404


def test_call_checks_event_workflow_and_ref(client, sign, app):
    register(client, sign)
    _pending_call(app)
    assert client.get(f"/runner/calls/{C1}", headers=auth(sign("push"))).status_code == 403
    other_wf = sign("workflow_dispatch", workflow_path=".github/workflows/other.yml")
    assert client.get(f"/runner/calls/{C1}", headers=auth(other_wf)).status_code == 403
    other_ref = sign("workflow_dispatch", ref="refs/heads/feature")
    assert client.get(f"/runner/calls/{C1}", headers=auth(other_ref)).status_code == 403


def test_bad_call_ids_are_unknown(client, sign):
    register(client, sign)
    assert client.get("/runner/calls/short", headers=auth(sign("workflow_dispatch"))).status_code == 404


def test_result_size_is_capped(client, sign, app):
    register(client, sign, {"policy": {"limits": {"max_result_bytes": 100}}})
    _pending_call(app)
    tok = sign("workflow_dispatch", run_id="5")
    assert client.get(f"/runner/calls/{C1}", headers=auth(tok)).status_code == 200
    huge = {"result": {"content": [{"type": "text", "text": "x" * 200_000}]}}
    assert client.post(f"/runner/calls/{C1}/result", json=huge, headers=auth(tok)).status_code == 413
    unknown = {"result": {"content": []}, "extra": 1}
    assert client.post(f"/runner/calls/{C1}/result", json=unknown, headers=auth(tok)).status_code == 422
