"""HTTP client for the gateway's runner API, authenticated with GitHub OIDC."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

class GatewayError(Exception):
    pass


LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def check_gateway_url(url: str) -> None:
    """Allow https://, and plain http:// only when the host is exactly a loopback name.

    Compares the parsed host, so http://localhost.evil.com or http://localhost@evil.com
    don't count as local.
    """
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError) as exc:
        raise GatewayError(f"Invalid gateway URL: {exc}") from exc
    if parsed.scheme == "https" and parsed.host:
        return
    if parsed.scheme == "http" and parsed.host in LOOPBACK_HOSTS and not parsed.userinfo:
        return
    raise GatewayError("The gateway URL must use https:// (plain http is only allowed for localhost)")


def default_audience(gateway_url: str) -> str:
    """The OIDC audience a gateway expects by default: its own URL, without trailing slash."""
    return gateway_url.rstrip("/")


class GatewayClient:
    """``token`` is a string or a function returning a fresh OIDC token per request."""

    def __init__(
        self,
        base_url: str,
        token: str | Callable[[], str],
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        check_gateway_url(base_url)
        self._token = token if callable(token) else (lambda: token)
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=60, transport=transport)

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}"}

    def _check(self, resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code >= 400:
            raise GatewayError(f"Gateway returned {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.content else {}

    def put_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        return self._check(self._http.put("/runner/manifest", json=manifest, headers=self._auth()))

    def get_call(self, call_id: str) -> dict[str, Any]:
        return self._check(self._http.get(f"/runner/calls/{call_id}", headers=self._auth()))

    def post_result(self, call_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._check(self._http.post(f"/runner/calls/{call_id}/result", json=payload, headers=self._auth()))

    def close(self) -> None:
        self._http.close()
