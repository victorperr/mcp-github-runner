#!/usr/bin/env bash
# Sets up the staging VM: swap, Docker, the gateway behind Caddy (HTTPS), and a
# GitHub Actions runner under its own user. Run on the VM (see README.md):
#
#   sudo DOMAIN=<vm dns name> APP_ID=<github app id> OWNER=<github user> \
#        STAGING_REPO=<owner/repo> RUNNER_TOKEN=<registration token> bash ~/upload/setup-vm.sh
#
# Safe to run again: it rebuilds and restarts the gateway, and skips the runner
# when it is already registered (RUNNER_TOKEN is then not needed).
set -euo pipefail

: "${DOMAIN:?set DOMAIN to the VM DNS name, e.g. mcp-gw-staging.francecentral.cloudapp.azure.com}"
: "${APP_ID:?set APP_ID to the GitHub App ID}"
: "${OWNER:?set OWNER to the GitHub user or org allowed to register servers}"
: "${STAGING_REPO:?set STAGING_REPO to owner/repo of the staging repo}"
[ "$(id -u)" = 0 ] || { echo "Run with sudo."; exit 1; }

UPLOAD="$(cd "$(dirname "$0")" && pwd)"
GW_DIR=/opt/mcp-gateway
RUNNER_USER=runner
RUNNER_DIR="/home/$RUNNER_USER/actions-runner"

for f in gateway/Dockerfile compose.yml Caddyfile; do
  [ -e "$UPLOAD/$f" ] || { echo "Missing $UPLOAD/$f: upload it first (see README.md)."; exit 1; }
done
if [ ! -e "$UPLOAD/app-key.pem" ] && [ ! -e "$GW_DIR/app-key.pem" ]; then
  echo "Missing $UPLOAD/app-key.pem (the GitHub App private key): upload it first (see README.md)."
  exit 1
fi

echo "== 1/5 Swap file (the B1s VM has only 1 GB of RAM)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== 2/5 Packages and Docker"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl git jq tar ca-certificates >/dev/null
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh

echo "== 3/5 Gateway files in $GW_DIR (root only: the runner user can't read them)"
install -d -m 700 "$GW_DIR"
rm -rf "$GW_DIR/gateway"
cp -r "$UPLOAD/gateway" "$UPLOAD/compose.yml" "$UPLOAD/Caddyfile" "$GW_DIR/"
# The gateway container runs as UID 10001; nobody else on the VM can read the key.
# The uploaded copy is deleted once installed.
if [ -e "$UPLOAD/app-key.pem" ]; then
  install -m 400 -o 10001 -g root "$UPLOAD/app-key.pem" "$GW_DIR/app-key.pem"
  rm -f "$UPLOAD/app-key.pem"
fi
cat > "$GW_DIR/gateway.env" <<EOF
GATEWAY_PUBLIC_URL=https://$DOMAIN
GITHUB_APP_ID=$APP_ID
GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/app-key.pem
GATEWAY_ALLOWED_OWNERS=$OWNER
GATEWAY_STATUS_POLL=15
# Only Caddy can reach the gateway (no published port), and Caddy sets
# X-Forwarded-For itself, so trusting proxy headers is safe here.
FORWARDED_ALLOW_IPS=*
EOF
chmod 600 "$GW_DIR/gateway.env"
echo "DOMAIN=$DOMAIN" > "$GW_DIR/.env"  # read by compose, passed to Caddy

echo "== 4/5 Build and start the gateway and Caddy (HTTPS certificate is automatic)"
docker compose -f "$GW_DIR/compose.yml" up -d --build
for _ in $(seq 1 36); do
  curl -fsS "https://$DOMAIN/healthz" >/dev/null 2>&1 && break
  sleep 5
done
if curl -fsS "https://$DOMAIN/healthz" >/dev/null 2>&1; then
  echo "   Gateway is up: https://$DOMAIN/healthz"
else
  echo "   Gateway not reachable over HTTPS yet. Check that ports 80 and 443 are open in the"
  echo "   VM's network settings and that the DNS name is right, then: sudo docker compose -f $GW_DIR/compose.yml logs"
  exit 1
fi

echo "== 5/5 GitHub Actions runner (user '$RUNNER_USER', not in the docker group)"
id "$RUNNER_USER" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$RUNNER_USER"
if [ -f "$RUNNER_DIR/.runner" ]; then
  echo "   Already registered; skipping."
else
  : "${RUNNER_TOKEN:?set RUNNER_TOKEN: gh api -X POST repos/$STAGING_REPO/actions/runners/registration-token --jq .token}"
  version="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r .tag_name)"
  version="${version#v}"
  sudo -u "$RUNNER_USER" mkdir -p "$RUNNER_DIR"
  curl -fsSL "https://github.com/actions/runner/releases/download/v$version/actions-runner-linux-x64-$version.tar.gz" \
    | sudo -u "$RUNNER_USER" tar -xz -C "$RUNNER_DIR"
  "$RUNNER_DIR/bin/installdependencies.sh" >/dev/null
  # The token goes through the environment, not the command line (visible in ps).
  sudo -u "$RUNNER_USER" RUNNER_TOKEN="$RUNNER_TOKEN" STAGING_REPO="$STAGING_REPO" RUNNER_NAME="$(hostname)-staging" \
    bash -c 'cd ~/actions-runner && ./config.sh --unattended --replace --url "https://github.com/$STAGING_REPO" \
      --token "$RUNNER_TOKEN" --name "$RUNNER_NAME" --labels mcp-staging'
  (cd "$RUNNER_DIR" && ./svc.sh install "$RUNNER_USER" >/dev/null && ./svc.sh start >/dev/null)
  echo "   Runner registered and started as a service."
fi

echo "== Checks"
echo "   kernel.yama.ptrace_scope = $(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo 'not available') (want 1 or more)"
if id -nG "$RUNNER_USER" | grep -qw docker; then
  echo "   WARNING: '$RUNNER_USER' is in the docker group, so tool code could control the gateway container."
else
  echo "   '$RUNNER_USER' is not in the docker group: good."
fi
echo
echo "Done. MCP URL for the staging repo: https://$DOMAIN/s/$STAGING_REPO/mcp"
