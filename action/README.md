# MCP GitHub Runner action

Runs one tool call (or uploads the tool list) of a FastMCP server for the MCP GitHub Runner gateway. Use it from the workflow in [template/](../template/).

```yaml
- uses: victorperr/mcp-github-runner/action@v1   # pin to a commit SHA in production
  with:
    mode: ${{ inputs.call_id && 'call' || 'manifest' }}
    call-id: ${{ inputs.call_id }}
    server: server.py:mcp
    gateway-url: ${{ vars.MCP_GATEWAY_URL }}
```

The job needs `permissions: id-token: write`.

## Inputs

| Input | Default | Description |
|---|---|---|
| `mode` | (required) | `manifest` uploads the tool list; `call` runs one tool call |
| `call-id` | `""` | Call ID sent by the gateway (required in `call` mode) |
| `server` | `server.py` | FastMCP server as `path/to/file.py[:attr]` |
| `working-directory` | `.` | Folder with `pyproject.toml` / `uv.lock` / `requirements.txt` |
| `gateway-url` | (required) | Gateway base URL (must be `https://`). Usually `${{ vars.MCP_GATEWAY_URL }}` |
| `audience` | `""` (the gateway URL) | OIDC audience the gateway expects. Only set it if the gateway sets `GATEWAY_OIDC_AUDIENCE` |
| `approval-environment` | `mcp-approval` | Environment that tools needing approval must run in |
| `policy-file` | `mcp-runner.toml` | Optional policy file (tool allowlist, approvals, limits) |
| `redact-env` | `""` | Extra env var names whose values are redacted from results |
| `python-version` | `3.12` | Python 3.11+ used when the project doesn't pin one |

## What it does

1. Installs `uv` (pinned by SHA) and your dependencies, from `uv.lock` when present (`--locked`).
2. Passes the OIDC request token and `MCP_CLIENT_TOKEN` to `mcp_runner` on stdin and removes them, plus the Actions runtime tokens, from the environment. Your code runs in a separate process that never holds them.
3. **`manifest`**: lists your tools, applies `mcp-runner.toml`, and uploads the manifest plus the SHA-256 of `MCP_CLIENT_TOKEN`.
4. **`call`**: fetches the call, checks on its own that the tool is published and that gated tools ran in the approval environment, runs the tool in the tool process, redacts secrets, caps the size, and posts the result.

See [docs/security.md](../docs/security.md) for the full model.

## License

Apache-2.0, see [LICENSE](../LICENSE).
