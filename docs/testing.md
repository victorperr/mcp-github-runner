# Test framework and release gates

This is the test plan for taking MCP GitHub Runner from a tested prototype to something you can sell. Each level has exact commands and pass criteria, and the **release gates** at the end decide whether a version may be sold.

| Level | What it proves | How it runs | Status (2026-11-06) |
|---|---|---|---|
| L0 Static | Code and workflows follow safe patterns; no vulnerable dependencies; the gateway image builds and starts | CI `lint`, `audit` and `image` jobs | ✅ green in CI on `main`, and locally |
| L1 Unit | Each module's logic, including the policy rules | CI `test` job | ✅ 169 tests, coverage 89% runner / 94% gateway (gates 85% / 90%) |
| L2 Local end to end | Real gateway, runner and MCP client, no GitHub | CI `test` job (`gateway/tests/test_e2e.py`) | ✅ passes |
| L3 Staging on real GitHub | Approvals, OIDC claims, run binding and runners on real infrastructure | `staging.yml` (manual) + manual cases | 🟡 automated S01–S15 pass; manual M01–M19 not run |
| L4 Security | Someone actively trying to break it | Fuzzing + manual pentest | ❌ not run |
| L5 Load and reliability | Behaviour under traffic, restarts, outages | Load script + chaos cases | ❌ not run |
| L6 Pilot | Real customers get value | 2–3 design partners | ❌ not started |

---

## L0 Static checks (CI, every push)

| Check | Command | Pass criteria |
|---|---|---|
| Python lint + bandit security rules | `ruff check .` | 0 findings |
| Workflow security (template injection, unpinned actions, excessive permissions…) | `zizmor --offline --config zizmor.yml .github/workflows action/action.yml template/.github/workflows` | 0 findings |
| Known vulnerabilities, template lockfile | `uv export --frozen …` then `pip-audit --require-hashes` (see `ci.yml`) | 0 findings that aren't in the reviewed list below |
| Known vulnerabilities, dev and gateway deps | `pip-audit -r requirements-dev.txt` | same |
| Pinned actions and dependencies stay current | Manual, before each release: check new versions of the pinned actions (`action.yml`, workflows) and of `fastmcp`/`mcp`; upgrade on a branch and run all levels. Known vulnerabilities are caught on every push by the `audit` job | Nothing more than one release behind without a reason |

### Known vulnerabilities (reviewed, ignored in CI)

| ID | Package | Why it's ignored | Exit condition |
|---|---|---|---|
| PYSEC-2026-2475 | fastmcp 2.14.7 | Command injection in `fastmcp install` on Windows. We never run that command | Upgrade to fastmcp ≥ 3.2 |
| PYSEC-2026-2476 | fastmcp 2.14.7 | OAuthProxy + GitHub provider consent bypass. We don't use OAuthProxy. **Must be fixed before adding GitHub OAuth for clients** | Upgrade to fastmcp ≥ 3.2 |
| PYSEC-2026-2447 | diskcache 5.6.3 | Pickle loading from a cache dir an attacker can write to. Only a risk on shared, non-ephemeral runners | Ephemeral runners; drop when the dependency is gone |

**Customer security scanners will flag these.** Moving to fastmcp 3.x is release gate G1.

## L1 Unit and L2 local end-to-end (CI, every push)

```bash
pip install -r requirements-dev.txt
cd action  && python -m pytest --cov=mcp_runner --cov-fail-under=85
cd gateway && python -m pytest --cov=app --cov-fail-under=90
```

Pass criteria: all green on Python 3.11 and 3.12, coverage at or above the thresholds. What's covered:

| Area | Test file | Key properties |
|---|---|---|
| Gateway policy | `gateway/tests/test_policy.py` | Each check denies with its own code; first failing check wins (all 26 combinations); no spurious allow; bad limits never mean unlimited |
| Runner API | `gateway/tests/test_runner_api.py` | OIDC signature/audience/expiry; event, workflow, ref and repo checks; strict manifest; run binding; replay; result size |
| Approvals | `gateway/tests/test_approval.py` | Environment claim required; fails closed on missing env or missing reviewers; rejection fails fast; run verification |
| Runner | `action/tests/test_runner.py` | Refuses gated tools without approval even if the gateway asks; credentials scrubbed; secrets redacted; result capped; https only |
| Policy file | `action/tests/test_policy.py` | Strict parsing; unknown keys and tool names are errors; `allow = []` publishes nothing |
| Full loop | `gateway/tests/test_e2e.py` | Real HTTP gateway + runner subprocess + FastMCP client, gated tool included |

---

## L3 Staging on real GitHub (before every release)

This is the most important missing level: it's the only one that checks GitHub's real behaviour, which every other level mocks.

### Setup (once)

A ready-made version of this setup on one Windows laptop (gateway + runner in WSL, Tailscale Funnel for HTTPS), with every command including the manual cases: [staging-laptop.md](staging-laptop.md).

1. **GitHub App**: create one with repository permissions **Actions: write**, **Environments: read**, **Metadata: read**, and no webhook. Note its App ID and download a private key.
2. **Gateway**: deploy the image built from `gateway/Dockerfile` (CI's `image` job builds and smoke-tests it) at an HTTPS URL, with a persistent volume on `/data` writable by UID 10001, and:

   | Env var | Value |
   |---|---|
   | `GATEWAY_PUBLIC_URL` | the HTTPS URL, e.g. `https://mcp-gw-staging.example.com` |
   | `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` | from step 1 |
   | `GATEWAY_ALLOWED_OWNERS` | your user or org, so nobody else can register on staging |
   | `GATEWAY_STATUS_POLL` | `15` |
   | `FORWARDED_ALLOW_IPS` | your load balancer's address, so audit logs get real client IPs |

   Leave `GATEWAY_OIDC_AUDIENCE` unset (it defaults to `GATEWAY_PUBLIC_URL`, which the action also uses) and never set `GATEWAY_DEV_MODE`. Audit events are on the gateway's stdout (`docker logs`): several manual cases read them.
3. **Staging repo** (public: required reviewers on a private repo need GitHub Enterprise), e.g. `victorperr/mcp-runner-staging`:
   - copy `staging/server.py`, `staging/mcp-runner.toml`, and `staging/workflow-mcp.yml` → `.github/workflows/mcp.yml`. Until the version under test is merged, change the action's `@main` to its branch;
   - run `uv init --bare && uv add "fastmcp>=2.12,<3" && uv lock`;
   - secrets `MCP_CLIENT_TOKEN` (32+ chars) and `STAGING_SECRET` (any 8+ char value);
   - variables `MCP_GATEWAY_URL` (exactly `GATEWAY_PUBLIC_URL`) and `MCP_RUNS_ON`;
   - environment `mcp-approval`: **you** as required reviewer, *Allow administrators to bypass* **off**, deployment branches limited to `main`;
   - **install the GitHub App before the first push**: registration is refused until it is installed;
   - register one ephemeral **Linux** self-hosted runner (S14/S15 need Linux) and check `cat /proc/sys/kernel/yama/ptrace_scope` prints 1 or more.
4. **This repo**: environment `staging` with secrets `STAGING_CLIENT_TOKEN`, `STAGING_REVIEWER_TOKEN` (a fine-grained token of the reviewer with Actions read/write and Deployments read/write on the staging repo) and variables `STAGING_MCP_URL`, `STAGING_REPO`.
5. Push the staging repo once, then run the preflight check. It reads the setup through the GitHub API (your `gh` login or `GITHUB_TOKEN`) and must show `0 failed`:
   ```
   STAGING_REPO=<owner>/<repo> STAGING_MCP_URL=https://<gateway>/s/<owner>/<repo>/mcp python staging/preflight.py
   ```

### Automated cases: `staging/test_staging.py`

Run with **Actions → Staging tests → Run workflow**, or locally with `python -m pytest staging -v -s` and the env vars set.

| ID | Case | Expected |
|---|---|---|
| S01 | `tools/list` | Exactly the 8 staging tools |
| S02 | `add(20, 22)` | `42` |
| S03 | 10 sequential calls | p95 ≤ 90 s (record p50/p95 in the release notes) |
| S04 | Gated tool, reviewer **approves** through the API when the "waiting for approval" link appears | Tool runs, result returned |
| S05 | Gated tool, reviewer **rejects** | Error `-32009` less than 60 s after the rejection |
| S06 | Tool returns the value of `STAGING_SECRET` | Client sees `value=***`, redaction count ≥ 1 |
| S07 | Tool lists its environment | No `ACTIONS_ID_TOKEN_REQUEST_*`, `ACTIONS_RUNTIME_TOKEN`, `MCP_CLIENT_TOKEN` |
| S08 | Tool checks `.git/config` | No auth header left by checkout |
| S09 | Invalid arguments | `-32003` in under 5 s, no run started |
| S10 | 50 KB result with a 20 KB limit | `-32013` |
| S11 | Wrong bearer token | HTTP `401` |
| S12 | Unknown tool | `-32001` |
| S13 | 5 slow calls at once, limit 3 | At least one `-32005`, the others succeed |
| S14 | Tool reads `/proc/<pid>/environ` of itself and every parent up to init (Linux) | No `ACTIONS_ID_TOKEN_REQUEST_*`, `ACTIONS_RUNTIME_TOKEN` or `MCP_CLIENT_TOKEN` anywhere |
| S15 | Tool opens its parent's (the supervisor's) `/proc/<pid>/mem` (Linux) | Permission denied |

### Manual cases (scripted steps, before every release)

| ID | Steps | Expected |
|---|---|---|
| M01 Workflow tampering | On `main`, delete the `environment:` line in `mcp.yml`, push, call `gated_action` | Run starts without asking for approval; gateway answers `403 requires approval`; client gets an error; **tool does not run**. Restore the line |
| M02 Compromised gateway (runner check) | Locally, patch `runner_api._authorize_call` to skip the environment check, point the staging repo's `MCP_GATEWAY_URL` at it, repeat M01 | Runner refuses with `-32012`; tool does not run |
| M03 Re-run | Re-run a finished `MCP call …` run from the Actions tab | Run fails with `409`; tool does not run again |
| M04 Cross-run claim | During a gated call waiting for approval, use a manifest run's OIDC token (debug step) to `GET /runner/calls/<id>` | `403 This run was not started for this call` |
| M05 Feature branch | Push the server to a `feature` branch with the workflow's `push` trigger widened | Manifest rejected `409`; live tools unchanged |
| M06 Second repo | Register a second repo; try its client token on the first repo's URL | `401` |
| M07 Token rotation | Change `MCP_CLIENT_TOKEN`, push | Old token `401`, new token works |
| M08 Missing environment | Delete `mcp-approval`, call `gated_action` | `-32006` at once, no run started |
| M09 No reviewers | Recreate `mcp-approval` without reviewers, call `gated_action` | `-32006` at once, no run started |
| M10 Runner offline | Stop the runner, call `add` | "Queued… Is a runner with the right labels online?" after 60 s, then `-32008` at the timeout |
| M11 App uninstalled | Uninstall the GitHub App, call `add` | `-32007` with "app is not installed" |
| M12 Approval after timeout | Set `approval_timeout_seconds = 60`, call `gated_action`, approve after 90 s | Client got `-32008`; the approved run fails with `409`; tool does not run |
| M13 Re-register from a call run | Add the temporary step below to the staging workflow, push, call `add` | The step prints `403`; `add` still works with the old client token. Remove the step |
| M14 Manual manifest upload | Actions → MCP server → Run workflow with `call_id` empty | Manifest job green (GitHub confirms it's an `MCP manifest` run); audit `manifest.registered` |
| M15 Another workflow | Copy `mcp.yml` to `.github/workflows/other.yml` (keep the push trigger), push | `other.yml`'s job fails with `409 … registered from .github/workflows/mcp.yml`. Delete the file |
| M16 Admin bypass | Turn on *Allow administrators to bypass* on `mcp-approval`, wait 60 s, call `gated_action` | `-32006` mentioning the bypass, no run started. Turn it off again |
| M17 Gateway restart mid-call | Call `sleep(120)`, restart the gateway after 20 s, then make 4 quick `add` calls | First call errors (no hang); audit `calls.recovered`; the `add` calls aren't refused with `-32005` |
| M18 Owner allowlist | Set `GATEWAY_ALLOWED_OWNERS` to another owner, restart, push the staging repo | Manifest job fails with `403 … does not accept servers from this owner`. Restore |
| M19 App not installed | Create a second private repo from the staging files **without** installing the App, push | Manifest job fails with `403 Install the MCP GitHub Runner app …` |

M13 step (temporary, after the action step in the staging repo's `mcp.yml`). It does from a tool-call job what an attacker with that job's OIDC token would try: replace the client token. The gateway must refuse it, because GitHub reports the run as `MCP call …`, not `MCP manifest`:

```yaml
      - name: "M13 (temporary): try to re-register from a tool-call run"
        if: inputs.call_id != ''
        env:
          GW: ${{ vars.MCP_GATEWAY_URL }}
        run: |
          aud=$(jq -rn --arg a "$GW" '$a | @uri')   # the audience is the gateway URL, URL-encoded
          token=$(curl -sS -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
            "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=$aud" | jq -r .value)
          zeros=$(printf '0%.0s' $(seq 64))
          curl -sS -o /dev/null -w 'M13: %{http_code}\n' -X PUT "$GW/runner/manifest" \
            -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
            -d "{\"tools\": [], \"client_token_sha256\": \"$zeros\"}"
```

### Results log

One row per staging session. Latency is printed by S03 (`gh run view <run-id> --log | grep latency`).

| Date | Version tested | Automated S01–S15 | Manual cases | S03 latency (p50 / p95) | Notes |
|---|---|---|---|---|---|
| 2026-11-06 | `main` @ `5a03739` | ✅ all pass ([run](https://github.com/victorperr/mcp-github-runner/actions/runs/42220690955)) | not run | to record | Laptop setup, one runner: S13's calls ran one after another |

### Client compatibility matrix

For each client: connect, list tools, call `add`, call `sleep(45)`, call `gated_action` with a 2-minute approval delay. Record whether progress messages are shown and after how long the client gives up.

| Client | Connect | Call | Shows progress | Survives 45 s | Survives 2-min approval |
|---|---|---|---|---|---|
| MCP Inspector | | | | | |
| Claude Desktop (via `mcp-remote`) | | | | | |
| Claude Code (`claude mcp add --transport http … --header`) | | | | | |
| Cursor | | | | | |
| VS Code (Copilot agent mode) | | | | | |

Pass criteria: at least 3 clients complete all five steps.

---

## L4 Security testing (before first sale, then yearly)

| Test | How | Pass criteria |
|---|---|---|
| API fuzzing of `/runner/*` | [schemathesis](https://schemathesis.readthedocs.io) against a **dev-mode** gateway's OpenAPI (`/openapi.json` is only served in dev mode) with a valid dev OIDC token | No 5xx; no 2xx on requests that break the schema |
| MCP endpoint fuzzing | Malformed JSON-RPC, huge bodies, wrong content types on `/s/{owner}/{repo}/mcp` | No 5xx, no crash; 413/400 as appropriate |
| Token brute force | 10,000 wrong tokens from one IP | All 401 and audited (rate limiting by IP is a known gap: add it, or put it in front of a WAF) |
| Container scan | `trivy image <gateway image>` | No critical or high CVEs that can't be explained |
| External pentest | A third party with the threat model in `docs/security.md` | No high or critical findings left open |

## L5 Load and reliability (before first sale)

| Test | How | Target |
|---|---|---|
| Waiting calls | Gateway with a dispatcher that answers after 60 s; 200 concurrent clients | p99 overhead < 500 ms, memory < 512 MB, no errors |
| Policy throughput | 1,000 calls/s denied by policy (invalid args) | No impact on in-flight calls |
| Gateway restart mid-call | Restart while 10 calls are waiting | Clients get an error (no hang); runs that finish later get `409` and do nothing; no stuck `pending` rows after the timeout |
| GitHub API down | Block `api.github.com` from the gateway | Dispatch fails with `-32007`; claims fail closed (`403`); recovers by itself |
| SQLite on a volume | Kill the container, restart | Registered servers and tokens survive |

---

## Release gates: when you can sell

A version can be sold only when **every** gate is green.

| Gate | Requirement | Status |
|---|---|---|
| G0 | L0–L2 green in CI on `main` | ✅ |
| G1 | No ignored vulnerabilities: **upgrade to fastmcp ≥ 3.2** | ❌ |
| G2 | L3 staging: S01–S15 and M01–M19 pass; latency recorded | 🟡 S01–S15 pass (2026-11-06); manual cases and latency open |
| G3 | Client matrix: 3+ clients pass all steps | ❌ |
| G4 | L4: no open high/critical findings; IP rate limiting on the MCP endpoint | ❌ |
| G5 | L5 targets met; gateway runs on a highly available setup (Postgres + Redis instead of SQLite + memory) | ❌ |
| G6 | Product basics: hosted gateway on a real domain with monitoring and backups, published GitHub App, sign-up and billing, Terms of Service, Privacy Policy and DPA, support channel | ❌ |
| G7 | L6 pilot: 2–3 design partners use it for 2+ weeks and at least one would pay | ❌ |

Until G2 passes, describe the product as a **preview**. Until G7, don't take payment. A paid design-partner pilot with clear "beta" terms is the usual middle step.

### What remains, in order

1. **G2:** run M01–M19 with [staging-laptop.md](staging-laptop.md#manual-cases-m01m19); record latency and results in the [results log](#results-log).
2. **G1:** upgrade to fastmcp ≥ 3.2 on a branch, rerun L0–L3, then remove the ignored vulnerabilities from `ci.yml` and this file.
3. **Product decision, approvals on private repos:** GitHub only allows required reviewers on private repos with GitHub Enterprise (seen in staging, 2026-11-06). Customers on other plans can't use gated tools from a private repo, and [self-hosted.md](self-hosted.md#4-hardening-checklist) rightly forbids self-hosted runners on public repos. Decide before selling: target Enterprise customers, implement approval in the gateway itself, or document gated tools as Enterprise-only.
4. **G3:** client matrix with at least 3 clients.
5. **G4 and G5:** fuzzing, IP rate limiting, container scan, external pentest; load tests and the move to Postgres + Redis.
6. **G6:** hosted gateway on a real domain with monitoring and backups, published GitHub App, sign-up and billing, legal documents, support channel.
7. **G7:** 2–3 design partners for 2+ weeks.
