"""Gateway access policy for tool calls, modelled on gh-aw's MCP access guards.

Guards run in a fixed order and the first failing guard picks the denial code:

    1. tool        the tool is published in the manifest
    2. size        the arguments fit max_argument_bytes
    3. schema      the arguments match the tool's inputSchema
    4. rate        fewer than max_calls_per_hour calls in the last hour
    5. concurrency fewer than max_concurrent_calls calls in flight

``evaluate`` is a pure function so the rules can be tested exhaustively. The
broker runs the same three stages separately (``check_request``,
``validate_arguments`` in an isolated process, then ``check_quota``), because
schemas come from tenants and checking them can be slow.
Limits come from the repo's mcp-runner.toml but are clamped to the gateway's
ceilings: a repo can tighten them, never loosen them past the operator's caps.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from enum import IntEnum
from functools import lru_cache
from typing import Any

import jsonschema
from jsonschema.exceptions import best_match


class Code(IntEnum):
    # Gateway guards (evaluated before dispatch, in this order)
    TOOL_NOT_FOUND = -32001
    ARGUMENTS_TOO_LARGE = -32002
    ARGUMENTS_INVALID = -32003
    RATE_LIMITED = -32004
    TOO_MANY_CONCURRENT = -32005
    # Dispatch and run outcomes
    APPROVAL_UNAVAILABLE = -32006
    DISPATCH_FAILED = -32007
    TIMEOUT = -32008
    RUN_ENDED_WITHOUT_RESULT = -32009
    # -32011..-32013 are used by the runner (mcp_runner/__main__.py)


@dataclass(frozen=True)
class Limits:
    max_calls_per_hour: int = 120
    max_concurrent_calls: int = 5
    max_argument_bytes: int = 64 * 1024
    max_result_bytes: int = 1024 * 1024
    call_timeout_seconds: int = 300
    approval_timeout_seconds: int = 900

    @classmethod
    def names(cls) -> set[str]:
        return {f.name for f in fields(cls)}

    @classmethod
    def effective(cls, manifest: dict[str, Any] | None, ceilings: "Limits") -> "Limits":
        """Repo limits clamped to the gateway's ceilings. Missing or bad values use the defaults."""
        requested = ((manifest or {}).get("policy") or {}).get("limits") or {}
        defaults = cls()
        values = {}
        for name in cls.names():
            value = requested.get(name, getattr(defaults, name))
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                value = getattr(defaults, name)  # fail safe: never "unlimited"
            values[name] = min(value, getattr(ceilings, name))
        return cls(**values)


@dataclass(frozen=True)
class Denial:
    code: Code
    reason: str

    def to_result(self) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": f"Denied ({self.code.name.lower()}): {self.reason}"}],
            "isError": True,
            "_meta": {"mcp-github-runner/denial": {"code": int(self.code), "by": "gateway", "reason": self.reason}},
        }


def tool_spec(manifest: dict[str, Any] | None, name: Any) -> dict[str, Any] | None:
    if not isinstance(name, str) or not name:
        return None
    for tool in (manifest or {}).get("tools") or []:
        if tool.get("name") == name:
            return tool
    return None


def input_schema(spec: dict[str, Any]) -> dict[str, Any]:
    return spec.get("inputSchema") or {"type": "object"}


def check_request(manifest: dict[str, Any] | None, tool: Any, arguments: Any, *, limits: Limits) -> Denial | None:
    """Guards 1 (tool) and 2 (size). Cheap."""
    # 1. tool
    if tool_spec(manifest, tool) is None:
        return Denial(Code.TOOL_NOT_FOUND, f"unknown tool '{tool}'")

    # 2. size
    if not isinstance(arguments, dict):
        return Denial(Code.ARGUMENTS_INVALID, "arguments must be a JSON object")
    size = len(json.dumps(arguments, separators=(",", ":")).encode())
    if size > limits.max_argument_bytes:
        return Denial(Code.ARGUMENTS_TOO_LARGE, f"arguments are {size} bytes; the limit is {limits.max_argument_bytes}")
    return None


@lru_cache(maxsize=512)
def _validator(schema_json: str) -> Any:
    """Checked and compiled validator per schema. Checking a schema against its
    metaschema is the expensive part, so it is done once per distinct schema."""
    schema = json.loads(schema_json)
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> Denial | None:
    """Guard 3 (schema)."""
    try:
        validator = _validator(json.dumps(schema, sort_keys=True, separators=(",", ":")))
    except (jsonschema.SchemaError, TypeError, ValueError):
        # Fail closed: a broken schema can't be used to check anything.
        return Denial(Code.ARGUMENTS_INVALID, "the tool's inputSchema is invalid")
    error = best_match(validator.iter_errors(arguments))
    if error is not None:
        return Denial(Code.ARGUMENTS_INVALID, f"arguments don't match the tool's schema: {error.message}")
    return None


def check_quota(*, limits: Limits, calls_last_hour: int, active_calls: int) -> Denial | None:
    """Guards 4 (rate) and 5 (concurrency)."""
    if calls_last_hour >= limits.max_calls_per_hour:
        return Denial(Code.RATE_LIMITED, f"limit of {limits.max_calls_per_hour} calls per hour reached")
    if active_calls >= limits.max_concurrent_calls:
        return Denial(Code.TOO_MANY_CONCURRENT, f"{active_calls} calls already in progress (limit {limits.max_concurrent_calls})")
    return None


def evaluate(
    manifest: dict[str, Any] | None,
    tool: str,
    arguments: Any,
    *,
    limits: Limits,
    calls_last_hour: int,
    active_calls: int,
) -> Denial | None:
    """Return the first failing guard, or None if the call may be dispatched."""
    return (
        check_request(manifest, tool, arguments, limits=limits)
        or validate_arguments(input_schema(tool_spec(manifest, tool)), arguments)
        or check_quota(limits=limits, calls_last_hour=calls_last_hour, active_calls=active_calls)
    )
