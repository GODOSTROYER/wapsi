"""Streamlit version shims so one codebase runs on 1.38 (the oldest the ``ui`` extra allows) and current releases.

Newer Streamlit replaced ``use_container_width=True`` with ``width="stretch"``; old versions only know the former.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from functools import lru_cache
from typing import Any


@lru_cache(maxsize=64)
def _params(fn: Callable[..., Any]) -> dict[str, inspect.Parameter]:
    try:
        return dict(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return {}


def stretch(fn: Callable[..., Any]) -> dict[str, Any]:
    """Keyword args that make ``fn`` fill its container on this Streamlit version."""
    params = _params(fn)
    width = params.get("width")
    if width is not None and isinstance(width.default, str):
        return {"width": "stretch"}
    if "use_container_width" in params:
        return {"use_container_width": True}
    return {}


def supports(fn: Callable[..., Any], name: str) -> bool:
    return name in _params(fn)


def optional(fn: Callable[..., Any], **kwargs: Any) -> dict[str, Any]:
    """Only the kwargs this Streamlit version understands (e.g. ``shortcut``, ``icon``)."""
    params = _params(fn)
    return {k: v for k, v in kwargs.items() if k in params and v is not None}
