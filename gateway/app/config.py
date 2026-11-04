"""Gateway settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    public_url: str = field(default_factory=lambda: _env("GATEWAY_PUBLIC_URL", "http://localhost:8000"))
    db_path: str = field(default_factory=lambda: _env("GATEWAY_DB_PATH", "gateway.db"))
    # Empty = the public URL (without trailing slash). One audience per deployment means a
    # runner token sent to another gateway (staging, self-hosted...) can't be replayed here.
    oidc_audience: str = field(default_factory=lambda: _env("GATEWAY_OIDC_AUDIENCE"))
    call_timeout: float = field(default_factory=lambda: float(_env("GATEWAY_CALL_TIMEOUT", "300")))
    # Calls to tools that need approval wait for a person, so they get longer.
    approval_timeout: float = field(default_factory=lambda: float(_env("GATEWAY_APPROVAL_TIMEOUT", "900")))
    heartbeat_interval: float = field(default_factory=lambda: float(_env("GATEWAY_HEARTBEAT", "10")))
    status_poll_interval: float = field(default_factory=lambda: float(_env("GATEWAY_STATUS_POLL", "15")))
    max_pending_calls: int = field(default_factory=lambda: int(_env("GATEWAY_MAX_PENDING_CALLS", "5")))
    max_manifest_bytes: int = 512 * 1024
    # Operator ceilings: a repo's mcp-runner.toml can lower these, never raise them.
    max_calls_per_hour: int = field(default_factory=lambda: int(_env("GATEWAY_MAX_CALLS_PER_HOUR", "1000")))
    max_argument_bytes: int = field(default_factory=lambda: int(_env("GATEWAY_MAX_ARGUMENT_BYTES", str(256 * 1024))))
    max_result_bytes: int = field(default_factory=lambda: int(_env("GATEWAY_MAX_RESULT_BYTES", str(4 * 1024 * 1024))))
    # "stdout" prints audit events as JSON lines; "off" leaves the logger to your logging config.
    audit_log: str = field(default_factory=lambda: _env("GATEWAY_AUDIT_LOG", "stdout"))
    # Tool arguments are checked against tenant schemas in worker processes, killed after this long.
    argument_check_timeout: float = field(default_factory=lambda: float(_env("GATEWAY_ARGUMENT_CHECK_TIMEOUT", "2")))
    argument_check_workers: int = field(default_factory=lambda: int(_env("GATEWAY_ARGUMENT_CHECK_WORKERS", "2")))
    # Comma-separated GitHub owners (users or orgs) allowed to register servers. Empty = any
    # owner that installed the GitHub App.
    allowed_owners: frozenset[str] = field(default_factory=lambda: frozenset(
        o.strip().lower() for o in _env("GATEWAY_ALLOWED_OWNERS").split(",") if o.strip()
    ))

    # "github" dispatches workflow runs; "local" runs mcp_runner as a subprocess (dev only).
    dispatcher: str = field(default_factory=lambda: _env("GATEWAY_DISPATCHER", "github"))
    local_server_dir: str = field(default_factory=lambda: _env("GATEWAY_LOCAL_SERVER_DIR", ""))
    local_server_spec: str = field(default_factory=lambda: _env("GATEWAY_LOCAL_SERVER_SPEC", "server.py"))

    # Dev mode trusts tokens signed by a local key instead of GitHub's OIDC issuer.
    dev_mode: bool = field(default_factory=lambda: _env("GATEWAY_DEV_MODE") == "1")
    dev_key_path: str = field(default_factory=lambda: _env("GATEWAY_DEV_KEY_PATH", "dev-oidc-key.pem"))

    github_app_id: str = field(default_factory=lambda: _env("GITHUB_APP_ID"))
    github_app_private_key: str = field(default_factory=lambda: _env("GITHUB_APP_PRIVATE_KEY"))
    github_api_url: str = field(default_factory=lambda: _env("GITHUB_API_URL", "https://api.github.com"))

    def __post_init__(self) -> None:
        if not self.oidc_audience:
            self.oidc_audience = self.public_url.rstrip("/")
        key_path = _env("GITHUB_APP_PRIVATE_KEY_PATH")
        if not self.github_app_private_key and key_path:
            self.github_app_private_key = Path(key_path).read_text()

    def mcp_url(self, full_name: str) -> str:
        return f"{self.public_url.rstrip('/')}/s/{full_name}/mcp"
