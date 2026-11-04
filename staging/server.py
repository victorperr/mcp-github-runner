"""Staging fixture server. Deploy it in a dedicated test repo (see docs/testing.md, level 3).

Each tool exists to prove one property end to end on real GitHub.
"""

import os
import sys
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


CREDENTIAL_NAMES = (
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_RUNTIME_TOKEN", "MCP_CLIENT_TOKEN",
)


def _ppid(pid: int) -> int:
    # /proc/<pid>/stat: "pid (comm) state ppid ..."; comm may contain spaces, so split after ")".
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])


@mcp.tool
def credential_exposure() -> dict:
    """S14/S15: can tool code find the runner's credentials anywhere it can read?

    Walks from this process up to init and reads each /proc/<pid>/environ (the
    environment a process started with, which unsetenv doesn't change), then
    tries to open the parent's memory. Linux only.
    """
    if not sys.platform.startswith("linux"):
        return {"platform": sys.platform}
    chain, leaks = [], []
    pid = os.getpid()
    while pid > 1 and len(chain) < 20:
        chain.append(pid)
        try:
            environ = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            environ = b""  # not readable: nothing leaks from it
        leaks += [f"{pid}:{name}" for name in CREDENTIAL_NAMES if f"{name}=".encode() in environ]
        try:
            pid = _ppid(pid)
        except (OSError, ValueError, IndexError):
            break
    try:
        with open(f"/proc/{os.getppid()}/mem", "rb"):
            parent_memory_readable = True
    except OSError:
        parent_memory_readable = False
    return {
        "platform": sys.platform,
        "process_chain": chain,
        "environ_leaks": leaks,
        "parent_memory_readable": parent_memory_readable,
    }


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
