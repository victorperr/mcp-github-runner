"""Optional ``mcp-runner.toml`` policy file in the server repo.

Validation is strict, like gh-aw's: unknown keys, wrong types, or names of
tools that don't exist are errors, never ignored. Missing settings fall back to
safe defaults, so a partial file can't switch protections off.

    [tools]
    allow = ["add", "internal_api_get", "restart_service"]  # omit = all tools; [] = none
    require_approval = ["restart_service"]                  # added to tools tagged "requires-approval"

    [limits]
    max_calls_per_hour = 120
    max_concurrent_calls = 5
    max_argument_bytes = 65536
    max_result_bytes = 1048576
    call_timeout_seconds = 300
    approval_timeout_seconds = 900
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

DEFAULT_POLICY_FILE = "mcp-runner.toml"

DEFAULT_LIMITS: dict[str, int] = {
    "max_calls_per_hour": 120,
    "max_concurrent_calls": 5,
    "max_argument_bytes": 64 * 1024,
    "max_result_bytes": 1024 * 1024,
    "call_timeout_seconds": 300,
    "approval_timeout_seconds": 900,
}
# Upper bounds accepted from the file. The gateway applies its own, possibly lower, ceilings.
MAX_LIMITS: dict[str, int] = {
    "max_calls_per_hour": 10_000,
    "max_concurrent_calls": 50,
    "max_argument_bytes": 1024 * 1024,
    "max_result_bytes": 4 * 1024 * 1024,
    "call_timeout_seconds": 3600,
    "approval_timeout_seconds": 24 * 3600,
}
EXAMPLE = """Example mcp-runner.toml:

[tools]
allow = ["add", "restart_service"]
require_approval = ["restart_service"]

[limits]
max_calls_per_hour = 120
"""


class PolicyError(Exception):
    pass


def _string_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise PolicyError(f"{where} must be a list of tool names.\n\n{EXAMPLE}")
    if len(set(value)) != len(value):
        raise PolicyError(f"{where} contains duplicate names")
    return list(value)


def parse_policy(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = set(raw) - {"tools", "limits"}
    if unknown:
        raise PolicyError(f"Unknown section(s) in policy: {sorted(unknown)}.\n\n{EXAMPLE}")

    tools = raw.get("tools", {})
    if not isinstance(tools, dict):
        raise PolicyError(f"[tools] must be a table.\n\n{EXAMPLE}")
    unknown = set(tools) - {"allow", "require_approval"}
    if unknown:
        raise PolicyError(f"Unknown key(s) in [tools]: {sorted(unknown)}.\n\n{EXAMPLE}")

    limits_raw = raw.get("limits", {})
    if not isinstance(limits_raw, dict):
        raise PolicyError(f"[limits] must be a table.\n\n{EXAMPLE}")
    unknown = set(limits_raw) - set(DEFAULT_LIMITS)
    if unknown:
        raise PolicyError(f"Unknown key(s) in [limits]: {sorted(unknown)}. Valid keys: {sorted(DEFAULT_LIMITS)}")

    limits = dict(DEFAULT_LIMITS)
    for key, value in limits_raw.items():
        # bool is an int subclass in Python; reject it explicitly.
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise PolicyError(f"limits.{key} must be a positive integer")
        if value > MAX_LIMITS[key]:
            raise PolicyError(f"limits.{key} must be at most {MAX_LIMITS[key]}, got {value}")
        limits[key] = value

    return {
        # None means "all tools"; an empty list means "no tools" (fail closed).
        "allow": _string_list(tools["allow"], "tools.allow") if "allow" in tools else None,
        "require_approval": _string_list(tools.get("require_approval", []), "tools.require_approval"),
        "limits": limits,
    }


def load_policy(path: str | Path = DEFAULT_POLICY_FILE) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return parse_policy({})
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"{p.name} is not valid TOML: {exc}") from exc
    return parse_policy(raw)


def check_tool_names(policy: dict[str, Any], tool_names: set[str]) -> None:
    """Names in the policy must exist, so a typo can't silently skip approval."""
    for key in ("allow", "require_approval"):
        names = policy.get(key) or []
        unknown = sorted(set(names) - tool_names)
        if unknown:
            raise PolicyError(f"tools.{key} lists tools the server doesn't have: {unknown}")
    if policy["allow"] is not None:
        hidden = sorted(set(policy["require_approval"]) - set(policy["allow"]))
        if hidden:
            raise PolicyError(f"tools.require_approval lists tools that tools.allow hides: {hidden}")
