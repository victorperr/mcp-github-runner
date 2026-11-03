"""Level 3: staging tests against a real gateway and a real GitHub repo.

Skipped unless these are set:
  STAGING_MCP_URL          https://<gateway>/s/<owner>/<staging-repo>/mcp
  STAGING_CLIENT_TOKEN     the staging repo's MCP_CLIENT_TOKEN
  STAGING_REPO             <owner>/<staging-repo>
  STAGING_REVIEWER_TOKEN   token of a required reviewer of mcp-approval (scope: repo)
Optional:
  STAGING_LATENCY_RUNS     default 10
  STAGING_P95_MAX_SECONDS  default 90

Run:  python -m pytest staging -v -s
"""

import asyncio
import os
import re
import statistics
import time

import httpx
import pytest
from fastmcp import Client

URL = os.environ.get("STAGING_MCP_URL", "")
TOKEN = os.environ.get("STAGING_CLIENT_TOKEN", "")
REPO = os.environ.get("STAGING_REPO", "")
REVIEWER = os.environ.get("STAGING_REVIEWER_TOKEN", "")

pytestmark = [
    pytest.mark.skipif(not (URL and TOKEN), reason="staging environment not configured"),
    pytest.mark.asyncio(loop_scope="module"),
]

RUN_URL = re.compile(r"/actions/runs/(\d+)")
GH = "https://api.github.com"


def denial_code(result) -> int | None:
    meta = result.meta or {}
    return (meta.get("mcp-github-runner/denial") or {}).get("code")


async def call(tool, args, **kw):
    async with Client(URL, auth=TOKEN, timeout=900) as client:
        return await client.call_tool(tool, args, raise_on_error=False, **kw)


async def review(run_id: str, state: str) -> None:
    """Approve or reject the pending mcp-approval deployment of a run, as a reviewer."""
    headers = {"Authorization": f"Bearer {REVIEWER}", "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(base_url=GH, headers=headers, timeout=30) as gh:
        pending = (await gh.get(f"/repos/{REPO}/actions/runs/{run_id}/pending_deployments")).json()
        env_ids = [p["environment"]["id"] for p in pending if p["environment"]["name"] == "mcp-approval"]
        assert env_ids, f"run {run_id} has no pending mcp-approval deployment"
        r = await gh.post(f"/repos/{REPO}/actions/runs/{run_id}/pending_deployments",
                          json={"environment_ids": env_ids, "state": state, "comment": f"staging test: {state}"})
        r.raise_for_status()


def reviewing_progress_handler(state: str, seen: dict):
    """Progress handler that reviews the run as soon as its URL shows up in a progress message."""
    async def handler(progress, total, message):
        seen.setdefault("messages", []).append(message or "")
        match = RUN_URL.search(message or "")
        if match and "Waiting for approval" in (message or "") and "run_id" not in seen:
            seen["run_id"] = match.group(1)
            seen["reviewed_at"] = time.monotonic()
            await review(match.group(1), state)
    return handler


# -- S01-S03: basics and latency ------------------------------------------------

async def test_s01_lists_published_tools():
    async with Client(URL, auth=TOKEN) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {"add", "echo_env", "env_keys", "git_credentials_present", "big", "sleep", "gated_action"}


async def test_s02_round_trip():
    result = await call("add", {"a": 20, "b": 22})
    assert not result.is_error, result.content
    assert result.data == 42


async def test_s03_latency():
    runs = int(os.environ.get("STAGING_LATENCY_RUNS", "10"))
    limit = float(os.environ.get("STAGING_P95_MAX_SECONDS", "90"))
    durations = []
    for i in range(runs):
        start = time.monotonic()
        result = await call("add", {"a": i, "b": 1})
        durations.append(time.monotonic() - start)
        assert result.data == i + 1
    durations.sort()
    p50 = statistics.median(durations)
    p95 = durations[max(0, int(round(0.95 * len(durations))) - 1)]
    print(f"\nlatency over {runs} calls: p50={p50:.1f}s p95={p95:.1f}s max={durations[-1]:.1f}s")
    assert p95 <= limit


# -- S04-S05: approvals -----------------------------------------------------------

@pytest.mark.skipif(not (REPO and REVIEWER), reason="needs STAGING_REPO and STAGING_REVIEWER_TOKEN")
async def test_s04_gated_tool_runs_after_approval():
    seen: dict = {}
    result = await call("gated_action", {"label": "s04"}, progress_handler=reviewing_progress_handler("approved", seen))
    assert "run_id" in seen, f"never saw a 'waiting for approval' message: {seen.get('messages')}"
    assert not result.is_error, result.content
    assert "approved action ran: s04" in result.content[0].text


@pytest.mark.skipif(not (REPO and REVIEWER), reason="needs STAGING_REPO and STAGING_REVIEWER_TOKEN")
async def test_s05_rejected_approval_fails_fast():
    seen: dict = {}
    result = await call("gated_action", {"label": "s05"}, progress_handler=reviewing_progress_handler("rejected", seen))
    assert "run_id" in seen
    assert result.is_error
    assert denial_code(result) == -32009  # run ended without a result
    # The gateway polls run status every 15s and waits 3s for a late result.
    assert time.monotonic() - seen["reviewed_at"] < 60


# -- S06-S08: what tool code can see -------------------------------------------------

async def test_s06_secrets_are_redacted():
    result = await call("echo_env", {"name": "STAGING_SECRET"})
    text = result.content[0].text
    assert text == "value=***", text
    assert (result.meta or {}).get("mcp-github-runner/redactions", 0) >= 1


async def test_s07_runner_credentials_are_scrubbed():
    result = await call("env_keys", {})
    keys = set(result.structured_content["result"])
    for name in ("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL",
                 "ACTIONS_RUNTIME_TOKEN", "MCP_CLIENT_TOKEN"):
        assert name not in keys, f"{name} visible to tool code"


async def test_s08_checkout_leaves_no_git_credentials():
    result = await call("git_credentials_present", {})
    assert result.data is False


# -- S09-S12: gateway policy on real traffic ----------------------------------------

async def test_s09_invalid_arguments_rejected_without_a_run():
    start = time.monotonic()
    result = await call("add", {"a": "not-a-number", "b": 1})
    assert denial_code(result) == -32003
    assert time.monotonic() - start < 5  # refused before any workflow run


async def test_s10_result_size_cap():
    result = await call("big", {"n": 50_000})
    assert result.is_error
    assert denial_code(result) == -32013


async def test_s11_wrong_client_token_is_401():
    async with httpx.AsyncClient() as http:
        r = await http.post(URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                            headers={"Authorization": "Bearer wrong-token-wrong-token-wrong-token"})
    assert r.status_code == 401


async def test_s12_unknown_tool():
    result = await call("does_not_exist", {})
    assert denial_code(result) == -32001


# -- S13: concurrency -----------------------------------------------------------------

async def test_s13_concurrent_calls_beyond_limit_are_refused():
    # Staging limit is 3 concurrent calls: fire 5 slow ones at once.
    results = await asyncio.gather(*[call("sleep", {"seconds": 20}) for _ in range(5)])
    codes = [denial_code(r) for r in results]
    assert codes.count(-32005) >= 1, codes
    assert sum(1 for r in results if not r.is_error) >= 1
