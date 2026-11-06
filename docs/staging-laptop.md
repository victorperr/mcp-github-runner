# Staging on a laptop (Ubuntu in WSL)

Level 3 of the [test framework](testing.md#l3-staging-on-real-github-before-every-release) on one Windows laptop: the gateway and a self-hosted runner run in Ubuntu (WSL), and **Tailscale Funnel** gives the gateway a public HTTPS URL. Ubuntu gets only Tailscale, `gh`, `uv`, `jq`, `python3-venv` and the runner's own dependencies. Nothing is installed on Windows.

Every command runs in the **Ubuntu terminal**. Each line is its own command unless it ends with `|` or `\`.

- [One-time setup](#one-time-setup) (steps 0–5)
- [Every test session](#every-test-session)
- [Manual cases M01–M19](#manual-cases-m01m19)

---

## One-time setup

### 0. Browser

1. **GitHub App:** GitHub → Settings → Developer settings → GitHub Apps → New.
   - Permissions: **Actions: write**, **Environments: read**, **Metadata: read**.
   - Webhook: **off** (uncheck *Active*).
   - Note the **App ID** and download the **private key** (it goes to `C:\Users\<you>\Downloads`).
2. A free account at **tailscale.com**. `sudo tailscale up` (step 2) adds Ubuntu as a machine.

### 1. Values you'll reuse

Edit `APP_ID`, `APP_KEY` and `REPO`, then paste. Each later step starts with `source ~/staging.env`.

```bash
cat > ~/staging.env <<'EOF'
OWNER=victorperr
APP_ID=123456                                                        # your App ID
APP_KEY=/mnt/c/Users/<you>/Downloads/<your-app>.private-key.pem      # the downloaded key
STAGING=$OWNER/mcp-runner-staging
REPO=/mnt/c/Users/<you>/.../mcp-github-runner                        # this repo, checked out on Windows
EOF
source ~/staging.env
```

### 2. systemd, tools and the public URL

```bash
python3 --version   # must be 3.11 or newer (Ubuntu 24.04 has 3.12)
grep -q 'systemd=true' /etc/wsl.conf 2>/dev/null || printf '\n[boot]\nsystemd=true\n' | sudo tee -a /etc/wsl.conf
```

If the second line added something, restart WSL (`wsl.exe --shutdown`), then reopen Ubuntu.

```bash
source ~/staging.env
sudo apt-get update && sudo apt-get install -y gh python3-venv jq
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.local/bin/env
curl -fsSL https://tailscale.com/install.sh | sh

gh auth login        # GitHub.com → HTTPS → Login with a web browser
gh auth setup-git    # lets git push use your gh login
git config --global user.name "<your name>"
git config --global user.email "<id>+<user>@users.noreply.github.com"   # GitHub → Settings → Emails

sudo tailscale up                  # open the link it prints and log in: this adds the machine
sudo tailscale funnel --bg 8000    # the first time, open the link it prints to turn on HTTPS + Funnel
echo "GW=https://$(tailscale status --json | python3 -c "import json,sys;print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")" >> ~/staging.env
source ~/staging.env && echo $GW   # e.g. https://laptop-abc123.tail1234.ts.net
```

- `gh auth login` can't open a browser from WSL. Open https://github.com/login/device on any device and enter the code it printed; the command finishes by itself.
- Ubuntu's `apt` ships an old `gh`. The commands in this guide work with it (they use `gh api` where newer flags would be needed).

### 3. Staging repo, secrets and approval environment

**Both repos must be public.** GitHub refuses required reviewers on a private repo unless you're on GitHub Enterprise, and a public repo can't use an action from a private one. This is safe here: the staging workflow only runs on `push` to `main` and `workflow_dispatch`, so forks can't reach the runner. Check the history for secrets before making the action repo public.

```bash
source ~/staging.env
gh api -X PATCH repos/$OWNER/mcp-github-runner -f visibility=public --jq .visibility   # prints "public"

gh repo create $STAGING --public
mkdir ~/mcp-runner-staging && cd ~/mcp-runner-staging && git init -q -b main
cp $REPO/staging/server.py $REPO/staging/mcp-runner.toml .
mkdir -p .github/workflows && cp $REPO/staging/workflow-mcp.yml .github/workflows/mcp.yml
printf '[project]\nname = "mcp-runner-staging"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = ["fastmcp>=2.12,<3"]\n' > pyproject.toml
printf '.venv/\n__pycache__/\n' > .gitignore
uv lock
git add . && git commit -qm "Staging MCP server"
git remote add origin https://github.com/$STAGING.git
```

**Browser:** your GitHub App → **Install App** → **Only select repositories** → `mcp-runner-staging` → Install. Do this before the first push.

**Browser:** staging repo → Settings → Actions → General → *Fork pull request workflows from outside collaborators* → **Require approval for all outside collaborators**.

```bash
source ~/staging.env

# Secrets and variables (the client token is saved in ~/staging.env)
echo "CLIENT_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" >> ~/staging.env
source ~/staging.env
gh secret set MCP_CLIENT_TOKEN -R $STAGING --body "$CLIENT_TOKEN"
gh secret set STAGING_SECRET  -R $STAGING --body "$(python3 -c 'import secrets; print(secrets.token_hex(12))')"
gh variable set MCP_GATEWAY_URL -R $STAGING --body "$GW"
gh variable set MCP_RUNS_ON     -R $STAGING --body '["self-hosted", "linux", "mcp-staging"]'
```

<a id="approval-environment"></a>**Approval environment** (you as reviewer, no admin bypass, `main` only). Several manual cases break it on purpose; this same block restores it:

```bash
source ~/staging.env
ME=$(gh api user --jq .id)
gh api -X PUT repos/$STAGING/environments/mcp-approval --input - <<EOF
{"reviewers": [{"type": "User", "id": $ME}], "can_admins_bypass": false,
 "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
EOF
gh api -X POST repos/$STAGING/environments/mcp-approval/deployment-branch-policies -f name=main -f type=branch
```

The last line answers `422 … already exists` if the `main` rule is still there. That's fine.

### 4. Gateway and runner

```bash
source ~/staging.env

# Gateway, under its own user (the runner can't read its key or database)
sudo useradd --system --home-dir /opt/mcp-gateway --shell /usr/sbin/nologin gateway
sudo install -d -m 700 /opt/mcp-gateway
sudo cp -r $REPO/gateway/app $REPO/gateway/requirements.txt /opt/mcp-gateway/
sudo install -m 400 "$APP_KEY" /opt/mcp-gateway/app-key.pem
printf 'GATEWAY_PUBLIC_URL=%s\nGITHUB_APP_ID=%s\nGITHUB_APP_PRIVATE_KEY_PATH=/opt/mcp-gateway/app-key.pem\nGATEWAY_DB_PATH=/opt/mcp-gateway/gateway.db\nGATEWAY_ALLOWED_OWNERS=%s\nGATEWAY_STATUS_POLL=15\nFORWARDED_ALLOW_IPS=127.0.0.1\n' "$GW" "$APP_ID" "$OWNER" | sudo tee /opt/mcp-gateway/gateway.env >/dev/null
sudo cat /opt/mcp-gateway/gateway.env   # 7 lines, no empty value
sudo chown -R gateway /opt/mcp-gateway
sudo -u gateway python3 -m venv /opt/mcp-gateway/venv
sudo -u gateway /opt/mcp-gateway/venv/bin/pip install -q --require-hashes -r /opt/mcp-gateway/requirements.txt
rm "$APP_KEY"    # the installed copy is enough

# Runner, under its own user
sudo useradd --create-home --shell /bin/bash runner
v=$(curl -fsSLI -o /dev/null -w '%{url_effective}' https://github.com/actions/runner/releases/latest); v=${v##*/v}
echo $v          # a version such as 2.338.2; stop if empty
sudo -u runner bash -c "mkdir -p ~/actions-runner && curl -fsSL https://github.com/actions/runner/releases/download/v$v/actions-runner-linux-x64-$v.tar.gz | tar -xz -C ~/actions-runner"
sudo /home/runner/actions-runner/bin/installdependencies.sh
RUNNER_TOKEN=$(gh api -X POST repos/$STAGING/actions/runners/registration-token --jq .token)
sudo -u runner RUNNER_TOKEN="$RUNNER_TOKEN" STAGING="$STAGING" bash -c 'cd ~/actions-runner && ./config.sh --unattended --url "https://github.com/$STAGING" --token "$RUNNER_TOKEN" --name wsl-staging --labels mcp-staging'
sudo bash -c 'cd /home/runner/actions-runner && ./svc.sh install runner && ./svc.sh start'

# Must print 1 or more
cat /proc/sys/kernel/yama/ptrace_scope
```

- `/home/runner` is private (`750`), which is why the service commands go through `sudo bash -c`. Keep it that way: it separates your user from the runner.
- If `ptrace_scope` printed 0: `echo kernel.yama.ptrace_scope=1 | sudo tee /etc/sysctl.d/10-ptrace.conf && sudo sysctl --system`

**Start the gateway** in a second Ubuntu tab (Windows Terminal: **Ctrl+Shift+T**, or the **˅** next to **+** → Ubuntu) and leave it open. Audit events print there; Ctrl+C stops it.

```bash
sudo -u gateway bash -c 'cd /opt/mcp-gateway && set -a && . ./gateway.env && exec venv/bin/uvicorn --factory app.main:create_app --host 127.0.0.1 --port 8000 --proxy-headers'
```

It's ready when it prints `Application startup complete.`

### 5. First push, test setup, automated run

Back in the first tab:

```bash
source ~/staging.env
curl -fsS $GW/healthz                          # gateway reachable from the internet?
cd ~/mcp-runner-staging && git push -qu origin main
gh run list -R $STAGING -L 3                   # wait for "MCP manifest" ✓ (the job runs on your laptop)

# The "staging" environment of this repo
gh api -X PUT repos/$OWNER/mcp-github-runner/environments/staging > /dev/null
gh secret set STAGING_CLIENT_TOKEN -R $OWNER/mcp-github-runner --env staging --body "$CLIENT_TOKEN"
gh variable set STAGING_MCP_URL -R $OWNER/mcp-github-runner --env staging --body "$GW/s/$STAGING/mcp"
gh variable set STAGING_REPO    -R $OWNER/mcp-github-runner --env staging --body "$STAGING"
```

**Browser:** GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate new token:
- Name `mcp-staging-reviewer`, expiry 30 days, resource owner: you.
- Repository access: *Only select repositories* → `mcp-runner-staging`.
- Permissions: **Actions: Read and write**, **Deployments: Read and write**.

Copy it (GitHub shows it once), then:

```bash
source ~/staging.env
gh secret set STAGING_REVIEWER_TOKEN -R $OWNER/mcp-github-runner --env staging
```

Run the command first, then paste the token **without quotes** at the `? Paste your secret` prompt. Don't put the token on the command line: it would land in your shell history.

```bash
# Must end with "0 failed"
cd $REPO
STAGING_REPO=$STAGING STAGING_MCP_URL=$GW/s/$STAGING/mcp uv run --no-project --with httpx python staging/preflight.py

# Automated cases S01–S15 (about 15 min, ~22 tool-call runs in the staging repo)
gh workflow run staging.yml -R $OWNER/mcp-github-runner
gh run list -R $OWNER/mcp-github-runner --workflow staging.yml -L 1    # note the run ID
gh run watch <run-id> -R $OWNER/mcp-github-runner
gh run view <run-id> -R $OWNER/mcp-github-runner --log | grep latency  # p50/p95 for the results log
```

Some runs in the staging repo end red on purpose (S05's rejected approval, sometimes S10's oversized result). Only the **Staging tests** run in this repo decides. With one runner, S13's calls run one after another.

Record the result in [testing.md](testing.md#results-log).

### The `mcp` command (once)

The manual cases call tools with this small client. It prints the gateway's progress messages (including the approval link) and the error code of a refused call.

```bash
cat > ~/mcp-call.py <<'PY'
import asyncio, json, os, sys
from fastmcp import Client

async def progress(_progress, _total, message):
    if message:
        print(f"... {message}", flush=True)

async def main():
    url = f"{os.environ['GW']}/s/{os.environ['STAGING']}/mcp"
    async with Client(url, auth=os.environ["CLIENT_TOKEN"], timeout=900) as client:
        if len(sys.argv) == 1:
            for tool in await client.list_tools():
                print(tool.name)
            return
        args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        r = await client.call_tool(sys.argv[1], args, raise_on_error=False, progress_handler=progress)
        code = ((r.meta or {}).get("mcp-github-runner/denial") or {}).get("code")
        text = " ".join(getattr(block, "text", "") for block in r.content)
        if r.is_error:
            print(f"ERROR {code or ''} {text}")
        else:
            print(r.data if r.data is not None else text)

try:
    asyncio.run(main())
except Exception as exc:  # e.g. 401 for a wrong token
    print(f"FAILED {type(exc).__name__}: {exc}")
    sys.exit(1)
PY

cat >> ~/.bashrc <<'EOF'
# mcp [tool ['{"json": "args"}']]; MCP_TOKEN=... or MCP_REPO=owner/repo override the defaults
mcp() { ( set -a; . ~/staging.env; set +a
  STAGING=${MCP_REPO:-$STAGING} CLIENT_TOKEN=${MCP_TOKEN:-$CLIENT_TOKEN} \
  uv run -q --no-project --with "fastmcp>=2.12,<3" python ~/mcp-call.py "$@" ) }
EOF
source ~/.bashrc

mcp                                    # the 8 staging tools
mcp add '{"a": 20, "b": 22}'           # 42
```

---

## Every test session

1. **Tab 1: start the gateway** (command at the end of step 4) and leave it open. Tailscale and the runner start by themselves.
2. **Tab 2: check everything is up:**
   ```bash
   source ~/staging.env
   curl -fsS $GW/healthz
   gh api repos/$STAGING/actions/runners --jq '.runners[] | "\(.name) \(.status)"'   # wsl-staging online
   mcp add '{"a": 1, "b": 2}'                                                       # 3
   ```
3. Keep the laptop awake and an Ubuntu window open: WSL stops when no window is open.

**Testing a new gateway version:** copy it in, then restart the gateway (Ctrl+C in tab 1, start again):
```bash
sudo rm -rf /opt/mcp-gateway/app && sudo cp -r $REPO/gateway/app /opt/mcp-gateway/ && sudo chown -R gateway /opt/mcp-gateway/app
# only if gateway/requirements.txt changed:
sudo cp $REPO/gateway/requirements.txt /opt/mcp-gateway/ && sudo -u gateway /opt/mcp-gateway/venv/bin/pip install -q --require-hashes -r /opt/mcp-gateway/requirements.txt
```

**Testing an unmerged action version:** in `~/mcp-runner-staging/.github/workflows/mcp.yml`, change `victorperr/mcp-github-runner/action@main` to `@<branch>`, commit, push.

**Useful while testing:**
```bash
gh run list -R $STAGING -L 5                         # latest runs and their IDs
gh run view <run-id> -R $STAGING --log-failed        # why a run failed
gh run cancel <run-id> -R $STAGING
```

Approving a gated call: `mcp` prints *"Waiting for approval … https://github.com/…/actions/runs/…"*. Open the link → **Review deployments** → tick `mcp-approval` → **Approve and deploy** (or **Reject**).

---

## Manual cases M01–M19

Expected results are in [testing.md](testing.md#manual-cases-scripted-steps-before-every-release). Every case starts in tab 2 with:

```bash
source ~/staging.env && cd ~/mcp-runner-staging
```

Each case undoes its change at the end. After any push, wait for the new **MCP manifest** run to be ✓ (`gh run list -R $STAGING -L 1`) before calling tools.

### M01 Workflow tampering, then M02 Compromised gateway

```bash
sed -i '/^    environment:/d' .github/workflows/mcp.yml
git commit -qam "M01: drop the approval environment" && git push -q
mcp gated_action '{"label": "m01"}'
```
Expected: the run starts without asking for approval, the gateway tab shows `403 … requires approval`, `mcp` prints an error, and the tool doesn't run.

**M02** (keep the M01 change): make the gateway skip its environment check, so only the runner's own check is left.
```bash
sudo sed -i 's/    if call.approval_env and (ident/    if False and call.approval_env and (ident/' /opt/mcp-gateway/app/runner_api.py
```
Restart the gateway (Ctrl+C in tab 1, start again), then:
```bash
mcp gated_action '{"label": "m02"}'
gh run view <run-id> -R $STAGING --log | grep -- -32012
```
Expected: the runner refuses with `-32012`; the tool doesn't run.

Undo both:
```bash
sudo cp $REPO/gateway/app/runner_api.py /opt/mcp-gateway/app/ && sudo chown gateway /opt/mcp-gateway/app/runner_api.py
git revert --no-edit HEAD && git push -q
```
Restart the gateway.

### M03 Re-run

```bash
gh run list -R $STAGING -L 10          # pick a finished "MCP call …" run
gh run rerun <run-id> -R $STAGING
gh run list -R $STAGING -L 1           # then: gh run view <run-id> -R $STAGING --log-failed
```
Expected: the re-run fails with `409`; the tool doesn't run again.

### M04 Cross-run claim

Add a temporary input and step that let a manifest run try to claim another run's call:
```bash
sed -i 's/^    inputs:$/    inputs:\n      m04_call_id:\n        required: false/' .github/workflows/mcp.yml
cat >> .github/workflows/mcp.yml <<'EOF'
      - name: "M04 (temporary): claim another run's call"
        if: always() && inputs.m04_call_id != ''
        env:
          GW: ${{ vars.MCP_GATEWAY_URL }}
          CALL: ${{ inputs.m04_call_id }}
        run: |
          aud=$(jq -rn --arg a "$GW" '$a | @uri')
          token=$(curl -sS -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
            "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=$aud" | jq -r .value)
          curl -sS -w '\nM04: %{http_code}\n' -H "Authorization: Bearer $token" "$GW/runner/calls/$CALL"
EOF
git commit -qam "M04: temporary claim step" && git push -q
```
1. In tab 2: `mcp gated_action '{"label": "m04"}'`. **Don't approve it.** Its run title is `MCP call <call-id>` (`gh run list -R $STAGING -L 1`).
2. In a third tab, start a manifest run that runs in `mcp-approval` (so it passes the approval check) and probes that call:
   ```bash
   source ~/staging.env
   gh workflow run mcp.yml -R $STAGING -f approval=true -f m04_call_id=<call-id>
   ```
3. Approve **this** new run (the `MCP manifest` one), not the gated call. Then:
   ```bash
   gh run view <manifest-run-id> -R $STAGING --log | grep -A1 "This run\|M04:"
   ```
Expected: `403 This run was not started for this call`. Reject the gated call's run, then undo:
```bash
git revert --no-edit HEAD && git push -q
```

### M05 Feature branch

```bash
mcp > /tmp/tools-before.txt
git switch -qc feature
sed -i 's/branches: \[main\]/branches: [main, feature]/' .github/workflows/mcp.yml
git commit -qam "M05: push trigger on feature" && git push -qu origin feature
gh run list -R $STAGING -L 1           # the manifest run on "feature"
mcp | diff /tmp/tools-before.txt - && echo "tools unchanged"
```
Expected: the manifest run fails with `409`; the live tools are unchanged. Undo:
```bash
git switch -q main && git branch -qD feature && git push -q origin --delete feature
```

### M19 App not installed, then M06 Second repo

Both use a second repo. It runs on GitHub-hosted runners, so it doesn't need a second self-hosted runner.

```bash
STAGING2=$OWNER/mcp-runner-staging2
TOKEN2=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
gh repo create $STAGING2 --private
mkdir ~/mcp-runner-staging2 && cd ~/mcp-runner-staging2 && git init -q -b main
cp -r ~/mcp-runner-staging/{server.py,mcp-runner.toml,pyproject.toml,uv.lock,.gitignore,.github} .
git add . && git commit -qm "Second staging repo" && git remote add origin https://github.com/$STAGING2.git
gh secret set MCP_CLIENT_TOKEN -R $STAGING2 --body "$TOKEN2"
gh variable set MCP_GATEWAY_URL -R $STAGING2 --body "$GW"
gh variable set MCP_RUNS_ON     -R $STAGING2 --body '["ubuntu-latest"]'
git push -qu origin main
gh run list -R $STAGING2 -L 1          # then: gh run view <run-id> -R $STAGING2 --log-failed
```
**M19** expected: the manifest run fails with `403 Install the MCP GitHub Runner app …`.

**M06:** your GitHub App → Install App → Configure → add `mcp-runner-staging2` → Save. Then:
```bash
git commit -q --allow-empty -m "Register" && git push -q
gh run list -R $STAGING2 -L 1                            # wait for ✓
MCP_REPO=$STAGING2 MCP_TOKEN=$TOKEN2 mcp add '{"a": 1, "b": 2}'   # 3: the second repo works
MCP_TOKEN=$TOKEN2 mcp add '{"a": 1, "b": 2}'                      # its token on the first repo
```
Expected: the last call fails with `401`.

Clean up: remove `mcp-runner-staging2` from the App's repositories, then delete the repo (browser: repo → Settings → Delete this repository, or `gh auth refresh -s delete_repo && gh api -X DELETE repos/$STAGING2`), and `rm -rf ~/mcp-runner-staging2 && cd ~/mcp-runner-staging`.

### M07 Token rotation

```bash
OLD=$CLIENT_TOKEN
NEW=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
gh secret set MCP_CLIENT_TOKEN -R $STAGING --body "$NEW"
git commit -q --allow-empty -m "M07: rotate client token" && git push -q
gh run list -R $STAGING -L 1                             # wait for ✓
MCP_TOKEN=$OLD mcp add '{"a": 1, "b": 2}'                # expected: 401
MCP_TOKEN=$NEW mcp add '{"a": 1, "b": 2}'                # expected: 3
```
Keep the new token everywhere:
```bash
sed -i "s|^CLIENT_TOKEN=.*|CLIENT_TOKEN=$NEW|" ~/staging.env && source ~/staging.env
gh secret set STAGING_CLIENT_TOKEN -R $OWNER/mcp-github-runner --env staging --body "$NEW"
```

### M08 Missing environment

```bash
gh api -X DELETE repos/$STAGING/environments/mcp-approval
sleep 60                                 # a passing environment check is trusted for 60 s
mcp gated_action '{"label": "m08"}'
gh run list -R $STAGING -L 1             # no new run
```
Expected: `-32006` at once, no run started. Undo: run the [approval environment](#approval-environment) block.

### M09 No reviewers

```bash
gh api -X DELETE repos/$STAGING/environments/mcp-approval
gh api -X PUT repos/$STAGING/environments/mcp-approval --input - <<EOF
{"can_admins_bypass": false, "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
EOF
sleep 60
mcp gated_action '{"label": "m09"}'
gh run list -R $STAGING -L 1
```
Expected: `-32006` at once, no run started. Undo: run the [approval environment](#approval-environment) block.

### M10 Runner offline

```bash
sudo bash -c 'cd /home/runner/actions-runner && ./svc.sh stop'
mcp add '{"a": 1, "b": 2}'
```
Expected: after 60 s, *"Queued… Is a runner with the right labels online?"*; then `-32008` when `call_timeout_seconds` (300 s) runs out. Undo:
```bash
gh run list -R $STAGING -L 1 && gh run cancel <run-id> -R $STAGING
sudo bash -c 'cd /home/runner/actions-runner && ./svc.sh start'
```

### M11 App uninstalled

**Browser:** GitHub → Settings → Applications → Installed GitHub Apps → your app → Configure → **Uninstall**.
```bash
mcp add '{"a": 1, "b": 2}'
```
Expected: `-32007` with *"app is not installed"*. Undo: your App → Install App → Only select repositories → `mcp-runner-staging`, then `mcp add '{"a": 1, "b": 2}'` must work again. If it still fails after a minute, restart the gateway and record that as a finding (the gateway kept a revoked token).

### M12 Approval after timeout

```bash
sed -i 's/^approval_timeout_seconds = .*/approval_timeout_seconds = 60/' mcp-runner.toml
git commit -qam "M12: 60 s approval timeout" && git push -q
gh run list -R $STAGING -L 1             # wait for ✓
mcp gated_action '{"label": "m12"}'
```
Wait **90 s** after the approval link appears, then approve the run in the browser.

Expected: `mcp` got `-32008`; the approved run fails with `409`; the tool doesn't run (`gh run view <run-id> -R $STAGING --log-failed`). Undo:
```bash
git revert --no-edit HEAD && git push -q
```

### M13 Re-register from a call run

```bash
cat >> .github/workflows/mcp.yml <<'EOF'
      - name: "M13 (temporary): try to re-register from a tool-call run"
        if: inputs.call_id != ''
        env:
          GW: ${{ vars.MCP_GATEWAY_URL }}
        run: |
          aud=$(jq -rn --arg a "$GW" '$a | @uri')
          token=$(curl -sS -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
            "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=$aud" | jq -r .value)
          zeros=$(printf '0%.0s' $(seq 64))
          curl -sS -o /dev/null -w 'M13: %{http_code}\n' -X PUT "$GW/runner/manifest" \
            -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
            -d "{\"tools\": [], \"client_token_sha256\": \"$zeros\"}"
EOF
git commit -qam "M13: temporary re-register step" && git push -q
gh run list -R $STAGING -L 1             # wait for ✓
mcp add '{"a": 1, "b": 2}'
gh run list -R $STAGING -L 1 && gh run view <run-id> -R $STAGING --log | grep "M13:"
mcp add '{"a": 1, "b": 2}'               # still works with the same token
```
Expected: `M13: 403`, and `add` still works. Undo:
```bash
git revert --no-edit HEAD && git push -q
```

### M14 Manual manifest upload

```bash
gh workflow run mcp.yml -R $STAGING
gh run list -R $STAGING -L 1
```
Expected: an `MCP manifest` run that ends ✓, and `manifest.registered` in the gateway tab.

### M15 Another workflow

```bash
cp .github/workflows/mcp.yml .github/workflows/other.yml
git add . && git commit -qm "M15: second workflow" && git push -q
gh run list -R $STAGING -L 2             # then: gh run view <other-run-id> -R $STAGING --log-failed
```
Expected: `other.yml`'s job fails with `409 … registered from .github/workflows/mcp.yml`. Undo:
```bash
git revert --no-edit HEAD && git push -q
```

### M16 Admin bypass

```bash
ME=$(gh api user --jq .id)
gh api -X PUT repos/$STAGING/environments/mcp-approval --input - <<EOF
{"reviewers": [{"type": "User", "id": $ME}], "can_admins_bypass": true,
 "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
EOF
sleep 60
mcp gated_action '{"label": "m16"}'
```
Expected: `-32006` mentioning the bypass, no run started. Undo: run the [approval environment](#approval-environment) block.

### M17 Gateway restart mid-call

1. Tab 2: `mcp sleep '{"seconds": 120}'`
2. About 20 s later, restart the gateway in tab 1 (Ctrl+C, start again).
3. Tab 2, as soon as the gateway is back:
   ```bash
   for i in 1 2 3; do mcp add "{\"a\": $i, \"b\": 1}" & done; wait
   mcp add '{"a": 4, "b": 1}'
   ```
Expected: the `sleep` call errors instead of hanging; `calls.recovered` in the gateway tab; none of the `add` calls is refused with `-32005` (limit 3: a leaked slot from the lost call would make one of them fail).

### M18 Owner allowlist

```bash
sudo sed -i 's/^GATEWAY_ALLOWED_OWNERS=.*/GATEWAY_ALLOWED_OWNERS=someone-else/' /opt/mcp-gateway/gateway.env
```
Restart the gateway, then:
```bash
git commit -q --allow-empty -m "M18" && git push -q
gh run list -R $STAGING -L 1             # then: gh run view <run-id> -R $STAGING --log-failed
```
Expected: the manifest run fails with `403 … does not accept servers from this owner`. Undo:
```bash
sudo sed -i "s/^GATEWAY_ALLOWED_OWNERS=.*/GATEWAY_ALLOWED_OWNERS=$OWNER/" /opt/mcp-gateway/gateway.env
```
Restart the gateway, then `git commit -q --allow-empty -m "Re-register" && git push -q` and wait for ✓.

### After the session

Record the results in [testing.md](testing.md#results-log), then check nothing is left over:
```bash
cd ~/mcp-runner-staging && git status && git log --oneline -3      # clean, last commit is a revert or "Re-register"
gh api repos/$STAGING/environments/mcp-approval --jq '{bypass: .can_admins_bypass, rules: [.protection_rules[].type]}'
```
`bypass` must be `false` and `rules` must include `required_reviewers`.
