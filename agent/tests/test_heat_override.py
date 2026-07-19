"""Phase 22 — agent-side building-hub vacancy override.

Exercises Agent._apply_heat_override directly. The method only touches
self._heat_override_active, self.state (AgentState), and self.settings,
so we bind it to a lightweight stub instead of constructing a full
Agent (whose __init__ enrolls, opens HTTP clients, and starts loops).

A FakeHeatSource records setpoint writes and lets a test simulate a
read-only source (set_setpoint -> False).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace


from agent.agent import Agent
from agent.config import AgentSettings
from agent.heat_source import HeatReading
from agent.state import AgentState


class FakeHeatSource:
    """Minimal HeatSource: a fixed reading + recorded setpoint writes."""

    def __init__(self, setpoint_c: float | None = 21.0, writable: bool = True):
        self._setpoint_c = setpoint_c
        self._writable = writable
        self.writes: list[float] = []

    async def read(self) -> HeatReading:
        return HeatReading(
            measured_kw=0.0,
            setpoint_c=self._setpoint_c,
            room_temp_c=19.0,
            expected_window_sec=None,
        )

    async def set_setpoint(self, temp_c: float) -> bool:
        if not self._writable:
            return False
        self.writes.append(temp_c)
        self._setpoint_c = temp_c
        return True


def _make_stub(tmp: Path, **settings_kw) -> SimpleNamespace:
    state = AgentState(str(tmp / "state.json"))
    state.node_id = "00000000-0000-0000-0000-000000000001"
    state.node_token = "node-token"
    state.save()
    settings = AgentSettings(enrollment_tokens="x", **settings_kw)
    return SimpleNamespace(
        _heat_override_active=False,
        state=state,
        settings=settings,
    )


def _apply(stub, override, setback, source):
    asyncio.run(Agent._apply_heat_override(stub, override, setback, source))


def test_setback_drops_to_per_node_temperature():
    with tempfile.TemporaryDirectory() as tmp:
        stub = _make_stub(Path(tmp))
        src = FakeHeatSource(setpoint_c=21.0)

        _apply(stub, "setback", 14.0, src)

        assert stub._heat_override_active is True
        assert src.writes == [14.0]
        assert stub.state.saved_setpoint_c == 21.0


def test_setback_uses_agent_default_when_no_per_node_temp():
    with tempfile.TemporaryDirectory() as tmp:
        stub = _make_stub(Path(tmp), setback_temp_c=12.0)
        src = FakeHeatSource(setpoint_c=20.0)

        _apply(stub, "setback", None, src)

        assert src.writes == [12.0]


def test_clear_restores_saved_setpoint():
    with tempfile.TemporaryDirectory() as tmp:
        stub = _make_stub(Path(tmp))
        src = FakeHeatSource(setpoint_c=21.0)

        _apply(stub, "setback", 14.0, src)
        _apply(stub, None, None, src)

        assert stub._heat_override_active is False
        assert src.writes == [14.0, 21.0]  # dropped, then restored
        assert stub.state.saved_setpoint_c is None


def test_repeated_setback_does_not_re_save_or_rewrite():
    """Idempotent: a second 'setback' heartbeat is a no-op, so the
    restore target can't be overwritten with the setback value."""
    with tempfile.TemporaryDirectory() as tmp:
        stub = _make_stub(Path(tmp))
        src = FakeHeatSource(setpoint_c=21.0)

        _apply(stub, "setback", 14.0, src)
        _apply(stub, "setback", 14.0, src)  # same state again

        assert src.writes == [14.0]
        assert stub.state.saved_setpoint_c == 21.0


def test_restart_mid_vacancy_does_not_clobber_saved_setpoint():
    """After a restart the thermostat reads the SETBACK temp. A fresh
    agent (override starts False) that gets another 'setback' must keep
    the persisted original, not capture the setback temp as the
    restore target."""
    with tempfile.TemporaryDirectory() as tmp:
        stub = _make_stub(Path(tmp))
        # Persisted from before the restart.
        stub.state.saved_setpoint_c = 21.0
        stub.state.save()
        # Thermostat now physically reads the setback temperature.
        src = FakeHeatSource(setpoint_c=14.0)

        _apply(stub, "setback", 14.0, src)

        assert stub.state.saved_setpoint_c == 21.0  # untouched


def test_saved_setpoint_survives_state_reload():
    with tempfile.TemporaryDirectory() as tmp:
        stub = _make_stub(Path(tmp))
        src = FakeHeatSource(setpoint_c=21.0)
        _apply(stub, "setback", 14.0, src)

        reloaded = AgentState(str(Path(tmp) / "state.json"))
        assert reloaded.load() is True
        assert reloaded.saved_setpoint_c == 21.0


def test_read_only_source_still_tracks_override_state():
    """A source that can't write setpoints (file/http) still flips the
    override flag so the demand loop zeroes demand — HADCD stops heating
    even though the conventional thermostat can't be cut."""
    with tempfile.TemporaryDirectory() as tmp:
        stub = _make_stub(Path(tmp))
        src = FakeHeatSource(writable=False)

        _apply(stub, "setback", 14.0, src)

        assert stub._heat_override_active is True
        assert src.writes == []


def test_state_save_omits_setpoint_when_none():
    with tempfile.TemporaryDirectory() as tmp:
        state = AgentState(str(Path(tmp) / "state.json"))
        state.node_id = "n"
        state.node_token = "t"
        state.save()
        data = json.loads((Path(tmp) / "state.json").read_text())
        assert "saved_setpoint_c" not in data
