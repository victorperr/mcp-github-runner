# Security model

MCP GitHub Runner lets AI agents call tools that run inside a company's network. This page covers who is trusted with what, the checks at each boundary, and the risks that remain.

Several ideas come from [GitHub Agentic Workflows](https://github.com/github/gh-aw) (`pkg/workflow/` in that repo):

| gh-aw idea | Where it lives here |
|---|---|
| Access checks run in a fixed order, the first failure picks a numbered denial code, and tests prove "no spurious allow" (`mcp_access.go`) | `gateway/app/policy.py`, `gateway/tests/test_policy.py` |
| Config with unknown fields is rejected, never silently ignored (`DisallowUnknownFields` in `enclave.go`) | Strict manifest model in `gateway/app/runner_api.py`, strict `mcp-runner.toml` parsing in `action/mcp_runner/policy.py` |
| Hard limits on invocations, output bytes and execution time (enclave quotas) | `[limits]` in `mcp-runner.toml`, clamped to the gateway's ceilings |
| Secure by default: a partial or empty config can never quietly switch protection off (`applySandboxDefaults` in `sandbox.go`) | Missing limits use safe defaults; an empty `allow = []` publishes nothing; a missing or unprotected approval environment blocks the call |
| Host-only credentials never reach the agent (control endpoint kept away from the executor in `enclave.go`) | The runner removes OIDC request tokens, runtime tokens and the client token from the environment before user code loads |
| Strict format checks on identifiers (repo slug regexes in `enclave.go`) | Call IDs, tool names and environment names are format-checked in both the gateway and the runner |
| Action pinning | Third-party actions pinned to commit SHAs in `action.yml` and the template |
| Audit labels | One JSON audit line per security event (`gateway/app/audit.py`) |

---

## Actors and what they're trusted with

```
 MCP client / agent ──(1)──▶ Gateway ──(2)──▶ GitHub ──(3)──▶ Runner job ──(4)──▶ Tool code ──▶ Private services
                   ◀───────────────────────────────(5) result ──────────┘
```

| Actor | Trusted to | Not trusted to |
|---|---|---|
| **MCP client / agent** | Pick a tool and arguments within the server's policy | Anything else. Treat it as possibly prompt-injected |
| **Gateway** (us) | Relay calls, enforce policy, start runs | Decide on its own that a gated tool may run (the runner checks again), see tool secrets |
| **GitHub** | Identity (OIDC), approvals (Environments), run scheduling, secrets | n/a: it is the root of trust |
| **Runner job** | Run the repo's code with the repo's secrets | Trust the gateway's word on approval |
| **Tool code** | Whatever the repo owner wrote | Access runner or gateway credentials |
| **Repo writers** | Change tools, policy and the workflow (via `main`) | n/a: this is the owner's server. Protect `main` |

---

## Controls at each boundary

### (1) Client → gateway

| Control | Where |
|---|---|
| Bearer token per server, stored only as SHA-256 and compared in constant time | `mcp_endpoint._token_ok` |
| Unknown repo and wrong token give the same `401`, so you can't probe which repos are registered | `mcp_endpoint.TenantMCPApp` |
| Client token must be at least 32 characters (checked on the runner, which is the only place that sees it) | `mcp_runner/security.check_client_token` |
| Policy checks before anything is dispatched, in order: **tool → argument size → argument schema → hourly rate → concurrency** | `gateway/app/policy.evaluate` |
| Tools stay isolated between repos (no shared schema cache across tenants) | `mcp_endpoint.build_mcp_server` |
| Failed authentication is audited with the client IP | `client.auth_failed` audit event |

### (2) Gateway → GitHub

| Control | Where |
|---|---|
| GitHub App installation token scoped to the one repo | `GitHubAppDispatcher._installation_token` |
| Only the call ID and an `approval` flag go into workflow inputs; arguments never do | `GitHubAppDispatcher.dispatch` |
| **Fail closed on approval:** a gated tool is never dispatched unless the environment exists and has required reviewers | `GitHubAppDispatcher._check_approval_environment` |

### (3) Runner → gateway

| Control | Where |
|---|---|
| OIDC token verified: GitHub issuer, signature (JWKS), audience, expiry | `gateway/app/oidc.py` |
| Manifest only from `push`/`workflow_dispatch`, only from the branch that registered first | `runner_api.put_manifest` |
| Manifest is strict: unknown fields, bad tool names, duplicates, non-positive limits → `422` | `runner_api.Manifest` |
| A call can only be read by a `workflow_dispatch` run of the registered workflow, on the registered branch, in the same repo (by `repository_id`, so renames are safe) | `runner_api._authorize_call` |
| **Call bound to its run:** GitHub must confirm the claiming run was started for this call (run title, event, workflow path, repo). A leaked OIDC token from another run can't claim it. Fails closed if GitHub can't be asked | `GitHubAppDispatcher.verify_run` |
| **Gated tools need the `environment` claim**, which GitHub adds only after a reviewer approves | `runner_api._authorize_call` |
| Each call is claimed once. Only the claiming run can post the result, and a late run after a timeout gets `409` | `store.claim_call`, `post_result` |
| Arguments deleted from the gateway once read | `store.clear_arguments` |
| Result size capped before parsing; result must be a valid `CallToolResult` with no extra fields | `runner_api.post_result` |
| Runner refuses a gateway URL that isn't `https://` (except localhost) | `mcp_runner/gateway_client.py` |

### (4) Runner → tool code

| Control | Where |
|---|---|
| **OIDC request token, Actions runtime tokens and `MCP_CLIENT_TOKEN` are removed from the environment before user code is imported.** Tool code and anything it spawns can't impersonate the runner | `mcp_runner/security.OIDCSource`, `scrub_environment` |
| `MCP_CLIENT_TOKEN` isn't even passed to tool-call jobs, only to manifest jobs | template `mcp.yml` |
| `actions/checkout` with `persist-credentials: false`, so `GITHUB_TOKEN` isn't left in `.git/config` | template `mcp.yml` |
| **The runner re-checks everything itself, without trusting the gateway:** the tool must be published by this repo's code and policy, and a gated tool needs its own OIDC token to show the approval environment. This holds even if the gateway is compromised | `mcp_runner/__main__._execute` |
| `python -P` so files in the repo can't shadow the runner's own modules | `action.yml` |
| Dependencies installed from `uv.lock` (`--locked`), with a warning if there is no lockfile | `action.yml` |

### (5) Result → client

| Control | Where |
|---|---|
| **Secret redaction:** values of env vars named like `*TOKEN*`, `*PASSWORD*`, `*KEY*`, `*SECRET*`, `*DSN*`… plus any listed in `redact-env` are replaced with `***` anywhere in the result | `mcp_runner/security.Redactor` |
| Result size limit, enforced on the runner and again on the gateway | `max_result_bytes` |
| Arguments and results are never logged, by the runner or the gateway | `audit.py`, `__main__.py` |

---

## Denial codes

Every refusal comes back as an MCP tool error with a code in `_meta["mcp-github-runner/denial"]`, so clients and logs can tell refusals apart.

| Code | Name | By | Meaning |
|---|---|---|---|
| -32001 | `tool_not_found` | gateway | Tool not published (doesn't exist or hidden by `tools.allow`) |
| -32002 | `arguments_too_large` | gateway | Arguments over `max_argument_bytes` |
| -32003 | `arguments_invalid` | gateway | Arguments don't match the tool's `inputSchema`, or the schema is broken |
| -32004 | `rate_limited` | gateway | `max_calls_per_hour` reached |
| -32005 | `too_many_concurrent` | gateway | `max_concurrent_calls` in flight |
| -32006 | `approval_unavailable` | gateway | Gated tool, but the environment is missing or has no required reviewers |
| -32007 | `dispatch_failed` | gateway | GitHub refused to start the run (app not installed, workflow missing…) |
| -32008 | `timeout` | gateway | No result in time (includes approval wait) |
| -32009 | `run_ended_without_result` | gateway | Run finished or was rejected without posting a result |
| -32011 | tool not allowed | runner | Runner refused a tool its own code/policy doesn't publish |
| -32012 | approval missing | runner | Runner refused a gated tool: its job didn't pass the approval environment |
| -32013 | result too large | runner | Result over the limit after redaction |

## Audit events

`gateway/app/audit.py` writes JSON lines to the `mcp_github_runner.audit` logger: `manifest.registered`, `manifest.rejected`, `call.created`, `call.denied`, `call.dispatched`, `call.dispatch_failed`, `call.claimed`, `call.claim_rejected`, `call.completed`, `call.timeout`, `call.run_ended`, `runner.auth_failed`, `client.auth_failed`. They contain IDs, tool names, codes and reasons, never arguments, results or tokens. GitHub's own run logs and the Environment's approval history complete the trail.

---

## Remaining risks

These aren't covered, or only partly:

- **A compromised gateway** can read arguments and results as they pass through, give clients fake results, and start *ungated* tools. It can't run gated tools without approval, because the runner checks that itself, and it never sees tool secrets. Run the gateway as sensitive infrastructure. End-to-end encryption between client and runner would close this but isn't built.
- **Prompt injection through tool results** (for example an internal page containing instructions for the agent) is the client's problem to handle. Keep tools narrow, and gate anything with side effects.
- **No network egress firewall on the runner.** gh-aw puts its agent behind a firewall (AWF) with a domain allowlist. Here, tool code can reach anything the runner's network can. Limit egress with network policy or a proxy (see [self-hosted.md §1](self-hosted.md#outbound-access-the-runner-needs)).
- **No process sandbox.** Tool code runs as the runner user, and redaction only catches values it knows about. Use ephemeral runners and least-privileged credentials.
- **Repo writers are fully trusted.** Anyone who can push to `main` can change tools and policy. Use branch protection, required reviews and `CODEOWNERS` on `server.py`, `mcp-runner.toml` and `.github/workflows/`.
- **Client auth is a shared bearer token** (no per-user identity, no expiry). Rotate it when people leave. GitHub OAuth per user is the planned replacement.
- **Single gateway instance:** state is in SQLite and memory, with no high availability.
- **Not yet verified on real GitHub:** the `environment` claim after approval, the empty `environment:` expression, and the fields `verify_run` reads from the runs API are coded from GitHub's docs and tested with mocks only.

## Reporting a problem

Please report security issues privately to the maintainers rather than in a public issue.
