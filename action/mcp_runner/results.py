"""MCP ``CallToolResult`` helpers shared by the supervisor and the tool process."""

from __future__ import annotations

from typing import Any

# Denial codes used by the runner. The gateway uses the same numbering (see gateway/app/policy.py).
RUNNER_TOOL_NOT_ALLOWED = -32011
RUNNER_APPROVAL_MISSING = -32012
RUNNER_RESULT_TOO_LARGE = -32013


def error_result(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def refusal(code: int, message: str) -> dict[str, Any]:
    result = error_result(f"Refused by the runner: {message}")
    result["_meta"] = {"mcp-github-runner/denial": {"code": code, "by": "runner", "reason": message}}
    return result
