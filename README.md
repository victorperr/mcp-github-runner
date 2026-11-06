# MCP GitHub Runner

Write a FastMCP server, push it to a GitHub repo, and Claude Desktop, Cursor or any MCP client can call its tools. Each call runs as a GitHub Actions job on **your self-hosted runners**, inside your network:

- **Reach private services.** Tools can call internal APIs and databases. The runner only makes outbound connections, so no inbound port is opened.
- **Approval for risky tools.** Tag a tool `requires-approval`, and each call waits for a reviewer to approve it in GitHub (GitHub Environments with required reviewers).
- **Secrets stay in GitHub.** Credentials live in repo or environment secrets. The gateway never sees them.
- **Audit trail built in.** Every call is a workflow run with its log, and access follows repo permissions.

The idea comes from [GitHub Agentic Workflows](https://github.com/github/gh-aw), which runs MCP servers inside Actions jobs for an agent in the same job. MCP GitHub Runner opens that up to **external** clients through a small relay gateway.

```
MCP client ──HTTP──▶ Gateway ──workflow_dispatch(call_id, approval)──▶ Job on your self-hosted runner
   ▲                   │  ▲                                              │ [reviewer approves, if tagged]
   │                   │  │                                              │ fetch args (OIDC)
   │                   │  └──────────── result (OIDC) ───────────────────┤ run tool → internal API / DB
   └──── result ───────┘                                                  ▼
```

## Layout

| Folder | What it is |
|---|---|
| [action/](action/) | Composite GitHub Action plus the `mcp_runner` Python package that runs on the runner |
| [gateway/](gateway/) | Hosted relay: FastAPI app with the public MCP endpoint and the runner API |
| [template/](template/) | Starter repo: `server.py` (with private-service and approval examples), `pyproject.toml`, `.github/workflows/mcp.yml` |
| [docs/self-hosted.md](docs/self-hosted.md) | **Runner setup, reaching private services, approvals, hardening, troubleshooting** |
| [docs/security.md](docs/security.md) | **Threat model, controls at each boundary, denial codes, remaining risks** |
| [docs/testing.md](docs/testing.md) | **Test levels, staging and manual test cases, release gates** |
| [staging/](staging/) | Staging server and test suite that run against real GitHub |

## Quickstart

1. Create a **private** repo from [template/](template/) and edit `server.py`.
2. Register a self-hosted runner in a runner group limited to that repo, and set the repo variable `MCP_RUNS_ON` to its labels, e.g. `["self-hosted", "mcp"]`. See [docs/self-hosted.md §1](docs/self-hosted.md#1-set-up-the-runner).
3. Install the **MCP GitHub Runner** GitHub App on the repo, and set the repo variable `MCP_GATEWAY_URL` to the gateway's `https://` address.
4. Add a repo secret `MCP_CLIENT_TOKEN` set to a long random string (`python -c "import secrets; print(secrets.token_urlsafe(32))"`), plus any secrets your tools need.
5. If any tool is tagged `requires-approval`, create the `mcp-approval` environment with required reviewers. See [§3](docs/self-hosted.md#3-require-approval-for-risky-tools).
6. Push to `main`. The job summary shows your MCP URL (`https://<gateway>/s/<owner>/<repo>/mcp`) and which tools need approval.
7. Add it to your client with header `Authorization: Bearer <MCP_CLIENT_TOKEN>`. For Claude Desktop:
   ```json
   {
     "mcpServers": {
       "my-repo": {
         "command": "npx",
         "args": ["mcp-remote", "https://<gateway>/s/<owner>/<repo>/mcp",
                  "--header", "Authorization: Bearer ${MCP_TOKEN}"],
         "env": { "MCP_TOKEN": "<MCP_CLIENT_TOKEN>" }
       }
     }
   }
   ```

## Development

```powershell
python -m venv .venv
.venv\Scripts\pip install "fastmcp>=2.12,<3" httpx respx pytest pytest-asyncio fastapi "uvicorn[standard]" "pyjwt[crypto]"

cd action;  ..\.venv\Scripts\python -m pytest     # runner tests
cd gateway; ..\.venv\Scripts\python -m pytest     # gateway tests, including a full local loop
```

Or install everything at once with `pip install -r requirements-dev.txt`. CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs lint (ruff, zizmor), both suites with coverage gates, and a dependency audit. The full test plan is in [docs/testing.md](docs/testing.md).

### Run the full loop locally (no GitHub)

Dev mode signs fake OIDC tokens with a local key, and the `local` dispatcher runs `mcp_runner call` as a subprocess instead of starting a workflow. It treats calls that need approval as already approved. The gateway refuses to start in dev mode unless `GATEWAY_PUBLIC_URL` is a localhost address.

```powershell
cd gateway
$env:GATEWAY_DEV_MODE="1"; $env:GATEWAY_DISPATCHER="local"; $env:GATEWAY_LOCAL_SERVER_DIR="..\template"
..\.venv\Scripts\python -m uvicorn --factory app.main:create_app --port 8000

# second terminal, same env vars:
..\.venv\Scripts\python -m app.dev register --dir ..\template --repo me/demo   # client token: dev-token-0123456789abcdefghijklm
npx @modelcontextprotocol/inspector   # connect to http://localhost:8000/s/me/demo/mcp, header Authorization: Bearer dev-token-0123456789abcdefghijklm
```

### Deploy the gateway

1. Create a GitHub App with repository permissions **Actions: write**, **Environments: read** and **Metadata: read**. No webhook is needed.
2. Deploy `gateway/Dockerfile` (Fly.io, Cloud Run, Render…) with a persistent volume on `/data` and these env vars. The container runs as UID 10001: a new Docker volume gets the right owner automatically, but a host directory must be made writable by that UID (`chown 10001 <dir>`). Env vars:
   `GATEWAY_PUBLIC_URL`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` (or `GITHUB_APP_PRIVATE_KEY_PATH`).
   Optional caps (repos can lower them in `mcp-runner.toml`, never raise them): `GATEWAY_CALL_TIMEOUT` (300), `GATEWAY_APPROVAL_TIMEOUT` (900), `GATEWAY_MAX_PENDING_CALLS` (5 per repo), `GATEWAY_MAX_CALLS_PER_HOUR` (1000), `GATEWAY_MAX_ARGUMENT_BYTES` (256 KB), `GATEWAY_MAX_RESULT_BYTES` (4 MB). Also `GATEWAY_STATUS_POLL` (15), `GATEWAY_OIDC_AUDIENCE` (defaults to `GATEWAY_PUBLIC_URL`; the action uses the gateway URL too, so each deployment has its own audience), `GATEWAY_ALLOWED_OWNERS` (comma-separated users/orgs allowed to register; empty means any repo that installed the App), and `GATEWAY_ARGUMENT_CHECK_TIMEOUT` (2 s) / `GATEWAY_ARGUMENT_CHECK_WORKERS` (2) for the isolated argument checks.
   Audit events are printed on stdout as JSON lines (the `mcp_github_runner.audit` logger); collect them with your platform's logs, or set `GATEWAY_AUDIT_LOG=off` and route that logger yourself.
3. Tag a release (`git tag v1.0.0 && git tag -f v1 && git push --tags`). Repos then use `victorperr/mcp-github-runner/action@v1`, or better, its commit SHA. Each repo sets the repo variable `MCP_GATEWAY_URL` to the gateway's `https://` address. The action has no default address on purpose.

## Security

The full threat model, every control and the denial codes are in **[docs/security.md](docs/security.md)**. In short:

- **Ordered policy checks before any run starts** (tool → argument size → schema → hourly rate → concurrency), configured per repo in [`mcp-runner.toml`](template/mcp-runner.toml) and clamped to the gateway's caps. Unknown keys or tool names are errors, never ignored.
- **Approval is enforced twice.** The gateway requires the approval-environment claim in the runner's OIDC token, and the runner re-checks it from its own token, so even a compromised gateway can't run a gated tool without a reviewer.
- **Each call is bound to its run.** GitHub must confirm the claiming run was started for that call.
- **Tool code never sees runner credentials.** OIDC and runtime tokens and the client token are removed before user code loads, and checkout leaves no git credentials behind.
- **Secret values are redacted from results**, and results are size-capped.
- **Supply chain**: pinned action SHAs, `uv sync --locked`, `python -P`.
- **Audit log**: one JSON line per security event, never containing arguments, results or tokens.

The checklist for runners, branches and credentials is in [docs/self-hosted.md §4](docs/self-hosted.md#4-hardening-checklist).

## Limits

- **Latency**: each call is a job start, usually 10–30 s on an idle self-hosted runner, plus approval time for gated tools. Best for tools where that's acceptable: lookups, ops actions, runbooks, long-running jobs. The gateway sends progress messages (queued / waiting for approval with link / running) so clients keep waiting.
- **Client timeouts**: some clients drop calls that take longer than a few minutes. Gated calls can wait up to 15 min.
- **Scaling**: waiting calls are in-memory futures, so run a single gateway instance. Moving to Redis pub/sub is needed for more.
- **Client auth**: v1 uses a bearer token. Next step: GitHub OAuth following the MCP authorization spec, so Claude.ai custom connectors work without `mcp-remote`.
- **GitHub-hosted runners** still work (set `MCP_RUNS_ON` to `"ubuntu-latest"`), but GitHub's terms discourage using Actions as a serverless backend, so the product targets self-hosted runners.

## Status

Preview. The automated staging suite (S01–S15) passes on real GitHub infrastructure (2026-11-06); the manual staging cases and the other release gates are still open. See [docs/testing.md](docs/testing.md#release-gates-when-you-can-sell).

## License

- [action/](action/), [template/](template/), [staging/](staging/), docs and everything else: **Apache-2.0**, see [LICENSE](LICENSE).
- [gateway/](gateway/): **Functional Source License 1.1 (FSL-1.1-ALv2)**, see [gateway/LICENSE.md](gateway/LICENSE.md). You may use, modify and self-host the gateway for any purpose except offering a competing commercial service. Each release becomes Apache-2.0 two years after it is published.

The design was inspired by [GitHub Agentic Workflows (gh-aw)](https://github.com/github/gh-aw).
