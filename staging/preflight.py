"""Check the staging setup before running the staging tests. Read-only.

    STAGING_REPO=owner/repo STAGING_MCP_URL=https://<gateway>/s/owner/repo/mcp python staging/preflight.py

GitHub access uses GITHUB_TOKEN, or `gh auth token` when it isn't set. The token
needs read access to the staging repo's settings (a repo admin's token, or a
fine-grained token with Administration, Environments, Secrets, Variables,
Actions and Contents: read). Secret values are never read, only their names.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

import httpx

GITHUB_API = "https://api.github.com"
APPROVAL_ENV = "mcp-approval"
REPO_FILES = ("server.py", "mcp-runner.toml", "uv.lock", ".github/workflows/mcp.yml")
REPO_SECRETS = ("MCP_CLIENT_TOKEN", "STAGING_SECRET")


@dataclass
class Check:
    status: str  # PASS | FAIL | WARN
    name: str
    detail: str = ""


def gateway_base(mcp_url: str) -> str:
    return mcp_url.split("/s/", 1)[0].rstrip("/")


def check_gateway(gw: httpx.Client, mcp_url: str) -> list[Check]:
    base = gateway_base(mcp_url)
    out = [Check("PASS" if base.startswith("https://") else "FAIL", "gateway uses https", base)]
    try:
        health = gw.get(f"{base}/healthz")
        out.append(Check("PASS" if health.status_code == 200 else "FAIL", "gateway /healthz", str(health.status_code)))
        anon = gw.post(mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        out.append(Check("PASS" if anon.status_code == 401 else "FAIL", "MCP endpoint refuses no token",
                         str(anon.status_code)))
        docs = gw.get(f"{base}/openapi.json")
        out.append(Check("PASS" if docs.status_code == 404 else "FAIL", "gateway is not in dev mode (no /openapi.json)",
                         str(docs.status_code)))
    except httpx.HTTPError as exc:
        out.append(Check("FAIL", "gateway reachable", str(exc)))
    return out


def check_repo(gh: httpx.Client, repo: str, mcp_url: str) -> list[Check]:
    out: list[Check] = []

    r = gh.get(f"/repos/{repo}")
    if r.status_code != 200:
        return [Check("FAIL", "repo readable with this token", f"{r.status_code} {r.text[:200]}")]
    out.append(Check("PASS" if r.json().get("private") else "FAIL", "repo is private",
                     "self-hosted runners must never serve a public repo"))

    for path in REPO_FILES:
        found = gh.get(f"/repos/{repo}/contents/{path}").status_code == 200
        out.append(Check("PASS" if found else "FAIL", f"file {path}"))

    for name in REPO_SECRETS:
        found = gh.get(f"/repos/{repo}/actions/secrets/{name}").status_code == 200
        out.append(Check("PASS" if found else "FAIL", f"secret {name} exists"))

    gw_var = gh.get(f"/repos/{repo}/actions/variables/MCP_GATEWAY_URL")
    want = gateway_base(mcp_url)
    if gw_var.status_code != 200:
        out.append(Check("FAIL", "variable MCP_GATEWAY_URL", "missing"))
    else:
        value = gw_var.json().get("value", "").rstrip("/")
        out.append(Check("PASS" if value == want else "FAIL", "variable MCP_GATEWAY_URL matches STAGING_MCP_URL",
                         f"{value!r} vs {want!r}. The OIDC audience is this URL on both sides"))

    labels: list[str] = ["self-hosted"]
    runs_on = gh.get(f"/repos/{repo}/actions/variables/MCP_RUNS_ON")
    if runs_on.status_code == 200:
        try:
            labels = json.loads(runs_on.json().get("value", ""))
            out.append(Check("PASS", "variable MCP_RUNS_ON", str(labels)))
        except ValueError:
            out.append(Check("FAIL", "variable MCP_RUNS_ON is a JSON list", runs_on.json().get("value", "")))
    else:
        out.append(Check("WARN", "variable MCP_RUNS_ON", 'missing: jobs will use ["self-hosted"]'))

    out += check_environment(gh, repo)
    out += check_runners(gh, repo, labels if isinstance(labels, list) else [labels])

    runs = gh.get(f"/repos/{repo}/actions/workflows/mcp.yml/runs", params={"event": "push", "per_page": 1})
    latest = (runs.json().get("workflow_runs") or [None])[0] if runs.status_code == 200 else None
    if latest is None:
        out.append(Check("WARN", "a manifest run on push", "none yet: push the staging repo once"))
    else:
        ok = latest.get("conclusion") == "success"
        out.append(Check("PASS" if ok else "FAIL", "latest manifest run succeeded",
                         f"{latest.get('status')}/{latest.get('conclusion')} {latest.get('html_url')}"))
    return out


def check_environment(gh: httpx.Client, repo: str) -> list[Check]:
    r = gh.get(f"/repos/{repo}/environments/{APPROVAL_ENV}")
    if r.status_code != 200:
        return [Check("FAIL", f"environment {APPROVAL_ENV} exists", str(r.status_code))]
    env = r.json()
    rules = env.get("protection_rules") or []
    reviewers = any(rule.get("type") == "required_reviewers" and rule.get("reviewers") for rule in rules)
    return [
        Check("PASS", f"environment {APPROVAL_ENV} exists"),
        Check("PASS" if reviewers else "FAIL", f"{APPROVAL_ENV} has required reviewers"),
        Check("PASS" if env.get("can_admins_bypass") is False else "FAIL", f"{APPROVAL_ENV} admin bypass is off",
              "the gateway refuses gated calls (-32006) while admins can bypass"),
        Check("PASS" if env.get("deployment_branch_policy") else "WARN", f"{APPROVAL_ENV} limited to selected branches",
              "recommended: main only"),
    ]


def check_runners(gh: httpx.Client, repo: str, labels: list[str]) -> list[Check]:
    r = gh.get(f"/repos/{repo}/actions/runners")
    if r.status_code != 200:
        return [Check("WARN", "self-hosted runner online", f"can't list runners ({r.status_code})")]
    wanted = {str(label).lower() for label in labels}
    matching = [
        runner for runner in r.json().get("runners", [])
        if wanted <= {label["name"].lower() for label in runner.get("labels", [])}
    ]
    online = [runner for runner in matching if runner.get("status") == "online"]
    if online:
        return [Check("PASS", "self-hosted runner online with the MCP_RUNS_ON labels",
                      ", ".join(f"{x['name']} ({x.get('os')})" for x in online))]
    if not r.json().get("runners"):
        # Runners from an organization runner group aren't listed on the repo endpoint.
        return [Check("WARN", "self-hosted runner online", "no repo-level runners: check the org runner group")]
    return [Check("FAIL", "self-hosted runner online with the MCP_RUNS_ON labels", f"wanted {sorted(wanted)}")]


def github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    gh_cli = shutil.which("gh")
    if not token and gh_cli:
        token = subprocess.run([gh_cli, "auth", "token"], capture_output=True, text=True, check=False).stdout.strip()
    return token


def main() -> int:
    repo = os.environ.get("STAGING_REPO", "")
    mcp_url = os.environ.get("STAGING_MCP_URL", "")
    if not repo or not mcp_url:
        print("Set STAGING_REPO and STAGING_MCP_URL (see docs/testing.md, level 3).")
        return 2
    token = github_token()
    if not token:
        print("Set GITHUB_TOKEN or log in with `gh auth login`.")
        return 2

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    with httpx.Client(timeout=20) as gw, httpx.Client(base_url=GITHUB_API, headers=headers, timeout=20) as gh:
        checks = check_gateway(gw, mcp_url) + check_repo(gh, repo, mcp_url)

    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f"[{c.status}] {c.name.ljust(width)}  {c.detail}")
    failed = sum(c.status == "FAIL" for c in checks)
    print(f"\n{failed} failed, {sum(c.status == 'WARN' for c in checks)} warnings")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
