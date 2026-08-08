"""Unit tests for RentalSessionHandler teardown reconciliation.

Regression: after an agent restart, self._active is empty. A session that
then goes 'stopping' must still have its container (deterministic name
hadcd-session-<sid8>) reaped and be reported stopped — previously the agent
skipped any stopping session it hadn't started itself, orphaning the
container forever.
"""

from __future__ import annotations

import sys
import types

from agent.rental_session_handler import RentalSessionHandler


# ---- fake docker -----------------------------------------------------


class _FakeContainer:
    def __init__(self, cid: str) -> None:
        self.id = cid
        self.stopped = False
        self.removed = False

    def stop(self, timeout: int = 10) -> None:
        self.stopped = True

    def remove(self) -> None:
        self.removed = True


class _FakeContainers:
    def __init__(self, containers: list[_FakeContainer]) -> None:
        self._containers = containers
        self.list_filters: list[dict] = []

    def list(self, all: bool = False, filters: dict | None = None):
        self.list_filters.append(filters or {})
        return list(self._containers)


class _FakeDockerClient:
    def __init__(self, containers: list[_FakeContainer]) -> None:
        self.containers = _FakeContainers(containers)


def _install_fake_docker(monkeypatch, containers: list[_FakeContainer]) -> _FakeDockerClient:
    client = _FakeDockerClient(containers)
    module = types.ModuleType("docker")
    module.from_env = lambda: client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "docker", module)
    return client


def _handler() -> RentalSessionHandler:
    return RentalSessionHandler(
        node_id="node-1",
        dispatcher_url="http://disp:8000",
        node_token="tok",
    )


def test_handler_uses_configured_ollama_image() -> None:
    h = RentalSessionHandler(
        node_id="node-1",
        dispatcher_url="http://disp:8000",
        node_token="tok",
        ollama_image="ollama/ollama@sha256:test-digest",
    )
    assert h._ollama_image == "ollama/ollama@sha256:test-digest"


# ---- tests -----------------------------------------------------------


async def test_orphaned_stopping_session_is_reaped(monkeypatch):
    sid = "3d1e26da-215e-4c1d-b287-f24beb5a964a"
    container = _FakeContainer("cid123456789")
    client = _install_fake_docker(monkeypatch, [container])

    posted: list[str] = []
    h = _handler()

    async def _fake_post_stopped(session_id: str) -> None:
        posted.append(session_id)

    monkeypatch.setattr(h, "_post_stopped", _fake_post_stopped)

    # sid NOT in self._active — the orphaned-across-restart case.
    await h.handle_sessions(
        [{"session_id": sid, "status": "stopping", "type": "api_endpoint"}]
    )

    assert container.stopped and container.removed
    assert posted == [sid]
    # Looked the container up by its deterministic name.
    assert client.containers.list_filters[0]["name"] == f"hadcd-session-{sid[:8]}"
    # In-flight guard released after teardown.
    assert sid not in h._stopping


async def test_stopping_session_deduped_by_inflight_guard(monkeypatch):
    sid = "abcd1234-0000-0000-0000-000000000000"
    _install_fake_docker(monkeypatch, [])
    h = _handler()
    h._stopping.add(sid)  # a teardown is already in flight

    called: list[str] = []

    async def _fake_stop(session_id: str) -> None:
        called.append(session_id)

    monkeypatch.setattr(h, "_stop_session", _fake_stop)

    await h.handle_sessions(
        [{"session_id": sid, "status": "stopping", "type": "api_endpoint"}]
    )

    assert called == []  # guard prevented a duplicate teardown
