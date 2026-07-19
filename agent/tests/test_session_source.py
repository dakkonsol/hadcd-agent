"""Unit tests for the interactive-session-source adapters (Phase 10g).

All tests follow the same fail-quiet contract as the heat-source tests:
on any error, `is_active()` must not raise — it returns the last known
value (or False if no successful reading has ever been taken) and logs.

The Sunshine adapter is tested with httpx.MockTransport so no real
Sunshine or network is required.
"""

from __future__ import annotations

import pytest
import httpx

from agent.config import AgentSettings
from agent.session_source import (
    NullSessionSource,
    SessionConfigError,
    SunshineSessionSource,
    build_session_source,
)


# ---- helpers --------------------------------------------------------


def _sunshine_source_with_handler(handler) -> SunshineSessionSource:
    """Build a SunshineSessionSource backed by httpx MockTransport."""
    src = SunshineSessionSource(
        base_url="https://localhost:47990",
        username="sunshine",
        password="secret",
        timeout_sec=5.0,
    )
    transport = httpx.MockTransport(handler)
    # Replace the real (TLS-skipping) client with a mock-transport one.
    src._client = httpx.AsyncClient(transport=transport)
    return src


# ---- NullSessionSource ----------------------------------------------


@pytest.mark.asyncio
async def test_null_source_always_returns_false():
    src = NullSessionSource()
    assert await src.is_active() is False
    # Multiple calls all return False.
    assert await src.is_active() is False


@pytest.mark.asyncio
async def test_null_source_aclose_is_a_noop():
    src = NullSessionSource()
    await src.aclose()  # must not raise


# ---- SunshineSessionSource — happy paths ----------------------------


@pytest.mark.asyncio
async def test_sunshine_bare_list_active():
    """A non-empty bare list counts as an active session."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"client_id": "laptop"}])

    src = _sunshine_source_with_handler(handler)
    try:
        assert await src.is_active() is True
    finally:
        await src.aclose()


@pytest.mark.asyncio
async def test_sunshine_bare_list_inactive():
    """An empty bare list means no active session."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    src = _sunshine_source_with_handler(handler)
    try:
        assert await src.is_active() is False
    finally:
        await src.aclose()


@pytest.mark.asyncio
async def test_sunshine_envelope_connections_key():
    """Handles a {"connections": [...]} envelope (some Sunshine versions)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"connections": [{"client_id": "tv"}]})

    src = _sunshine_source_with_handler(handler)
    try:
        assert await src.is_active() is True
    finally:
        await src.aclose()


@pytest.mark.asyncio
async def test_sunshine_envelope_clients_key():
    """Handles a {"clients": [...]} envelope."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"clients": [{"id": "tablet"}]})

    src = _sunshine_source_with_handler(handler)
    try:
        assert await src.is_active() is True
    finally:
        await src.aclose()


@pytest.mark.asyncio
async def test_sunshine_empty_envelope():
    """An envelope with an empty list is inactive."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"connections": []})

    src = _sunshine_source_with_handler(handler)
    try:
        assert await src.is_active() is False
    finally:
        await src.aclose()


@pytest.mark.asyncio
async def test_sunshine_hits_correct_url():
    """The adapter calls /api/connections, not some other path."""
    seen_path: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_path.append(request.url.path)
        return httpx.Response(200, json=[])

    src = _sunshine_source_with_handler(handler)
    try:
        await src.is_active()
    finally:
        await src.aclose()

    assert seen_path == ["/api/connections"]


# ---- SunshineSessionSource — transient-failure / fail-quiet ---------


@pytest.mark.asyncio
async def test_sunshine_http_error_returns_last_known():
    """On a 5xx, the last known value is returned (not raised)."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: succeed with an active session.
            return httpx.Response(200, json=[{"client_id": "laptop"}])
        # Second call: Sunshine is down.
        return httpx.Response(503, text="upstream error")

    src = _sunshine_source_with_handler(handler)
    try:
        first = await src.is_active()   # True
        second = await src.is_active()  # 503 → fall back to True
    finally:
        await src.aclose()

    assert first is True
    assert second is True  # held from last known


@pytest.mark.asyncio
async def test_sunshine_connect_error_before_first_success_returns_false():
    """Before any successful reading, a transport failure defaults to False."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    src = _sunshine_source_with_handler(handler)
    try:
        result = await src.is_active()
    finally:
        await src.aclose()

    assert result is False  # default _last_known when no prior success


@pytest.mark.asyncio
async def test_sunshine_transport_error_after_success_holds_true():
    """After a True reading, a transport error returns True (hold state)."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json=[{"id": "phone"}])
        raise httpx.ConnectError("no route to host")

    src = _sunshine_source_with_handler(handler)
    try:
        first = await src.is_active()   # True
        second = await src.is_active()  # error → hold True
    finally:
        await src.aclose()

    assert first is True
    assert second is True


@pytest.mark.asyncio
async def test_sunshine_invalid_json_returns_last_known():
    """A garbled (non-JSON) response is treated as a transient failure."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json=[])  # inactive
        return httpx.Response(200, content=b"not json at all")

    src = _sunshine_source_with_handler(handler)
    try:
        first = await src.is_active()   # False
        second = await src.is_active()  # garbled → hold False
    finally:
        await src.aclose()

    assert first is False
    assert second is False


@pytest.mark.asyncio
async def test_sunshine_recovery_after_failure():
    """When Sunshine comes back after a failure the flag updates correctly."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json=[])       # inactive
        if call_count == 2:
            return httpx.Response(503, text="down")   # transient error
        return httpx.Response(200, json=[{"id": "tv"}])  # active again

    src = _sunshine_source_with_handler(handler)
    try:
        r1 = await src.is_active()  # False
        r2 = await src.is_active()  # 503 → hold False
        r3 = await src.is_active()  # True
    finally:
        await src.aclose()

    assert r1 is False
    assert r2 is False
    assert r3 is True


# ---- SunshineSessionSource — state tracking -------------------------


@pytest.mark.asyncio
async def test_sunshine_updates_last_known_on_success():
    """_last_known flips correctly as the session state changes."""
    toggle = [False]

    def handler(request: httpx.Request) -> httpx.Response:
        active = toggle[0]
        toggle[0] = not toggle[0]
        return httpx.Response(200, json=[{"id": "x"}] if active else [])

    src = _sunshine_source_with_handler(handler)
    try:
        assert await src.is_active() is False  # toggle[0] was False
        assert await src.is_active() is True   # toggle[0] was True
        assert await src.is_active() is False  # toggle[0] was False
    finally:
        await src.aclose()


# ---- build_session_source factory -----------------------------------


def test_build_none_returns_null_source():
    settings = AgentSettings(enrollment_tokens="x", session_source="none")
    src = build_session_source(settings)
    assert isinstance(src, NullSessionSource)


def test_build_empty_returns_null_source():
    settings = AgentSettings(enrollment_tokens="x", session_source="")
    src = build_session_source(settings)
    assert isinstance(src, NullSessionSource)


def test_build_default_returns_null_source():
    """When SESSION_SOURCE is not set, the default is null."""
    settings = AgentSettings(enrollment_tokens="x")
    src = build_session_source(settings)
    assert isinstance(src, NullSessionSource)


def test_build_sunshine_requires_password():
    settings = AgentSettings(
        enrollment_tokens="x",
        session_source="sunshine",
        sunshine_password="",
    )
    with pytest.raises(SessionConfigError, match="SUNSHINE_PASSWORD"):
        build_session_source(settings)


def test_build_sunshine_with_password_returns_sunshine_source():
    settings = AgentSettings(
        enrollment_tokens="x",
        session_source="sunshine",
        sunshine_url="https://localhost:47990",
        sunshine_username="sunshine",
        sunshine_password="admin",
    )
    src = build_session_source(settings)
    try:
        assert isinstance(src, SunshineSessionSource)
        assert "/api/connections" in src.connections_url
    finally:
        import asyncio
        asyncio.run(src.aclose())


def test_build_unknown_kind_raises():
    settings = AgentSettings(
        enrollment_tokens="x",
        session_source="obs-studio",
    )
    with pytest.raises(SessionConfigError, match="unknown SESSION_SOURCE"):
        build_session_source(settings)
