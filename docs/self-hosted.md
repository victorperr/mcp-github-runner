# Running MCP tools on self-hosted runners

MCP GitHub Runner runs each tool call as a GitHub Actions job on **your own runners**. That gives AI agents access to internal systems (databases, internal APIs, servers) without opening any inbound port. The runner only makes outbound connections, and access, secrets and audit logs stay in GitHub.

This guide covers:

1. [Setting up the runner](#1-set-up-the-runner)
2. [Reaching private services](#2-reach-private-services)
3. [Requiring approval for risky tools](#3-require-approval-for-risky-tools)
4. [Hardening checklist](#4-hardening-checklist)
5. [Troubleshooting](#5-troubleshooting)

---

## 1. Set up the runner

### Where to put it

Put the runner on a machine or Kubernetes cluster **inside the network segment your tools need**, and nowhere broader. If the tools only need the internal API and one read replica, the runner's subnet should only reach those two.

### Register it

Register the runner at the **organization** level and put it in a **runner group restricted to your MCP repo** (Organization settings → Actions → Runner groups → *Selected repositories*). Other repos then can't schedule jobs on a machine that can reach your internal network.

Give it a dedicated label, for example `mcp`:

```bash
./config.sh --url https://github.com/<org> --token <token> --labels mcp --runnergroup mcp-runners --ephemeral
```

**Use ephemeral runners.** `--ephemeral` makes the runner take one job and then de-register, so nothing carries over from one tool call to the next. On Kubernetes, [Actions Runner Controller](https://github.com/actions/actions-runner-controller) gives you ephemeral runners that scale up when calls come in and back down when idle.

### Point the workflow at it

The template workflow reads its runner labels from the repo variable `MCP_RUNS_ON` (a JSON array) and defaults to `["self-hosted"]`:

```yaml
runs-on: ${{ fromJSON(vars.MCP_RUNS_ON || '["self-hosted"]') }}
```

Set it under Settings → Secrets and variables → Actions → **Variables**, for example `["self-hosted", "linux", "mcp"]`.

### Outbound access the runner needs

The runner never needs inbound access. Outbound HTTPS (443) must reach:

| Destination | Why |
|---|---|
| `github.com`, `api.github.com`, `*.actions.githubusercontent.com`, `codeload.github.com` | Normal GitHub Actions runner traffic. See GitHub's [self-hosted runner network docs](https://docs.github.com/actions/hosting-your-own-runners/managing-self-hosted-runners/about-self-hosted-runners#communication-between-self-hosted-runners-and-github) for the full list |
| `objects.githubusercontent.com`, `github-releases.githubusercontent.com` | Downloading `uv` (via `astral-sh/setup-uv`) |
| `pypi.org`, `files.pythonhosted.org` | Installing your server's dependencies. You can use an internal mirror instead, see below |
| Your gateway URL | Fetching call arguments and posting results |
| Your private services | Whatever your tools call |

**Python packages through an internal mirror:** set `UV_INDEX_URL` (for example to your Artifactory or Nexus PyPI proxy) as a repo variable and pass it in the workflow's `env:`. Then PyPI doesn't need to be reachable.

**Speed:** a persistent (non-ephemeral) runner keeps `uv`'s cache between jobs, which saves a few seconds per call. Ephemeral runners are safer. If you go ephemeral, pre-install `uv` in the runner image and mount a shared cache volume.

---

## 2. Reach private services

Tools are ordinary Python. Whatever the runner's network can reach, a tool can call.

### Pass connection details as secrets

Store URLs and credentials as GitHub secrets and pass them to the job in [mcp.yml](../template/.github/workflows/mcp.yml):

```yaml
env:
  INTERNAL_API_URL: ${{ secrets.INTERNAL_API_URL }}
  INTERNAL_API_TOKEN: ${{ secrets.INTERNAL_API_TOKEN }}
  DATABASE_URL: ${{ secrets.READONLY_DATABASE_URL }}
```

Secrets never pass through the gateway. Only the runner sees them, and GitHub masks them in logs.

### Example: internal HTTP API

From [template/server.py](../template/server.py):

```python
# An allowlist, not a blocklist: a check like `".." in path` is bypassed by "%2e%2e".
ALLOWED_PATH = re.compile(r"/(status|health)|/api/v1/(orders|customers)(/[A-Za-z0-9_-]+)?")

@mcp.tool(annotations={"readOnlyHint": True})
def internal_api_get(path: str) -> dict:
    """GET a path on the internal API (only reachable from inside the network)."""
    base = os.environ["INTERNAL_API_URL"]           # e.g. http://billing.internal:8080
    if not ALLOWED_PATH.fullmatch(path):            # keep the agent on these endpoints
        raise ValueError("path is not allowed")
    resp = httpx.get(base + path, headers={"Authorization": f"Bearer {os.environ['INTERNAL_API_TOKEN']}"})
    return {"status": resp.status_code, "body": resp.text[:10_000]}
```

### Example: read-only database query

```python
import psycopg  # add "psycopg[binary]" to pyproject.toml

@mcp.tool(annotations={"readOnlyHint": True})
def orders_for_customer(customer_id: int) -> list[dict]:
    """Last 20 orders for a customer."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.read_only = True
        rows = conn.execute(
            "SELECT id, total, created_at FROM orders WHERE customer_id = %s ORDER BY created_at DESC LIMIT 20",
            (customer_id,),
        ).fetchall()
    return [{"id": r[0], "total": float(r[1]), "created_at": r[2].isoformat()} for r in rows]
```

### Rules of thumb

- **Narrow tools beat generic ones.** `orders_for_customer(id)` is safer than `run_sql(query)`. The agent can only do what the tool's code allows.
- **Use the least-privileged credential that works.** Read-only database users, scoped API tokens, a dedicated service account per MCP repo.
- **Validate inputs** as if they came from the internet. Arguments are written by an AI model, which may in turn be reading untrusted content.
- **Return only what's needed.** Results go back through the gateway to the client, so trim large or sensitive fields.
- **Internal DNS works.** The runner uses its host's resolver, so names like `billing.internal` resolve as they would for any other service on that network.

---

## 3. Require approval for risky tools

Tag a tool with `requires-approval`, and every call to it waits until a reviewer approves the job in GitHub:

```python
@mcp.tool(tags={"requires-approval"}, annotations={"destructiveHint": True})
def restart_service(name: str) -> str:
    """Restart an internal service. Needs approval in GitHub before it runs."""
    subprocess.run(["systemctl", "restart", name], check=True)
    return f"restarted {name}"
```

### How it works

1. When the server's tool list is uploaded, the gateway records which tools are tagged.
2. A call to a tagged tool starts the workflow with `approval: true`. The job then uses the `mcp-approval` environment, so GitHub holds it until a required reviewer approves.
3. Meanwhile the MCP client sees progress messages such as *"Waiting for approval in the 'mcp-approval' environment: https://github.com/…/actions/runs/…"*, with the link reviewers need.
4. After approval, GitHub adds `environment: mcp-approval` to the job's OIDC token. **The gateway checks that claim** before it hands over the call. A job that skipped the environment (for example because someone edited the workflow) gets `403 requires approval`.
5. Before dispatching, the gateway also checks that the environment exists, has **required reviewers**, and doesn't let administrators bypass them. If any of that is missing, the call fails right away ("fail closed"), instead of running without approval.
6. If a reviewer rejects the job, the run ends. The gateway notices and tells the client "The run ended without returning a result. Was the approval rejected?" instead of waiting for the timeout.

### Set up the environment

1. Repo → Settings → **Environments** → New environment → name it `mcp-approval`.
2. Turn on **Required reviewers** and add the people or teams who may approve. Optionally turn on **Prevent self-review**.
3. Turn **off** *Allow administrators to bypass configured protection rules* (it is on by default). The gateway refuses gated calls while it is on, because an admin could otherwise start the job without any review.
4. Under **Deployment branches and tags**, choose *Selected branches* → `main`, so only the registered branch can use it.
5. Optional: put the credentials that only risky tools need (for example `DEPLOY_TOKEN`) in the **environment's** secrets instead of the repo's. GitHub only exposes environment secrets to jobs that passed approval, so even a bug in a non-gated tool can't read them.

Required reviewers on **private** repos need a paid GitHub plan (Team or Enterprise for organizations). Check GitHub's current plan docs.

**Another environment name:** set `approval-environment:` on the action and change the `environment:` line in `mcp.yml` to match. Both must use the same name.

### Timeouts

Gated calls wait up to `GATEWAY_APPROVAL_TIMEOUT` (default 900 s = 15 min). Other calls wait up to `GATEWAY_CALL_TIMEOUT` (default 300 s). If approval comes after the gateway has given up, the job starts but gets `409` for the call and does nothing, so a late approval never runs a stale request.

Some MCP clients stop waiting long before 15 minutes. The gateway's progress notifications keep most clients waiting, but if yours gives up, the agent can call the tool again after approval is set up.

---

## 4. Hardening checklist

- [ ] **Private repo.** Never attach self-hosted runners that reach internal systems to a public repo: a fork's pull request could run code on them.
- [ ] **Runner group restricted to this repo only.**
- [ ] **Ephemeral runners**, or a dedicated VM that is reset regularly.
- [ ] **Branch protection on `main`** with required reviews. A push to `main` changes which tools exist, so treat `server.py` like production code. Add `CODEOWNERS` entries for `server.py`, `mcp-runner.toml` and `.github/workflows/`.
- [ ] **A [`mcp-runner.toml`](../template/mcp-runner.toml) policy** with an explicit `allow` list and limits that fit your use (calls per hour, argument and result size, timeouts).
- [ ] **A committed `uv.lock`**, so the action installs exact versions (`uv sync --locked`).
- [ ] **Secret-looking names for secret env vars** (`*_TOKEN`, `*_PASSWORD`, `*_KEY`…), or list them in the action's `redact-env`, so their values are redacted from results.
- [ ] **`mcp-approval` environment** with required reviewers, admin bypass turned off, and a `main`-only branch rule, if you have any gated tools.
- [ ] **Separate credentials per MCP repo**, all least-privileged. Destructive credentials only in environment secrets.
- [ ] **Network egress limited** to GitHub, the gateway, your package index and the services the tools need.
- [ ] **A long, random `MCP_CLIENT_TOKEN`**, rotated when someone with access leaves (update the secret, then push or re-run the manifest job).
- [ ] **Read the run logs** now and then. Every call is a workflow run titled `MCP call <id>` with the tool name. Arguments are not logged.

---

## 5. Troubleshooting

| What the client sees | Cause and fix |
|---|---|
| *Queued, waiting for a self-hosted runner… Is a runner with the right labels online?* | No idle runner matches `runs-on`. Check that the runner is online, `MCP_RUNS_ON` matches its labels, and the runner group includes this repo. |
| *This tool needs approval, but … has no 'mcp-approval' environment* | Create the environment (section 3). |
| *The 'mcp-approval' environment … has no required reviewers* | Add reviewers to the environment. The gateway refuses to run gated tools without them. |
| *The 'mcp-approval' environment … lets administrators bypass its reviewers* | In the environment's settings, turn off *Allow administrators to bypass configured protection rules*. |
| *The run ended (failure) without returning a result* | Open the linked run. Usually a dependency install failed or the server didn't import. For gated tools it can also mean the approval was rejected. |
| *Tool '…' requires approval: the job must run in the 'mcp-approval' environment* (in the run log) | The `environment:` line in `mcp.yml` was changed or removed, or the environment name doesn't match `approval-environment`. |
| *The MCP GitHub Runner app is not installed on …* | Install the GitHub App on the repo. It needs **Actions: write**, **Environments: read** and **Metadata: read**. |
| `401` from the gateway | Wrong `Authorization: Bearer` token in the client, or `MCP_CLIENT_TOKEN` was changed but the manifest wasn't uploaded again since. |
