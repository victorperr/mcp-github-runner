"""Offline tests for staging/preflight.py (mocked GitHub and gateway)."""

import httpx
import respx

from preflight import GITHUB_API, REPO_SECRETS, check_gateway, check_repo

REPO = "me/staging"
GW = "https://gw.example.com"
MCP_URL = f"{GW}/s/{REPO}/mcp"


def mock_good_setup(mock):
    mock.get(f"{GW}/healthz").respond(json={"status": "ok"})
    mock.post(MCP_URL).respond(401)
    mock.get(f"{GW}/openapi.json").respond(404)
    api = f"{GITHUB_API}/repos/{REPO}"
    mock.get(api).respond(json={"private": True})
    mock.get(url__startswith=f"{api}/contents/").respond(json={})
    for name in REPO_SECRETS:  # one route each, so a test can override a single secret
        mock.get(f"{api}/actions/secrets/{name}").respond(json={})
    mock.get(f"{api}/actions/variables/MCP_GATEWAY_URL").respond(json={"value": GW + "/"})
    mock.get(f"{api}/actions/variables/MCP_RUNS_ON").respond(json={"value": '["self-hosted", "mcp"]'})
    mock.get(f"{api}/environments/mcp-approval").respond(json={
        "can_admins_bypass": False, "deployment_branch_policy": {"custom_branch_policies": True},
        "protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "User"}]}],
    })
    mock.get(f"{api}/actions/runners").respond(json={"runners": [
        {"name": "r1", "os": "Linux", "status": "online",
         "labels": [{"name": "self-hosted"}, {"name": "Linux"}, {"name": "mcp"}]},
    ]})
    mock.get(f"{api}/actions/workflows/mcp.yml/runs").respond(json={"workflow_runs": [
        {"status": "completed", "conclusion": "success", "html_url": "u"},
    ]})


def run_checks():
    with httpx.Client() as gw, httpx.Client(base_url=GITHUB_API) as gh:
        return check_gateway(gw, MCP_URL) + check_repo(gh, REPO, MCP_URL)


def test_good_setup_passes():
    with respx.mock(assert_all_called=False) as mock:
        mock_good_setup(mock)
        checks = run_checks()
    assert [c for c in checks if c.status != "PASS"] == []


def test_common_mistakes_fail():
    with respx.mock(assert_all_called=False) as mock:
        mock_good_setup(mock)
        api = f"{GITHUB_API}/repos/{REPO}"
        mock.get(f"{GW}/openapi.json").respond(200)  # dev mode left on
        mock.get(api).respond(json={"private": False})
        mock.get(f"{api}/actions/secrets/STAGING_SECRET").respond(404)
        mock.get(f"{api}/actions/variables/MCP_GATEWAY_URL").respond(json={"value": "https://other.example.com"})
        mock.get(f"{api}/environments/mcp-approval").respond(json={
            "can_admins_bypass": True,  # GitHub's default
            "protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "User"}]}],
        })
        checks = run_checks()
    failed = {c.name for c in checks if c.status == "FAIL"}
    assert failed == {
        "gateway is not in dev mode (no /openapi.json)",
        "repo is private",
        "secret STAGING_SECRET exists",
        "variable MCP_GATEWAY_URL matches STAGING_MCP_URL",
        "mcp-approval admin bypass is off",
    }
    assert any(c.status == "WARN" and "selected branches" in c.name for c in checks)
