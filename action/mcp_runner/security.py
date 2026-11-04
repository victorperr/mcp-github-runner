"""Runner-side protections. These hold even if the gateway is compromised.

- read_credentials takes the OIDC request credentials and client token from
  stdin: action.yml removes them from the environment before it execs Python,
  so no process in the chain has them in /proc/<pid>/environ.
- OIDCSource holds those credentials in the supervisor's memory only. Tool code
  runs in a separate process (worker.py), and harden_process stops same-user
  processes from reading the supervisor's memory on Linux.
- scrub_environment removes other runtime credentials the tools don't need.
- Redactor replaces secret values in tool results before they leave the runner.
- unverified_claims reads our own OIDC token, which came straight from the
  Actions runtime over TLS, so the runner can check approval itself.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any, TextIO

import httpx

# Credentials the Actions runtime puts in the step environment. Tools never need them.
RUNTIME_CREDENTIALS = (
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_URL",
    "ACTIONS_RUNTIME_TOKEN",
    "ACTIONS_RUNTIME_URL",
    "ACTIONS_RESULTS_URL",
    "ACTIONS_CACHE_URL",
    "MCP_RUNNER_OIDC_TOKEN",
    "MCP_CLIENT_TOKEN",
)

SECRET_NAME = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE_KEY|API_KEY|APIKEY|CREDENTIAL|_KEY$|^KEY$|DSN|DATABASE_URL)", re.I)
MIN_SECRET_LEN = 8
REDACTED = "***"
CALL_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
MIN_CLIENT_TOKEN_LEN = 32


class SecurityError(Exception):
    pass


@dataclass
class Credentials:
    oidc_request_url: str = ""
    oidc_request_token: str = ""
    client_token: str = ""


def read_credentials(stream: TextIO | None = None) -> Credentials:
    """Read the credentials action.yml writes on stdin, one per line, then detach stdin.

    Line order: OIDC request URL, OIDC request token, client token (empty lines allowed).
    """
    stream = sys.stdin if stream is None else stream
    lines = (stream.read().split("\n") + ["", "", ""])[:3]
    if stream is sys.stdin:
        # Replace fd 0 so nothing keeps the pipe (or bash's here-string file) open.
        try:
            devnull = os.open(os.devnull, os.O_RDONLY)
            os.dup2(devnull, 0)
            os.close(devnull)
        except OSError:
            pass
    return Credentials(*(line.strip() for line in lines))


def harden_process() -> None:
    """Linux: mark this process non-dumpable.

    Other processes of the same user (tool code included) then can't read its
    memory through /proc/<pid>/mem or attach with ptrace. Children started with
    exec are dumpable again, which is fine: they hold no credentials.
    """
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE, 0
    except (OSError, AttributeError):  # pragma: no cover - libc without prctl
        pass


class OIDCSource:
    """Holds the OIDC request credentials in memory and removes them from the environment."""

    def __init__(
        self,
        audience: str,
        environ: MutableMapping[str, str] | None = None,
        *,
        url: str | None = None,
        request_token: str | None = None,
    ):
        env = os.environ if environ is None else environ
        self.audience = audience
        self._override = env.pop("MCP_RUNNER_OIDC_TOKEN", None)  # local testing only
        env_url = env.pop("ACTIONS_ID_TOKEN_REQUEST_URL", None)
        env_token = env.pop("ACTIONS_ID_TOKEN_REQUEST_TOKEN", None)
        self._url = url or env_url
        self._request_token = request_token or env_token

    @property
    def secrets(self) -> list[str]:
        return [v for v in (self._override, self._request_token) if v]

    def token(self) -> str:
        if self._override:
            return self._override
        if not self._url or not self._request_token:
            raise SecurityError(
                "No GitHub OIDC token available. Add `permissions: id-token: write` to the workflow."
            )
        # The URL already carries query params (api-version); httpx `params=` would drop them.
        resp = httpx.get(
            httpx.URL(self._url).copy_merge_params({"audience": self.audience}),
            headers={"Authorization": f"bearer {self._request_token}"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["value"]


def scrub_environment(environ: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """Remove runtime credentials from the environment. Returns what was removed."""
    env = os.environ if environ is None else environ
    return {name: env.pop(name) for name in RUNTIME_CREDENTIALS if name in env}


def unverified_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without checking the signature.

    Only safe for a token this process just received from the Actions runtime.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError) as exc:
        raise SecurityError("Could not read the OIDC token") from exc


def validate_call_id(call_id: str) -> str:
    if not CALL_ID.match(call_id or ""):
        raise SecurityError("Invalid call ID")
    return call_id


def check_client_token(token: str) -> None:
    if len(token) < MIN_CLIENT_TOKEN_LEN:
        raise SecurityError(
            f"MCP_CLIENT_TOKEN must be at least {MIN_CLIENT_TOKEN_LEN} characters. Generate one with:\n"
            "  python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )


class Redactor:
    """Replaces known secret values anywhere in a JSON-like result."""

    def __init__(self, environ: MutableMapping[str, str] | None = None, extra: list[str] | None = None):
        env = os.environ if environ is None else environ
        explicit = {n.strip() for n in env.get("MCP_REDACT_ENV", "").split(",") if n.strip()}
        values: set[str] = set(extra or [])
        for name, value in env.items():
            if name in RUNTIME_CREDENTIALS or name in explicit or SECRET_NAME.search(name):
                values.add(value)
        # Longest first, so a secret that contains another is replaced whole.
        self._values = sorted((v for v in values if v and len(v) >= MIN_SECRET_LEN), key=len, reverse=True)

    def add(self, value: str | None) -> None:
        if value and len(value) >= MIN_SECRET_LEN and value not in self._values:
            self._values.append(value)
            self._values.sort(key=len, reverse=True)

    def redact(self, obj: Any) -> tuple[Any, int]:
        """Return (redacted copy, number of replacements)."""
        count = 0

        def walk(value: Any) -> Any:
            nonlocal count
            if isinstance(value, str):
                for secret in self._values:
                    if secret in value:
                        count += value.count(secret)
                        value = value.replace(secret, REDACTED)
                return value
            if isinstance(value, list):
                return [walk(v) for v in value]
            if isinstance(value, dict):
                return {walk(k) if isinstance(k, str) else k: walk(v) for k, v in value.items()}
            return value

        return walk(obj), count
