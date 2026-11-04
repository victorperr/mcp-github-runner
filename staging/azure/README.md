# Staging on one Azure VM

One small Ubuntu VM runs the whole staging setup:

- the **gateway** in Docker, behind **Caddy** for HTTPS (free automatic certificate on the VM's Azure DNS name);
- the **self-hosted runner**, as a separate `runner` user that is not in the `docker` group, so tool code can't read the gateway's database or GitHub App key.

Nothing is installed on your PC. Commands below run in **Git Bash** on your PC unless marked *(VM)*. The general staging plan and the test cases are in [docs/testing.md](../../docs/testing.md#l3-staging-on-real-github-before-every-release).

Cost: Azure for Students covers a B1s VM under its free services (check your portal's "Free services" page). The public IP and disk may use a little of the $100 credit. Turn on auto-shutdown and stop the VM when you're not testing.

---

## 0. Values you'll reuse

Set these once in Git Bash (edit the first lines):

```bash
OWNER=victorperr
APP_ID=123456                                        # your GitHub App's App ID
APP_KEY=~/Downloads/mcp-github-runner-staging.private-key.pem   # its downloaded private key
VM_KEY=~/.ssh/mcp-staging.pem                        # VM SSH key (step 1)
FQDN=mcp-gw-staging-$OWNER.francecentral.cloudapp.azure.com     # the VM DNS name (step 1)

STAGING=$OWNER/mcp-runner-staging
GW=https://$FQDN
REPO_DIR=/c/Users/victo/Documents/centrale_travail/PORTEFOLIO
UV=$REPO_DIR/mcp-github-runner/.venv/Scripts/uv.exe
```

## 1. Create the VM (Azure portal)

**Create a resource → Virtual machine:**

| Tab | Setting | Value |
|---|---|---|
| Basics | Subscription | Azure for Students |
| | Resource group | Create new: `mcp-staging` |
| | Virtual machine name | `mcp-staging` |
| | Region | One your subscription allows (if creation is refused by a policy, try France Central, Sweden Central or West Europe) |
| | Image | Ubuntu Server 24.04 LTS – x64 |
| | Size | **Standard_B1s** (look for "free services eligible") |
| | Authentication | SSH public key, username `azureuser`, *Generate new key pair*, name `mcp-staging` |
| | Inbound ports | **SSH (22), HTTP (80), HTTPS (443)** |
| Disks | OS disk | Defaults are fine |
| Management | Auto-shutdown | On, e.g. 23:00 your time zone |

**Review + create → Create → Download private key.** Move it to `~/.ssh/mcp-staging.pem`.

Then give the VM a DNS name: open the VM → **Overview** → *DNS name: Not configured* → set **DNS name label** to `mcp-gw-staging-<your user>` → **Save**. The full name (e.g. `mcp-gw-staging-victorperr.francecentral.cloudapp.azure.com`) is your `FQDN` in step 0.

Check you can connect:

```bash
ssh -i $VM_KEY azureuser@$FQDN "uname -a"
```

If SSH refuses the key as "unprotected", run in PowerShell: `icacls $HOME\.ssh\mcp-staging.pem /inheritance:r /grant:r "$($env:USERNAME):(R)"`.

## 2. Let the staging repo use the action

`mcp-github-runner` is private, so its action can only be used by other repos you own once you allow it:

```bash
gh api -X PUT repos/$OWNER/mcp-github-runner/actions/permissions/access -f access_level=user
```

## 3. Create the staging repo (don't push yet)

```bash
cd $REPO_DIR
gh repo create $STAGING --private
mkdir mcp-runner-staging && cd mcp-runner-staging && git init -q -b main
cp ../mcp-github-runner/staging/server.py ../mcp-github-runner/staging/mcp-runner.toml .
mkdir -p .github/workflows && cp ../mcp-github-runner/staging/workflow-mcp.yml .github/workflows/mcp.yml
cat > pyproject.toml <<'EOF'
[project]
name = "mcp-runner-staging"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["fastmcp>=2.12,<3"]
EOF
printf '.venv/\n__pycache__/\n' > .gitignore
"$UV" lock          # resolves exact versions only; installs nothing on your PC
git add . && git commit -qm "Staging MCP server"
git remote add origin https://github.com/$STAGING.git
```

## 4. Install the GitHub App on it (browser)

GitHub → **Settings → Developer settings → GitHub Apps** → your staging App → **Install App** → your account → **Only select repositories** → `mcp-runner-staging` → **Install**.

This must happen before the first push: the gateway refuses to register a repo without the App.

## 5. Secrets, variables and the approval environment

```bash
CLIENT_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
echo "Client token (save it in your password manager): $CLIENT_TOKEN"

gh secret set MCP_CLIENT_TOKEN -R $STAGING --body "$CLIENT_TOKEN"
gh secret set STAGING_SECRET  -R $STAGING --body "$(python -c "import secrets; print(secrets.token_hex(12))")"
gh variable set MCP_GATEWAY_URL -R $STAGING --body "$GW"
gh variable set MCP_RUNS_ON     -R $STAGING --body '["self-hosted", "linux", "mcp-staging"]'

# mcp-approval: you as required reviewer, admin bypass off, main branch only.
ME=$(gh api user --jq .id)
gh api -X PUT repos/$STAGING/environments/mcp-approval --input - <<EOF
{"reviewers": [{"type": "User", "id": $ME}],
 "can_admins_bypass": false,
 "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
EOF
gh api -X POST repos/$STAGING/environments/mcp-approval/deployment-branch-policies -f name=main -f type=branch
```

If the environment call fails about reviewers: required reviewers on a private repo need GitHub Pro, which is free with the GitHub Student Developer Pack.

## 6. Set up the VM

From an up-to-date `main` of this repo, upload the gateway, the kit and the App key, then run the setup script on the VM:

```bash
cd $REPO_DIR/mcp-github-runner && git checkout -q main && git pull -q
ssh -i $VM_KEY azureuser@$FQDN "mkdir -p upload"
scp -i $VM_KEY -rq gateway staging/azure/compose.yml staging/azure/Caddyfile staging/azure/setup-vm.sh azureuser@$FQDN:upload/
scp -i $VM_KEY -q "$APP_KEY" azureuser@$FQDN:upload/app-key.pem

RUNNER_TOKEN=$(gh api -X POST repos/$STAGING/actions/runners/registration-token --jq .token)
ssh -i $VM_KEY azureuser@$FQDN \
  "sudo DOMAIN=$FQDN APP_ID=$APP_ID OWNER=$OWNER STAGING_REPO=$STAGING RUNNER_TOKEN=$RUNNER_TOKEN bash upload/setup-vm.sh"
```

It takes a few minutes. It ends with the MCP URL and two checks (`ptrace_scope` 1 or more, `runner` not in the docker group). It installs the key where only the gateway can read it and deletes the uploaded copy.

To deploy a new gateway version later, re-upload `gateway` and run the same `ssh … setup-vm.sh` line without `RUNNER_TOKEN` (and re-upload the key only if it changed).

## 7. First push

```bash
cd $REPO_DIR/mcp-runner-staging && git push -qu origin main
gh run watch -R $STAGING
```

The manifest job runs on the VM and registers the server. Audit events show it *(VM)*:

```bash
ssh -i $VM_KEY azureuser@$FQDN "sudo docker compose -f /opt/mcp-gateway/compose.yml logs gateway | grep manifest"
```

## 8. The `staging` environment of this repo

The Staging tests workflow runs here and needs to reach the staging server:

```bash
cd $REPO_DIR/mcp-github-runner
gh api -X PUT repos/$OWNER/mcp-github-runner/environments/staging > /dev/null
gh secret set STAGING_CLIENT_TOKEN -R $OWNER/mcp-github-runner --env staging --body "$CLIENT_TOKEN"
gh variable set STAGING_MCP_URL -R $OWNER/mcp-github-runner --env staging --body "$GW/s/$STAGING/mcp"
gh variable set STAGING_REPO    -R $OWNER/mcp-github-runner --env staging --body "$STAGING"
```

For the approval tests (S04/S05), create a **fine-grained token** as yourself (the reviewer): GitHub → Settings → Developer settings → **Fine-grained tokens → Generate new token**, repository access *Only select repositories* → `mcp-runner-staging`, permissions **Actions: Read and write** and **Deployments: Read and write**, expiry 30 days. Then:

```bash
gh secret set STAGING_REVIEWER_TOKEN -R $OWNER/mcp-github-runner --env staging   # paste the token when asked
```

## 9. Preflight, then the tests

```bash
STAGING_REPO=$STAGING STAGING_MCP_URL=$GW/s/$STAGING/mcp .venv/Scripts/python staging/preflight.py
```

It must end with `0 failed`. Then run the automated cases (S01–S15) and follow them:

```bash
gh workflow run staging.yml -R $OWNER/mcp-github-runner
gh run watch -R $OWNER/mcp-github-runner
```

The manual cases (M01–M19) are in [docs/testing.md](../../docs/testing.md#manual-cases-scripted-steps-before-every-release).

## When you're done

Stop the VM in the portal (**Stop**, not just shut down from inside, so compute isn't billed). Delete the `mcp-staging` resource group when you no longer need staging. To remove the runner from GitHub: repo **Settings → Actions → Runners**.
