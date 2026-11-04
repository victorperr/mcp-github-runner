"""Check tool arguments against tenant schemas in separate processes, with a deadline.

Tool schemas come from the repos that register with the gateway. A schema with a
catastrophic regex (``"pattern": "(a+)+$"``) or an expensive keyword
(``uniqueItems`` on a large array of objects) could otherwise hold the CPU and
the event loop for every tenant. Each check runs in a small worker process that
is killed when it goes over the deadline; Python can't stop a running regex or
thread any other way.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import threading
from multiprocessing.connection import Connection
from typing import Any

from .policy import Code, Denial, validate_arguments

logger = logging.getLogger(__name__)

READY = "ready"
START_TIMEOUT = 60  # seconds for a new worker to import its modules


def _serve(conn: Connection) -> None:  # pragma: no cover - runs in the child process
    conn.send(READY)
    while True:
        try:
            schema, arguments = conn.recv()
        except (EOFError, OSError):
            return
        denial = validate_arguments(schema, arguments)
        conn.send(None if denial is None else (int(denial.code), denial.reason))


class _Worker:
    def __init__(self, ctx: Any):
        self.conn, child = ctx.Pipe()
        self.proc = ctx.Process(target=_serve, args=(child,), daemon=True, name="mcp-schema-check")
        self.proc.start()
        child.close()
        if not self.conn.poll(START_TIMEOUT) or self.conn.recv() != READY:
            self.kill()
            raise RuntimeError("schema check worker did not start")

    def kill(self) -> None:
        if self.proc.is_alive():
            self.proc.kill()
        self.proc.join(5)
        self.conn.close()


class SchemaChecker:
    """Runs ``policy.validate_arguments`` in up to ``workers`` processes, ``timeout`` seconds each."""

    def __init__(self, *, timeout: float = 2.0, workers: int = 2):
        self.timeout = timeout
        self._ctx = multiprocessing.get_context("spawn")  # no forked copy of the gateway's state
        self._slots = threading.BoundedSemaphore(workers)
        self._idle: list[_Worker] = []
        self._lock = threading.Lock()
        self._closed = False

    async def check(self, schema: dict[str, Any], arguments: dict[str, Any]) -> Denial | None:
        return await asyncio.to_thread(self._check, schema, arguments)

    def _check(self, schema: dict[str, Any], arguments: dict[str, Any]) -> Denial | None:
        with self._slots:
            with self._lock:
                worker = self._idle.pop() if self._idle else None
            try:
                worker = worker or _Worker(self._ctx)
                worker.conn.send((schema, arguments))
                if not worker.conn.poll(self.timeout):
                    logger.warning("argument check went over %.1fs; worker killed", self.timeout)
                    worker.kill()
                    worker = None
                    return Denial(Code.ARGUMENTS_INVALID, f"checking the arguments took over {self.timeout:g}s")
                answer = worker.conn.recv()
            except (EOFError, OSError, RuntimeError) as exc:
                logger.warning("argument check failed: %s", exc)
                if worker is not None:
                    worker.kill()
                    worker = None
                return Denial(Code.ARGUMENTS_INVALID, "the arguments could not be checked")
            finally:
                if worker is not None:
                    with self._lock:
                        if self._closed:
                            worker.kill()
                        else:
                            self._idle.append(worker)
        return None if answer is None else Denial(Code(answer[0]), answer[1])

    def close(self) -> None:
        with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
        for worker in idle:
            worker.kill()
