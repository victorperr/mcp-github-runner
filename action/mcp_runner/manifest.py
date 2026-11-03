"""Build the tool manifest the gateway serves for ``tools/list``.

The same function runs in call mode, so the runner decides from its own code
(not from what the gateway says) which tools exist and which need approval.
"""

from __future__ import annotations

import hashlib
from typing import Any

from fastmcp import Client

from .policy import check_tool_names, parse_policy

APPROVAL_TAG = "requires-approval"
DEFAULT_APPROVAL_ENVIRONMENT = "mcp-approval"


def hash_client_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _tags(tool: dict[str, Any]) -> list[str]:
    return ((tool.get("_meta") or {}).get("_fastmcp") or {}).get("tags") or []


async def build_manifest(
    server: Any,
    approval_environment: str = DEFAULT_APPROVAL_ENVIRONMENT,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = policy or parse_policy({})
    async with Client(server) as client:
        tools = await client.list_tools()
        init = client.initialize_result

    all_tools = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools]
    check_tool_names(policy, {t["name"] for t in all_tools})

    allow = policy["allow"]
    published = [t for t in all_tools if allow is None or t["name"] in allow]
    gated = {t["name"] for t in published if APPROVAL_TAG in _tags(t)} | set(policy["require_approval"])

    server_info = init.serverInfo if init else None
    return {
        "server_name": server_info.name if server_info else getattr(server, "name", "fastmcp"),
        "server_version": server_info.version if server_info else None,
        "instructions": init.instructions if init else None,
        "tools": published,
        # These tools only run after someone approves the job in this GitHub
        # Environment. Both the gateway and the runner enforce it.
        "approval_required": sorted(gated),
        "approval_environment": approval_environment,
        "policy": {"limits": policy["limits"]},
    }
