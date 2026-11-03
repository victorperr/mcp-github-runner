"""Run one tool call in process and return an MCP ``CallToolResult`` dict."""

from __future__ import annotations

import traceback
from typing import Any

from fastmcp import Client


def error_result(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


async def run_tool(server: Any, tool: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    try:
        async with Client(server) as client:
            result = await client.call_tool_mcp(tool, arguments or {})
    except Exception as exc:  # report anything to the client instead of hanging it
        traceback.print_exc()
        return error_result(f"Tool '{tool}' failed on the runner: {exc}")
    return result.model_dump(mode="json", by_alias=True, exclude_none=True)
