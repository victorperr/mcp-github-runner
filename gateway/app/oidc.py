"""Verify GitHub Actions OIDC tokens sent by the runner.

In dev mode, a local RSA key signs and verifies tokens instead, so the whole
flow can be tested without GitHub.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_JWKS_URL = f"{GITHUB_ISSUER}/.well-known/jwks"
DEV_ISSUER = "mcp-github-runner-dev"

logger = logging.getLogger(__name__)


class OIDCError(Exception):
    pass


@dataclass
class RunnerIdentity:
    repository_id: str
    repository: str
    event_name: str
    ref: str
    workflow_path: str  # e.g. .github/workflows/mcp.yml
    run_id: str
    # Set by GitHub only when the job ran in an Environment (after any required approval).
    environment: str | None = None

    @classmethod
    def from_claims(cls, claims: dict[str, Any]) -> "RunnerIdentity":
        try:
            # workflow_ref = "owner/repo/.github/workflows/mcp.yml@refs/heads/main"
            workflow_ref = claims["workflow_ref"]
            repo = claims["repository"]
            path = workflow_ref.split("@", 1)[0]
            if not path.lower().startswith(repo.lower() + "/"):
                raise OIDCError("workflow_ref does not belong to the repository")
            return cls(
                repository_id=str(claims["repository_id"]),
                repository=repo,
                event_name=claims["event_name"],
                ref=claims["ref"],
                workflow_path=path[len(repo) + 1 :],
                run_id=str(claims["run_id"]),
                environment=claims.get("environment") or None,
            )
        except KeyError as exc:
            raise OIDCError(f"OIDC token is missing claim {exc}") from exc


class JWKSCache:
    """GitHub's signing keys, refreshed at most once per ``min_refresh`` seconds.

    PyJWKClient downloads the key set again for every unknown ``kid``, so anyone
    could make the gateway call GitHub (and tie up a worker thread) once per
    garbage token. Here an unknown ``kid`` triggers a refresh only if the last
    one is older than ``min_refresh``; otherwise the token is rejected.
    """

    def __init__(self, url: str, *, min_refresh: float = 60, max_age: float = 3600, timeout: float = 10):
        self.url = url
        self.min_refresh = min_refresh
        self.max_age = max_age
        self.timeout = timeout
        self._keys: dict[str, Any] = {}
        self._fetched_at = float("-inf")
        self._lock = threading.Lock()

    def key_for(self, kid: str | None) -> Any:
        if not kid:
            raise OIDCError("Invalid OIDC token: no key ID")
        key = self._keys.get(kid)
        if key is not None and time.monotonic() - self._fetched_at < self.max_age:
            return key
        with self._lock:  # one refresh at a time; others reuse its result
            now = time.monotonic()
            key = self._keys.get(kid)
            stale = now - self._fetched_at >= self.max_age
            if (key is None or stale) and now - self._fetched_at >= self.min_refresh:
                self._refresh()
                key = self._keys.get(kid)
        if key is None:
            raise OIDCError("Invalid OIDC token: unknown signing key")
        return key

    def _refresh(self) -> None:
        # Set first, so failed fetches are rate limited too.
        self._fetched_at = time.monotonic()
        try:
            resp = httpx.get(self.url, timeout=self.timeout)
            resp.raise_for_status()
            jwk_set = jwt.PyJWKSet.from_dict(resp.json())
        except (httpx.HTTPError, ValueError, jwt.PyJWTError) as exc:
            # Keep the keys we have; tokens signed with them still verify.
            logger.warning("could not refresh GitHub OIDC keys: %s", exc)
            return
        self._keys = {k.key_id: k.key for k in jwk_set.keys if k.key_id}


class OIDCVerifier:
    def __init__(
        self, audience: str, *, issuer: str = GITHUB_ISSUER, public_key: Any = None, jwks: JWKSCache | None = None
    ):
        self.audience = audience
        self.issuer = issuer
        self._public_key = public_key
        self._jwks = None if public_key is not None else (jwks or JWKSCache(GITHUB_JWKS_URL))

    def verify(self, token: str) -> RunnerIdentity:
        try:
            if self._public_key is not None:
                key = self._public_key
            else:
                key = self._jwks.key_for(jwt.get_unverified_header(token).get("kid"))
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise OIDCError(f"Invalid OIDC token: {exc}") from exc
        return RunnerIdentity.from_claims(claims)


class DevSigner:
    """Signs fake runner tokens for local testing."""

    def __init__(self, key_path: str | None = None):
        path = Path(key_path) if key_path else None
        if path and path.exists():
            self._key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        else:
            self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            if path:
                pem = self._key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
                # Anyone holding this key can forge runner tokens for a dev gateway: owner-only.
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(pem)

    @property
    def public_key(self) -> Any:
        return self._key.public_key()

    def sign(
        self,
        *,
        audience: str,
        repository: str,
        repository_id: str,
        event_name: str,
        ref: str = "refs/heads/main",
        workflow_path: str = ".github/workflows/mcp.yml",
        run_id: str = "1",
        environment: str | None = None,
        issuer: str = DEV_ISSUER,
        ttl: int = 600,
    ) -> str:
        now = int(time.time())
        claims = {
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "exp": now + ttl,
            "repository": repository,
            "repository_id": repository_id,
            "event_name": event_name,
            "ref": ref,
            "workflow_ref": f"{repository}/{workflow_path}@{ref}",
            "run_id": run_id,
        }
        if environment:
            claims["environment"] = environment
        return jwt.encode(claims, self._key, algorithm="RS256")
