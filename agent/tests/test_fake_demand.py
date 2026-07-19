"""Tests for agent.fake_demand — the synthetic BMS writer.

All tests are synchronous (no async needed; the module uses blocking I/O
and time.sleep, both of which are controlled via mocking).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agent.fake_demand import (
    _default_bms_path,
    _demand_payload,
    _zero_payload,
    main,
    run_constant,
    run_cycle,
    run_off,
    write_payload,
)


# ── payload helpers ────────────────────────────────────────────────────────────

class TestDemandPayload:
    def test_keys_present(self):
        p = _demand_payload(5.0, 21.0, 18.5, 1800)
        assert set(p) == {"measured_kw", "setpoint_c", "room_temp_c",
                          "expected_window_sec"}

    def test_values_match(self):
        p = _demand_payload(3.5, 22.0, 19.0, 900)
        assert p["measured_kw"] == 3.5
        assert p["setpoint_c"] == 22.0
        assert p["room_temp_c"] == 19.0
        assert p["expected_window_sec"] == 900

    def test_measured_kw_is_positive(self):
        p = _demand_payload(5.0, 21.0, 18.5, 1800)
        assert p["measured_kw"] > 0


class TestZeroPayload:
    def test_measured_kw_is_zero(self):
        p = _zero_payload(21.0, 18.5)
        assert p["measured_kw"] == 0.0

    def test_expected_window_is_zero(self):
        p = _zero_payload(21.0, 18.5)
        assert p["expected_window_sec"] == 0

    def test_setpoint_and_room_preserved(self):
        p = _zero_payload(22.5, 17.0)
        assert p["setpoint_c"] == 22.5
        assert p["room_temp_c"] == 17.0


# ── write_payload ──────────────────────────────────────────────────────────────

class TestWritePayload:
    def test_creates_file(self, tmp_path):
        p = tmp_path / "bms.json"
        write_payload(p, {"measured_kw": 5.0})
        assert p.exists()

    def test_content_is_valid_json(self, tmp_path):
        p = tmp_path / "bms.json"
        payload = {"measured_kw": 5.0, "setpoint_c": 21.0}
        write_payload(p, payload)
        loaded = json.loads(p.read_text())
        assert loaded == payload

    def test_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "nested" / "deep" / "bms.json"
        write_payload(p, {"measured_kw": 5.0})
        assert p.exists()

    def test_overwrites_existing(self, tmp_path):
        p = tmp_path / "bms.json"
        write_payload(p, {"measured_kw": 5.0})
        write_payload(p, {"measured_kw": 0.0})
        loaded = json.loads(p.read_text())
        assert loaded["measured_kw"] == 0.0

    def test_no_stray_tmp_files(self, tmp_path):
        p = tmp_path / "bms.json"
        write_payload(p, {"measured_kw": 5.0})
        files = list(tmp_path.iterdir())
        assert files == [p]

    def test_output_ends_with_newline(self, tmp_path):
        p = tmp_path / "bms.json"
        write_payload(p, {"measured_kw": 5.0})
        assert p.read_text().endswith("\n")


# ── run_constant ───────────────────────────────────────────────────────────────

class TestRunConstant:
    def test_once_writes_demand(self, tmp_path):
        p = tmp_path / "bms.json"
        run_constant(p, 5.0, 21.0, 18.5, 1800, 15.0, once=True)
        data = json.loads(p.read_text())
        assert data["measured_kw"] == 5.0

    def test_once_does_not_sleep(self, tmp_path):
        p = tmp_path / "bms.json"
        with patch("agent.fake_demand.time.sleep") as mock_sleep:
            run_constant(p, 5.0, 21.0, 18.5, 1800, 15.0, once=True)
        mock_sleep.assert_not_called()

    def test_loops_until_interrupted(self, tmp_path):
        p = tmp_path / "bms.json"
        # Allow exactly 3 iterations then stop by raising.
        call_count = {"n": 0}

        def fake_sleep(_):
            call_count["n"] += 1
            if call_count["n"] >= 3:
                raise KeyboardInterrupt

        with patch("agent.fake_demand.time.sleep", side_effect=fake_sleep):
            with pytest.raises(KeyboardInterrupt):
                run_constant(p, 5.0, 21.0, 18.5, 1800, 15.0, once=False)

        assert call_count["n"] == 3

    def test_uses_custom_kw(self, tmp_path):
        p = tmp_path / "bms.json"
        run_constant(p, 2.5, 21.0, 18.5, 1800, 15.0, once=True)
        data = json.loads(p.read_text())
        assert data["measured_kw"] == 2.5


# ── run_off ────────────────────────────────────────────────────────────────────

class TestRunOff:
    def test_once_writes_zero(self, tmp_path):
        p = tmp_path / "bms.json"
        run_off(p, 21.0, 18.5, 15.0, once=True)
        data = json.loads(p.read_text())
        assert data["measured_kw"] == 0.0

    def test_once_does_not_sleep(self, tmp_path):
        p = tmp_path / "bms.json"
        with patch("agent.fake_demand.time.sleep") as mock_sleep:
            run_off(p, 21.0, 18.5, 15.0, once=True)
        mock_sleep.assert_not_called()


# ── run_cycle ──────────────────────────────────────────────────────────────────

class TestRunCycle:
    """Cycle mode tests control time.monotonic to force a phase transition."""

    def _run_n(self, path, n_iters, monotonic_values):
        """Run cycle for exactly n_iters iterations using preset monotonic values."""
        sleep_count = {"n": 0}

        def fake_sleep(_):
            sleep_count["n"] += 1
            if sleep_count["n"] >= n_iters:
                raise KeyboardInterrupt

        with patch("agent.fake_demand.time.monotonic",
                   side_effect=monotonic_values), \
             patch("agent.fake_demand.time.sleep", side_effect=fake_sleep):
            with pytest.raises(KeyboardInterrupt):
                run_cycle(
                    path=path,
                    measured_kw=5.0,
                    setpoint_c=21.0,
                    room_temp_c=18.5,
                    expected_window_sec=1800,
                    interval_sec=15.0,
                    on_sec=300.0,
                    off_sec=120.0,
                )

    def test_first_tick_is_heating(self, tmp_path):
        p = tmp_path / "bms.json"
        # monotonic calls: [cycle_start=0.0, loop1=10.0]
        # elapsed = (10 - 0) % 420 = 10 → heating phase
        self._run_n(p, 1, [0.0, 10.0])
        data = json.loads(p.read_text())
        assert data["measured_kw"] == 5.0

    def test_transitions_to_idle_after_on_sec(self, tmp_path):
        p = tmp_path / "bms.json"
        # monotonic calls: [cycle_start=0.0, loop1=10.0, loop2=310.0]
        # loop1: elapsed=10 → heating; loop2: elapsed=310 ≥ on_sec(300) → idle
        self._run_n(p, 2, [0.0, 10.0, 310.0])
        data = json.loads(p.read_text())
        assert data["measured_kw"] == 0.0

    def test_transitions_back_to_heating_after_full_period(self, tmp_path):
        p = tmp_path / "bms.json"
        # monotonic calls: [cycle_start=0.0, loop1=10, loop2=310, loop3=430]
        # period=420; loop3: (430-0)%420=10 → back to heating
        self._run_n(p, 3, [0.0, 10.0, 310.0, 430.0])
        data = json.loads(p.read_text())
        assert data["measured_kw"] == 5.0


# ── main() CLI ─────────────────────────────────────────────────────────────────

class TestMain:
    def test_constant_once(self, tmp_path):
        p = tmp_path / "bms.json"
        rc = main(["--mode", "constant", "--once", "--output", str(p)])
        assert rc == 0
        data = json.loads(p.read_text())
        assert data["measured_kw"] == 5.0

    def test_off_once(self, tmp_path):
        p = tmp_path / "bms.json"
        rc = main(["--mode", "off", "--once", "--output", str(p)])
        assert rc == 0
        data = json.loads(p.read_text())
        assert data["measured_kw"] == 0.0

    def test_custom_kw_once(self, tmp_path):
        p = tmp_path / "bms.json"
        rc = main(["--once", "--output", str(p), "--measured-kw", "3.2"])
        assert rc == 0
        data = json.loads(p.read_text())
        assert data["measured_kw"] == 3.2

    def test_keyboard_interrupt_writes_zero(self, tmp_path):
        p = tmp_path / "bms.json"

        def fake_sleep(_):
            raise KeyboardInterrupt

        with patch("agent.fake_demand.time.sleep", side_effect=fake_sleep):
            rc = main(["--mode", "constant", "--output", str(p)])

        assert rc == 0
        data = json.loads(p.read_text())
        assert data["measured_kw"] == 0.0

    def test_unknown_mode_exits_nonzero(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(["--mode", "bogus"])
        assert exc_info.value.code != 0

    def test_default_output_is_platform_path(self, tmp_path):
        """--output defaults to the platform BMS path without error."""
        default = _default_bms_path()
        assert default  # non-empty
        assert "hadcd" in default.lower() or "hadcd" in default.lower()


# ── default path ──────────────────────────────────────────────────────────────

class TestDefaultBmsPath:
    def test_returns_string(self):
        assert isinstance(_default_bms_path(), str)

    def test_contains_bms_json(self):
        assert "bms.json" in _default_bms_path()

    def test_windows_path_under_programdata(self):
        with patch("agent.fake_demand.platform.system", return_value="Windows"):
            p = _default_bms_path()
        assert "ProgramData" in p

    def test_linux_path_under_var_lib(self):
        with patch("agent.fake_demand.platform.system", return_value="Linux"):
            p = _default_bms_path()
        assert p.startswith("/var/lib")
