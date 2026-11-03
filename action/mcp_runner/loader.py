"""Load a FastMCP server object from a spec like ``server.py:mcp``."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

DEFAULT_ATTRS = ("mcp", "server", "app")


class LoadError(Exception):
    pass


def _is_fastmcp(obj: Any) -> bool:
    try:
        from fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - fastmcp is a hard dependency
        raise LoadError("fastmcp is not installed in the project environment") from exc
    return isinstance(obj, FastMCP)


def load_server(spec: str, base_dir: str | Path = ".") -> Any:
    """Import ``path/to/file.py[:attr]`` and return the FastMCP instance.

    Without ``:attr`` the first of ``mcp``, ``server`` or ``app`` that is a
    FastMCP instance is used.
    """
    path_part, _, attr = spec.partition(":")
    path = (Path(base_dir) / path_part).resolve()
    if not path.is_file():
        raise LoadError(f"Server file not found: {path}")

    # Let the server import its sibling modules.
    sys.path.insert(0, str(path.parent))

    module_name = f"_mcp_runner_user_{path.stem}"
    module_spec = importlib.util.spec_from_file_location(module_name, path)
    if module_spec is None or module_spec.loader is None:
        raise LoadError(f"Cannot import {path}")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_name] = module
    module_spec.loader.exec_module(module)

    if attr:
        if not hasattr(module, attr):
            raise LoadError(f"{path.name} has no attribute '{attr}'")
        obj = getattr(module, attr)
        if not _is_fastmcp(obj):
            raise LoadError(f"'{attr}' in {path.name} is not a FastMCP instance")
        return obj

    for name in DEFAULT_ATTRS:
        obj = getattr(module, name, None)
        if obj is not None and _is_fastmcp(obj):
            return obj
    raise LoadError(
        f"No FastMCP instance found in {path.name}; "
        f"name it one of {', '.join(DEFAULT_ATTRS)} or use 'file.py:attr'"
    )
