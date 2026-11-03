"""Staging fixture server. Deploy it in a dedicated test repo (see docs/testing.md, level 3).

Each tool exists to prove one property end to end on real GitHub.
"""

import os
import time
from pathlib import Path

from fastmcp import FastMCP

mcp = FastMCP("staging-server")


@mcp.tool
def add(a: int, b: int) -> int:
    """S02/S03: basic round trip and latency."""
    return a + b


@mcp.tool
def echo_env(name: str) -> str:
    """S06: returns an env var's value; secrets must come back redacted."""
    return f"value={os.environ.get(name, '<unset>')}"


@mcp.tool
def env_keys() -> list[str]:
    """S07: names (not values) of env vars visible to tool code."""
    return sorted(os.environ)


@mcp.tool
def git_credentials_present() -> bool:
    """S08: True if actions/checkout left an auth header in .git/config."""
    config = Path(".git/config")
    return config.exists() and "extraheader" in config.read_text().lower()


@mcp.tool
def big(n: int) -> str:
    """S10: returns n bytes, to hit max_result_bytes."""
    return "x" * n


@mcp.tool
def sleep(seconds: int) -> str:
    """S14: a slow tool, for timeout and progress behaviour."""
    time.sleep(min(seconds, 600))
    return f"slept {seconds}s"


@mcp.tool(tags={"requires-approval"})
def gated_action(label: str) -> str:
    """S04/S05: only runs after approval in the mcp-approval environment."""
    return f"approved action ran: {label}"
