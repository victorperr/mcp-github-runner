"""Example FastMCP server. Each tool call runs on one of your self-hosted runners,
so tools can reach services on your private network."""

import os
import platform

import httpx
from fastmcp import FastMCP

mcp = FastMCP("example-server", instructions="Internal tools running on our self-hosted GitHub runners.")


@mcp.tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@mcp.tool
def runner_info() -> dict:
    """Describe the machine this call ran on."""
    return {
        "os": platform.platform(),
        "python": platform.python_version(),
        "runner": os.environ.get("RUNNER_NAME", "local"),
        "run_url": (
            f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
            f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
            if "GITHUB_RUN_ID" in os.environ
            else None
        ),
    }


@mcp.tool(annotations={"readOnlyHint": True})
def internal_api_get(path: str) -> dict:
    """GET a path on the internal API (only reachable from inside the network).

    INTERNAL_API_URL and INTERNAL_API_TOKEN come from repo secrets, passed in mcp.yml.
    """
    base = os.environ.get("INTERNAL_API_URL")
    if not base:
        raise ValueError("INTERNAL_API_URL is not set; add it as a repo secret and pass it in mcp.yml")
    if not path.startswith("/") or ".." in path:
        raise ValueError("path must be an absolute path like /status")
    headers = {}
    if token := os.environ.get("INTERNAL_API_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    resp = httpx.get(base.rstrip("/") + path, headers=headers, timeout=30)
    return {"status": resp.status_code, "body": resp.text[:10_000]}


# Tagged tools only run after a reviewer approves the job in the
# "mcp-approval" GitHub Environment (see docs/self-hosted.md).
@mcp.tool(tags={"requires-approval"}, annotations={"destructiveHint": True})
def restart_service(name: str) -> str:
    """Restart an internal service. Needs approval in GitHub before it runs."""
    # Example only: replace with your real command, e.g.
    # subprocess.run(["systemctl", "restart", name], check=True)
    return f"(example) would restart {name}"


if __name__ == "__main__":
    mcp.run()
