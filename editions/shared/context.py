"""The services the agent's tools and plugins use, bound by the edition that hosts the agent.

A services object is anything with ``settings`` (``.demo_mode``), ``ledger``, ``store``, ``llm``, ``guard``,
``search``, ``sink``, ``cfg`` and ``lock`` (optionally ``scorer``): ``editions.gcp.app.services.Services`` and
``editions.amd.app.services.Services`` both qualify.

One slot per process, because one edition hosts the agent in a process:

- the Google Cloud edition's ``services.get_services`` / ``set_services`` read and write this slot directly;
- the AMD edition keeps its own services for the pipeline API and binds them here when it builds or runs the
  agent (``AgentRuntime`` re-binds before every run, so a process that hosts both editions, such as the test
  suite, always gives the tools the services of the agent that is running).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

_services: Any | None = None
_lock = threading.Lock()


def get_services(factory: Callable[[], Any] | None = None) -> Any:
    """The bound services; when none are bound, ``factory`` builds (and binds) them."""
    global _services
    if _services is None:
        with _lock:
            if _services is None:
                if factory is None:
                    raise RuntimeError("no agent services are bound: call editions.shared.context.set_services() "
                                       "(the edition's agent runtime does this)")
                _services = factory()
    return _services


def set_services(services: Any | None) -> None:
    """Bind (or clear, with ``None``) the process-wide services."""
    global _services
    with _lock:
        _services = services


def peek_services() -> Any | None:
    """The bound services without building any (``None`` when the slot is empty)."""
    return _services
