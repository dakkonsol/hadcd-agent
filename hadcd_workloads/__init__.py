"""Shared task handler registry — the workload interface.

This is the one piece of code with two real consumers: the central
server's local executor (`backend/app/executor/worker.py`), and the
node agent (`agent/executor.py`). A task's payload names a
`task_type`; this registry maps that name to a Python callable.

Real workloads plug in by importing `register` and decorating a new
handler; the registry pattern keeps the security property intact —
**only registered handlers ever run**, never arbitrary code from a
payload.

Importing this package auto-registers the built-in handlers
(`sleep`, `cpu_burn`, `fib`, `matrix_multiply`).
"""

from hadcd_workloads.registry import (
    UnknownTaskType,
    register,
    registered_types,
    run_registered,
)

# Importing the handlers module fires its @register decorators.
# Suppress F401 — the import is a side-effect import.
from hadcd_workloads import handlers as _handlers  # noqa: F401
from hadcd_workloads import container as _container  # noqa: F401
from hadcd_workloads import gpu_mining_fill as _gpu_mining_fill  # noqa: F401
from hadcd_workloads import p2pool_fill as _p2pool_fill  # noqa: F401
from hadcd_workloads import synthetic_heat_fill as _synthetic_heat_fill  # noqa: F401

# Optional, operator-fleet-only handler: disposable remote-desktop
# (Sunshine/Moonlight) sandbox sessions. It ships ONLY in the private
# operator-fleet build — the public agent omits sandbox_handler.py so
# public/independent nodes cannot offer desktop sessions at all (and the
# dispatcher already restricts sandbox tasks to operator-owned nodes).
# We import it only when the module file is actually present, so this
# __init__ stays byte-identical across the private and public repos and
# a genuine import error inside the handler is NOT silently swallowed.
import importlib.util as _importlib_util

if _importlib_util.find_spec("hadcd_workloads.sandbox_handler") is not None:
    from hadcd_workloads import sandbox_handler as _sandbox_handler  # noqa: F401

__all__ = [
    "UnknownTaskType",
    "register",
    "registered_types",
    "run_registered",
]
