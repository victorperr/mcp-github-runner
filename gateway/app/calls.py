"""Turns an MCP tool call into a workflow run and waits for its result."""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any, Awaitable, Callable

from .audit import audit
from .dispatch import Dispatcher, DispatchError, RunInfo
from .policy import Code, Denial, Limits, check_quota, check_request, input_schema, tool_spec, validate_arguments
from .schema_check import SchemaChecker
from .store import ServerRecord, Store

Heartbeat = Callable[[float, str], Awaitable[None]]

QUEUED_STATES = {"queued", "requested", "pending"}
NO_RUNNER_HINT_AFTER = 60  # seconds queued before suggesting the runner may be offline


def describe(info: RunInfo | None, approval_env: str | None, elapsed: float) -> str:
    secs = f"{int(elapsed)}s"
    if info is None:
        return f"Waiting for GitHub to start the run ({secs})"
    if info.status == "waiting":
        return f"Waiting for approval in the '{approval_env or 'protected'}' environment: {info.url} ({secs})"
    if info.status in QUEUED_STATES:
        msg = f"Queued, waiting for a self-hosted runner ({secs})"
        if elapsed > NO_RUNNER_HINT_AFTER:
            msg += f". Is a runner with the right labels online? {info.url}"
        return msg
    if info.status == "in_progress":
        return f"Running on the runner ({secs}): {info.url}"
    return f"Run is {info.status} ({secs}): {info.url}"


class CallBroker:
    def __init__(
        self,
        store: Store,
        dispatcher: Dispatcher,
        *,
        ceilings: Limits,
        heartbeat_interval: float,
        status_poll_interval: float = 15,
        schema_checker: SchemaChecker | None = None,
    ):
        self.store = store
        self.dispatcher = dispatcher
        self.ceilings = ceilings
        self.heartbeat_interval = heartbeat_interval
        self.status_poll_interval = status_poll_interval
        # None checks schemas in this process (tests); the app always isolates them.
        self.schema_checker = schema_checker
        self._waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}

    def limits_for(self, server: ServerRecord) -> Limits:
        return Limits.effective(server.manifest, self.ceilings)

    async def _check_arguments(self, server: ServerRecord, tool: str, arguments: dict[str, Any]) -> Denial | None:
        schema = input_schema(tool_spec(server.manifest, tool))
        if self.schema_checker is None:
            return validate_arguments(schema, arguments)
        return await self.schema_checker.check(schema, arguments)

    async def run(
        self,
        server: ServerRecord,
        tool: str,
        arguments: dict[str, Any],
        heartbeat: Heartbeat | None = None,
    ) -> dict[str, Any]:
        limits = self.limits_for(server)
        # Same guard order as policy.evaluate. The schema check runs off the event
        # loop; the quota check runs after it, with no await before create_call,
        # so concurrent calls can't both slip under the limits.
        denial = (
            check_request(server.manifest, tool, arguments, limits=limits)
            or await self._check_arguments(server, tool, arguments)
            or check_quota(
                limits=limits,
                calls_last_hour=self.store.count_calls_since(server.repository_id, time.time() - 3600),
                active_calls=self.store.count_active_calls(server.repository_id),
            )
        )
        if denial is not None:
            audit("call.denied", repo=server.full_name, tool=tool, code=int(denial.code), reason=denial.reason)
            return denial.to_result()

        approval_env = server.approval_environment_for(tool)
        timeout = limits.approval_timeout_seconds if approval_env else limits.call_timeout_seconds
        call_id = secrets.token_urlsafe(16)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters[call_id] = future
        self.store.create_call(call_id, server.repository_id, tool, arguments, approval_env)
        audit("call.created", repo=server.full_name, call_id=call_id, tool=tool, approval_env=approval_env)
        try:
            try:
                await self.dispatcher.dispatch(server, call_id, approval_env)
            except DispatchError as exc:
                self.store.finish_call(call_id, "failed", from_status=("pending",))
                code = Code.APPROVAL_UNAVAILABLE if exc.approval else Code.DISPATCH_FAILED
                audit("call.dispatch_failed", repo=server.full_name, call_id=call_id, code=int(code), reason=str(exc))
                return Denial(code, str(exc)).to_result()
            audit("call.dispatched", repo=server.full_name, call_id=call_id)

            if heartbeat is not None and approval_env:
                await heartbeat(0, f"'{tool}' needs approval in GitHub before it runs")

            start = time.monotonic()
            last_poll = -self.status_poll_interval
            info: RunInfo | None = None
            while True:
                elapsed = time.monotonic() - start
                remaining = timeout - elapsed
                if remaining <= 0:
                    self.store.finish_call(call_id, "timeout", from_status=("pending", "running"))
                    audit("call.timeout", repo=server.full_name, call_id=call_id)
                    hint = f" Still waiting for approval? {info.url}" if info and info.status == "waiting" else ""
                    return Denial(
                        Code.TIMEOUT,
                        f"No result after {int(timeout)}s. Check the Actions tab of {server.full_name}.{hint}",
                    ).to_result()
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(future), timeout=min(self.heartbeat_interval, remaining)
                    )
                except asyncio.TimeoutError:
                    pass

                elapsed = time.monotonic() - start
                if elapsed - last_poll >= self.status_poll_interval:
                    last_poll = elapsed
                    info = await self.dispatcher.run_status(server, call_id)
                    if info is not None and info.status == "completed":
                        # The result may still be on its way; give it a moment.
                        try:
                            return await asyncio.wait_for(asyncio.shield(future), timeout=3)
                        except asyncio.TimeoutError:
                            self.store.finish_call(call_id, "failed", from_status=("pending", "running"))
                            audit("call.run_ended", repo=server.full_name, call_id=call_id, conclusion=info.conclusion)
                            why = " Was the approval rejected?" if approval_env else ""
                            return Denial(
                                Code.RUN_ENDED_WITHOUT_RESULT,
                                f"The run ended ({info.conclusion}) without returning a result.{why} {info.url}",
                            ).to_result()
                if heartbeat is not None:
                    await heartbeat(elapsed, describe(info, approval_env, elapsed))
        finally:
            self._waiters.pop(call_id, None)
            self.dispatcher.forget(call_id)
            if not future.done():
                future.cancel()

    def complete(self, call_id: str, result: dict[str, Any]) -> bool:
        """Called by the runner API. Returns False if nobody is waiting any more."""
        future = self._waiters.get(call_id)
        if future is None or future.done():
            return False
        future.set_result(result)
        return True
