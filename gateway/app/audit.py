"""Structured audit log: one JSON line per security-relevant event.

Never log arguments, results or tokens here: only identifiers and decisions.
Send the ``mcp_github_runner.audit`` logger to your log pipeline.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger("mcp_github_runner.audit")


def audit(event: str, **fields: Any) -> None:
    record = {"ts": round(time.time(), 3), "event": event, **{k: v for k, v in fields.items() if v is not None}}
    logger.info(json.dumps(record, default=str, separators=(",", ":")))
