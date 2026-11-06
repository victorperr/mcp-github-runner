"""Structured audit log: one JSON line per security-relevant event.

Never log arguments, results or tokens here: only identifiers and decisions.
By default the lines go to stdout (``docker logs``, your platform's log viewer).
Set GATEWAY_AUDIT_LOG=off to route the ``mcp_github_runner.audit`` logger with
your own logging configuration instead.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

logger = logging.getLogger("mcp_github_runner.audit")


class _StdoutHandler(logging.StreamHandler):
    """Writes to whatever sys.stdout is at the time of each record."""

    @property
    def stream(self):  # type: ignore[override]
        return sys.stdout

    @stream.setter
    def stream(self, value) -> None:
        pass


def enable_stdout() -> None:
    """Print audit lines on stdout, unless the operator already routed this logger.

    Without a handler, the lines went to Python's root logger, which nothing
    configures (uvicorn only sets up its own loggers), so INFO events were dropped.
    """
    if logger.handlers:
        return
    handler = _StdoutHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def audit(event: str, **fields: Any) -> None:
    record = {"ts": round(time.time(), 3), "event": event, **{k: v for k, v in fields.items() if v is not None}}
    logger.info(json.dumps(record, default=str, separators=(",", ":")))
