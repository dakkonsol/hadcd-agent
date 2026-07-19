"""The task-handler registration mechanism.

Separated from the handlers themselves so that this module has no
domain-specific imports — the agent and backend can both import the
registration API without pulling in handler code they may not need.

Importing the parent package (`hadcd_workloads`) is what triggers the
built-in handlers' @register decorators; importing only this module
gives you an empty registry to register against.
"""

from __future__ import annotations

from collections.abc import Callable

# A handler: args dict in, result dict out.
TaskHandler = Callable[[dict], dict]

_REGISTRY: dict[str, TaskHandler] = {}


class UnknownTaskType(Exception):
    """Raised when a payload names a task_type with no registered handler."""


def register(name: str) -> Callable[[TaskHandler], TaskHandler]:
    """Decorator: register `fn` as the handler for `name`."""

    def decorator(fn: TaskHandler) -> TaskHandler:
        if name in _REGISTRY:
            raise ValueError(f"task_type '{name}' is already registered")
        _REGISTRY[name] = fn
        return fn

    return decorator


def registered_types() -> list[str]:
    """All registered task_type names, sorted."""
    return sorted(_REGISTRY)


def run_registered(task_type: str | None, args: dict | None) -> dict:
    """Look up and run the handler for `task_type`.

    Raises UnknownTaskType if the type is missing or not registered.
    The handler's return value is coerced to a dict.
    """
    if not task_type:
        raise UnknownTaskType("payload is missing 'task_type'")
    handler = _REGISTRY.get(task_type)
    if handler is None:
        raise UnknownTaskType(
            f"unknown task_type '{task_type}'; "
            f"registered: {registered_types()}"
        )
    result = handler(args or {})
    if not isinstance(result, dict):
        result = {"value": result}
    return result
