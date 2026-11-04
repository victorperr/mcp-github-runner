"""Start a workflow run for a tool call, and look up how that run is doing.

GitHubAppDispatcher: production, uses a GitHub App installation token.
LocalDispatcher: dev only, runs ``python -m mcp_runner call`` on this machine.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
import jwt

from .oidc import DevSigner
from .store import ServerRecord

logger = logging.getLogger(__name__)

RUN_TITLE = "MCP call {call_id}"  # must match run-name in the template workflow
MANIFEST_RUN_TITLE = "MCP manifest"  # run-name of a dispatch run with no call_id
ENV_CHECK_TTL = 300


class DispatchError(Exception):
    def __init__(self, message: str, *, approval: bool = False):
        super().__init__(message)
        self.approval = approval  # True when the approval environment is missing or unprotected


@dataclass
class RunInfo:
    status: str  # queued | waiting | in_progress | completed | ...
    conclusion: str | None
    url: str


class Dispatcher(Protocol):
    async def dispatch(self, server: ServerRecord, call_id: str, approval_env: str | None = None) -> None: ...

    async def run_status(self, server: ServerRecord, call_id: str) -> RunInfo | None: ...

    async def verify_run(self, server: ServerRecord, call_id: str, run_id: str) -> bool:
        """True only if ``run_id`` is the run started for ``call_id``. Fails closed."""
        ...

    async def verify_manifest_run(self, server: ServerRecord, run_id: str) -> bool:
        """True only if ``run_id`` is a manual manifest run (no call ID). Fails closed."""
        ...

    async def is_installed(self, server: ServerRecord) -> bool:
        """True if the gateway's GitHub App is installed on the repo. Raises if GitHub can't be asked."""
        ...


class GitHubAppDispatcher:
    def __init__(self, app_id: str, private_key: str, api_url: str = "https://api.github.com"):
        if not app_id or not private_key:
            raise ValueError("GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY are required for the github dispatcher")
        self._app_id = app_id
        self._private_key = private_key
        self._http = httpx.AsyncClient(
            base_url=api_url,
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
            timeout=30,
        )
        self._installations: dict[str, int] = {}
        self._tokens: dict[int, tuple[str, float]] = {}
        self._env_ok: dict[tuple[str, str], float] = {}
        self._run_ids: dict[str, int] = {}

    def _app_jwt(self) -> str:
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self._app_id}, self._private_key, algorithm="RS256")

    async def _installation_token(self, server: ServerRecord) -> str:
        inst_id = self._installations.get(server.repository_id)
        if inst_id is None:
            r = await self._http.get(
                f"/repos/{server.owner}/{server.repo}/installation",
                headers={"Authorization": f"Bearer {self._app_jwt()}"},
            )
            if r.status_code == 404:
                raise DispatchError(
                    f"The MCP GitHub Runner app is not installed on {server.full_name}. Install it, then retry."
                )
            r.raise_for_status()
            inst_id = r.json()["id"]
            self._installations[server.repository_id] = inst_id

        cached = self._tokens.get(inst_id)
        if cached and cached[1] > time.time() + 300:
            return cached[0]
        r = await self._http.post(
            f"/app/installations/{inst_id}/access_tokens",
            headers={"Authorization": f"Bearer {self._app_jwt()}"},
            # Scope the token to this one repo; it gets the App's permissions.
            json={"repository_ids": [int(server.repository_id)]},
        )
        r.raise_for_status()
        body = r.json()
        # Tokens live 1h; refreshing after ~50 min is enough.
        self._tokens[inst_id] = (body["token"], time.time() + 3000)
        return body["token"]

    async def _check_approval_environment(self, server: ServerRecord, env: str, token: str) -> None:
        """Fail closed unless the Environment exists and has required reviewers."""
        key = (server.repository_id, env.lower())
        if self._env_ok.get(key, 0) > time.time():
            return
        r = await self._http.get(
            f"/repos/{server.owner}/{server.repo}/environments/{env}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code == 404:
            raise DispatchError(
                f"This tool needs approval, but {server.full_name} has no '{env}' environment. "
                f"Create it with required reviewers (Settings > Environments).",
                approval=True,
            )
        if r.status_code >= 400:
            raise DispatchError(f"Could not check the '{env}' environment ({r.status_code}): {r.text}", approval=True)
        rules = r.json().get("protection_rules") or []
        has_reviewers = any(rule.get("type") == "required_reviewers" and rule.get("reviewers") for rule in rules)
        if not has_reviewers:
            raise DispatchError(
                f"The '{env}' environment in {server.full_name} has no required reviewers, "
                f"so approval would be skipped. Add reviewers, then retry.",
                approval=True,
            )
        self._env_ok[key] = time.time() + ENV_CHECK_TTL

    async def dispatch(self, server: ServerRecord, call_id: str, approval_env: str | None = None) -> None:
        token = await self._installation_token(server)
        if approval_env:
            await self._check_approval_environment(server, approval_env, token)
        workflow_file = Path(server.workflow_path).name
        ref = server.ref.removeprefix("refs/heads/")
        r = await self._http.post(
            f"/repos/{server.owner}/{server.repo}/actions/workflows/{workflow_file}/dispatches",
            headers={"Authorization": f"Bearer {token}"},
            json={"ref": ref, "inputs": {"call_id": call_id, "approval": "true" if approval_env else "false"}},
        )
        if r.status_code >= 400:
            if r.status_code in (401, 403, 404):
                self._installations.pop(server.repository_id, None)
            raise DispatchError(f"GitHub refused to start the workflow ({r.status_code}): {r.text}")

    async def run_status(self, server: ServerRecord, call_id: str) -> RunInfo | None:
        """Find the run started for this call (by its run-name) and report its state."""
        try:
            token = await self._installation_token(server)
            auth = {"Authorization": f"Bearer {token}"}
            run_id = self._run_ids.get(call_id)
            if run_id is None:
                workflow_file = Path(server.workflow_path).name
                r = await self._http.get(
                    f"/repos/{server.owner}/{server.repo}/actions/workflows/{workflow_file}/runs",
                    params={"event": "workflow_dispatch", "per_page": 30},
                    headers=auth,
                )
                r.raise_for_status()
                title = RUN_TITLE.format(call_id=call_id)
                run = next((w for w in r.json().get("workflow_runs", []) if w.get("display_title") == title), None)
                if run is None:
                    return None
                self._run_ids[call_id] = run["id"]
            else:
                r = await self._http.get(f"/repos/{server.owner}/{server.repo}/actions/runs/{run_id}", headers=auth)
                r.raise_for_status()
                run = r.json()
            if run.get("status") == "completed":
                self._run_ids.pop(call_id, None)
            return RunInfo(status=run["status"], conclusion=run.get("conclusion"), url=run["html_url"])
        except (httpx.HTTPError, DispatchError, KeyError) as exc:
            logger.warning("run status lookup failed for call %s: %s", call_id, exc)
            return None

    async def _run_has_title(self, server: ServerRecord, run_id: str, title: str) -> bool:
        """True only if GitHub says ``run_id`` is a dispatch run of the server's workflow with this title."""
        if not run_id.isdigit():
            return False
        try:
            token = await self._installation_token(server)
            r = await self._http.get(
                f"/repos/{server.owner}/{server.repo}/actions/runs/{run_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code != 200:
                return False
            run = r.json()
        except (httpx.HTTPError, DispatchError, ValueError) as exc:
            logger.warning("run verification failed for run %s: %s", run_id, exc)
            return False
        return (
            run.get("display_title") == title
            and run.get("event") == "workflow_dispatch"
            and run.get("path") == server.workflow_path
            and str(run.get("repository", {}).get("id")) == server.repository_id
        )

    async def verify_run(self, server: ServerRecord, call_id: str, run_id: str) -> bool:
        """Ask GitHub whether this run was started for this call.

        Binds a call to one run: a leaked OIDC token from another run of the same
        workflow can't claim it. Any doubt (API error, mismatch) means no.
        """
        return await self._run_has_title(server, run_id, RUN_TITLE.format(call_id=call_id))

    async def verify_manifest_run(self, server: ServerRecord, run_id: str) -> bool:
        """Ask GitHub whether this dispatch run is a manual manifest upload, not a tool call.

        Tool-call runs are workflow_dispatch runs too; without this check, code
        running in a call job could re-register the server with its own client token.
        """
        return await self._run_has_title(server, run_id, MANIFEST_RUN_TITLE)

    async def is_installed(self, server: ServerRecord) -> bool:
        try:
            await self._installation_token(server)
        except DispatchError:
            return False
        return True


class LocalDispatcher:
    """Runs the call on this machine, as a runner would. For development only.

    Calls that need approval are treated as approved: the fake OIDC token gets
    the environment claim GitHub would add after a reviewer approves.
    """

    def __init__(self, gateway_url: str, audience: str, signer: DevSigner, server_dir: str, server_spec: str):
        self._gateway_url = gateway_url
        self._audience = audience
        self._signer = signer
        self._server_dir = server_dir
        self._server_spec = server_spec
        self._run_counter = 0
        self._runs: dict[str, str] = {}  # call_id -> fake run_id
        self._tasks: set[asyncio.Task] = set()
        self._action_dir = str(Path(__file__).resolve().parents[2] / "action")

    async def dispatch(self, server: ServerRecord, call_id: str, approval_env: str | None = None) -> None:
        self._run_counter += 1
        self._runs[call_id] = f"local-{self._run_counter}"
        if approval_env:
            logger.warning("local dispatcher: auto-approving call %s (environment '%s')", call_id, approval_env)
        token = self._signer.sign(
            audience=self._audience,
            repository=server.full_name,
            repository_id=server.repository_id,
            event_name="workflow_dispatch",
            ref=server.ref,
            workflow_path=server.workflow_path,
            run_id=self._runs[call_id],
            environment=approval_env,
        )
        env = {
            **os.environ,
            "MCP_RUNNER_OIDC_TOKEN": token,
            "PYTHONPATH": self._action_dir,
        }
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "mcp_runner", "call",
            "--call-id", call_id,
            "--gateway", self._gateway_url,
            "--server", self._server_spec,
            cwd=self._server_dir or None,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async def _wait() -> None:
            out, _ = await proc.communicate()
            if proc.returncode != 0:
                logger.error("local runner for call %s failed:\n%s", call_id, out.decode(errors="replace"))

        # Fire and forget, like workflow_dispatch; keep a reference so it isn't garbage collected.
        task = asyncio.create_task(_wait())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run_status(self, server: ServerRecord, call_id: str) -> RunInfo | None:
        return None

    async def verify_run(self, server: ServerRecord, call_id: str, run_id: str) -> bool:
        return self._runs.get(call_id) == run_id

    async def verify_manifest_run(self, server: ServerRecord, run_id: str) -> bool:
        # Dev registration (app.dev) uses push events; no dispatch run is ever a manifest run here.
        return False

    async def is_installed(self, server: ServerRecord) -> bool:
        return True
