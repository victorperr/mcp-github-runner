"""Public MCP endpoint: /s/{owner}/{repo}/mcp (Streamable HTTP).

One low-level MCP server serves every registered repo. The ASGI wrapper
authenticates the client token and puts the repo on the request; handlers
read it back from ``request_context.request``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.types import Receive, Scope, Send

from .audit import audit
from .calls import CallBroker
from .store import ServerRecord, Store


def _current_server(mcp: Server) -> ServerRecord:
    request = mcp.request_context.request
    return request.scope["state"]["mcp_server"]


def build_mcp_server(store: Store, broker: CallBroker) -> Server:
    mcp = Server(
        "mcp-github-runner",
        instructions=(
            "Tools run on the owner's GitHub Actions runners, so a call can take 20-60s. "
            "Some tools need a person to approve the run in GitHub first."
        ),
    )

    # Handlers are registered directly instead of with @mcp.list_tools/@mcp.call_tool:
    # those decorators cache tool schemas by name in one dict shared by all repos.
    # The server record comes from TenantMCPApp, which read it for this request.
    async def list_tools(_: types.ListToolsRequest) -> types.ServerResult:
        server = _current_server(mcp)
        tools = server.derived.get("mcp_tools")
        if tools is None:  # parsed once per manifest version (the record is replaced on upsert)
            tools = [types.Tool.model_validate(t) for t in (server.manifest or {}).get("tools", [])]
            server.derived["mcp_tools"] = tools
        return types.ServerResult(types.ListToolsResult(tools=tools))

    async def call_tool(req: types.CallToolRequest) -> types.ServerResult:
        ctx = mcp.request_context
        server = _current_server(mcp)
        name = req.params.name
        # Tool, argument and rate checks happen in the broker (app/policy.py).

        progress_token = ctx.meta.progressToken if ctx.meta else None

        async def heartbeat(elapsed: float, message: str) -> None:
            if progress_token is not None:
                await ctx.session.send_progress_notification(
                    progress_token, elapsed, None, message, related_request_id=str(ctx.request_id)
                )
            else:
                await ctx.session.send_log_message(
                    "info", message, logger="mcp-github-runner", related_request_id=ctx.request_id
                )

        result = await broker.run(server, name, req.params.arguments or {}, heartbeat)
        return types.ServerResult(types.CallToolResult.model_validate(result))

    mcp.request_handlers[types.ListToolsRequest] = list_tools
    mcp.request_handlers[types.CallToolRequest] = call_tool
    return mcp


_NO_HASH = "0" * 64  # compared against when the repo is unknown, so both paths do the same work


def _token_ok(token: str, expected_hash: str | None) -> bool:
    ok = hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), expected_hash or _NO_HASH)
    return ok and bool(expected_hash) and bool(token)


class TenantMCPApp:
    """ASGI app mounted at /s. Path: /{owner}/{repo}/mcp"""

    def __init__(self, store: Store, manager: StreamableHTTPSessionManager, public_url: str):
        self.store = store
        self.manager = manager
        self.public_url = public_url.rstrip("/")

    async def _reply(self, send: Send, status: int, body: dict[str, Any], headers: list | None = None) -> None:
        data = json.dumps(body).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())]
            + (headers or []),
        })
        await send({"type": "http.response.body", "body": data})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        # Starlette's Mount keeps the full path and puts the mount prefix in root_path.
        path, root = scope["path"], scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root):]
        parts = [p for p in path.split("/") if p]
        if len(parts) != 3 or parts[2] != "mcp":
            await self._reply(send, 404, {"error": "Use /s/{owner}/{repo}/mcp"})
            return
        full_name = f"{parts[0]}/{parts[1]}"

        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        # Same work for unknown repos and wrong tokens (one indexed lookup, one hash),
        # so response timing doesn't reveal which repos are registered.
        found = self.store.find_token_hash(full_name)
        authorized = _token_ok(token, found[1] if found else None)
        server = self.store.get_server(found[0]) if authorized else None
        if server is None:
            client = scope.get("client") or (None,)
            audit("client.auth_failed", repo=full_name, ip=client[0], had_token=bool(token))
            # Same response for unknown repos and bad tokens.
            await self._reply(
                send, 401, {"error": "Invalid or missing bearer token"},
                [(b"www-authenticate", b'Bearer realm="mcp-github-runner"')],
            )
            return

        scope.setdefault("state", {})["mcp_server"] = server
        await self.manager.handle_request(scope, receive, send)
