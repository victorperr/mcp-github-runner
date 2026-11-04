"""Gateway entry point: ``uvicorn --factory app.main:create_app``."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from .audit import audit
from .calls import CallBroker
from .config import Settings
from .dispatch import Dispatcher, GitHubAppDispatcher, LocalDispatcher
from .mcp_endpoint import TenantMCPApp, build_mcp_server
from .oidc import DEV_ISSUER, DevSigner, OIDCVerifier
from .policy import Limits
from .runner_api import router as runner_router
from .schema_check import SchemaChecker
from .store import CALL_RETENTION_SECONDS, Store

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
PURGE_INTERVAL_SECONDS = 3600


def create_app(settings: Settings | None = None, dispatcher: Dispatcher | None = None) -> FastAPI:
    settings = settings or Settings()
    store = Store(settings.db_path)
    recovered = store.fail_unfinished_calls()
    if recovered:
        audit("calls.recovered", count=recovered, reason="gateway restarted while they were in flight")

    signer: DevSigner | None = None
    if settings.dev_mode:
        # Dev mode trusts a local signing key instead of GitHub: never on a reachable address.
        host = urlsplit(settings.public_url).hostname
        if host not in LOOPBACK_HOSTS:
            raise ValueError(
                f"GATEWAY_DEV_MODE=1 is only for local testing, but GATEWAY_PUBLIC_URL is {settings.public_url}. "
                "Use a localhost URL, or turn dev mode off."
            )
        signer = DevSigner(settings.dev_key_path)
        oidc = OIDCVerifier(settings.oidc_audience, issuer=DEV_ISSUER, public_key=signer.public_key)
    else:
        oidc = OIDCVerifier(settings.oidc_audience)

    if dispatcher is None:
        if settings.dispatcher == "local":
            if signer is None:
                raise ValueError("The local dispatcher needs GATEWAY_DEV_MODE=1")
            dispatcher = LocalDispatcher(
                settings.public_url, settings.oidc_audience, signer,
                settings.local_server_dir, settings.local_server_spec,
            )
        else:
            dispatcher = GitHubAppDispatcher(
                settings.github_app_id, settings.github_app_private_key, settings.github_api_url
            )

    ceilings = Limits(
        max_calls_per_hour=settings.max_calls_per_hour,
        max_concurrent_calls=settings.max_pending_calls,
        max_argument_bytes=settings.max_argument_bytes,
        max_result_bytes=settings.max_result_bytes,
        call_timeout_seconds=int(settings.call_timeout),
        approval_timeout_seconds=int(settings.approval_timeout),
    )
    schema_checker = SchemaChecker(timeout=settings.argument_check_timeout, workers=settings.argument_check_workers)
    broker = CallBroker(
        store, dispatcher,
        ceilings=ceilings,
        heartbeat_interval=settings.heartbeat_interval,
        status_poll_interval=settings.status_poll_interval,
        schema_checker=schema_checker,
    )
    manager = StreamableHTTPSessionManager(app=build_mcp_server(store, broker), stateless=True)

    async def purge_old_calls() -> None:
        while True:
            store.purge_calls(time.time() - CALL_RETENTION_SECONDS)
            await asyncio.sleep(PURGE_INTERVAL_SECONDS)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        purger = asyncio.create_task(purge_old_calls())
        try:
            async with manager.run():
                yield
        finally:
            purger.cancel()
            schema_checker.close()

    # The interactive API docs only describe the runner API; keep them to dev mode.
    docs = {} if settings.dev_mode else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="MCP GitHub Runner gateway", lifespan=lifespan, **docs)
    app.state.settings = settings
    app.state.store = store
    app.state.oidc = oidc
    app.state.broker = broker
    app.state.dev_signer = signer
    app.include_router(runner_router)
    app.mount("/s", TenantMCPApp(store, manager, settings.public_url))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
