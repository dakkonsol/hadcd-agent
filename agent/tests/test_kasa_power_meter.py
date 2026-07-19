"""Unit tests for KasaPowerMeter and helpers (Phase 16b).

No real hardware required — all kasa library interaction is mocked.
Tests cover:
  - _extract_watts: modern Module.Energy API, legacy emeter_realtime dict
    (both power W and power_mw variants), fallback to None
  - KasaPowerMeter.enabled: True / False based on ip
  - KasaPowerMeter.poll: success path, failure path (fail-quiet)
  - KasaPowerMeter.average_watts_since: empty window, single sample,
    multi-sample trapezoid, pre-window samples
  - build_kasa_meter: disabled when ip is empty

``python-kasa`` is imported *inside* ``_extract_watts`` (lazy import), so
we patch ``sys.modules['kasa']`` rather than a module-level name.
"""

from __future__ import annotations

import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.kasa_power_meter import (
    KasaPowerMeter,
    _extract_watts,
    build_kasa_meter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_kasa_module(energy_key: str = "energy_key") -> MagicMock:
    """Return a mock ``kasa`` package with ``Module.Energy`` set."""
    mock_kasa = MagicMock()
    mock_kasa.Module.Energy = energy_key
    return mock_kasa


# ---------------------------------------------------------------------------
# _extract_watts
# ---------------------------------------------------------------------------


class TestExtractWatts:
    def test_modern_energy_module(self):
        """Module.Energy.current_power → direct float."""
        energy_mod = MagicMock()
        energy_mod.current_power = 125.0

        device = MagicMock()
        mock_kasa = _make_kasa_module()
        device.modules = {mock_kasa.Module.Energy: energy_mod}

        with patch.dict(sys.modules, {"kasa": mock_kasa}):
            # Remove any cached kasa import so our patch takes effect
            result = _extract_watts(device)
        assert result == pytest.approx(125.0)

    def test_modern_energy_module_none_current_power(self):
        """Module.Energy present but current_power is None → try legacy."""
        energy_mod = MagicMock()
        energy_mod.current_power = None

        device = MagicMock()
        mock_kasa = _make_kasa_module()
        device.modules = {mock_kasa.Module.Energy: energy_mod}
        device.emeter_realtime = {"power": 80.0}

        with patch.dict(sys.modules, {"kasa": mock_kasa}):
            result = _extract_watts(device)
        assert result == pytest.approx(80.0)

    def test_legacy_power_key(self):
        """Legacy emeter_realtime dict with 'power' key (watts)."""
        device = MagicMock()
        device.emeter_realtime = {"power": 200.5}

        # Simulate kasa not installed (ImportError on `from kasa import Module`)
        with patch.dict(sys.modules, {"kasa": None}):
            result = _extract_watts(device)
        assert result == pytest.approx(200.5)

    def test_legacy_power_mw_key(self):
        """Legacy emeter_realtime dict with 'power_mw' key (milliwatts → watts)."""
        device = MagicMock()
        device.emeter_realtime = {"power_mw": 50_000}

        with patch.dict(sys.modules, {"kasa": None}):
            result = _extract_watts(device)
        assert result == pytest.approx(50.0)

    def test_no_usable_api(self):
        """Neither API available → returns None."""
        device = MagicMock()
        device.emeter_realtime = {}  # no 'power' or 'power_mw' keys

        with patch.dict(sys.modules, {"kasa": None}):
            result = _extract_watts(device)
        assert result is None

    def test_emeter_not_dict(self):
        """emeter_realtime is not a dict (e.g. AttributeError) → returns None."""
        device = MagicMock(spec=[])  # no attributes at all → AttributeError

        with patch.dict(sys.modules, {"kasa": None}):
            result = _extract_watts(device)
        assert result is None


# ---------------------------------------------------------------------------
# KasaPowerMeter.enabled
# ---------------------------------------------------------------------------


class TestEnabled:
    def test_enabled_when_ip_set(self):
        m = KasaPowerMeter(ip="192.168.1.50")
        assert m.enabled is True

    def test_disabled_when_ip_empty(self):
        m = KasaPowerMeter(ip="")
        assert m.enabled is False

    def test_last_watts_none_initially(self):
        m = KasaPowerMeter(ip="192.168.1.50")
        assert m.last_watts is None


# ---------------------------------------------------------------------------
# KasaPowerMeter.poll — success path
# ---------------------------------------------------------------------------


class TestPollSuccess:
    @pytest.fixture
    def meter(self):
        return KasaPowerMeter(ip="192.168.1.50", username="u", password="p")

    @pytest.mark.asyncio
    async def test_poll_returns_watts_and_caches(self, meter):
        mock_device = AsyncMock()
        mock_device.update = AsyncMock()

        with (
            patch.object(meter, "_get_device", return_value=mock_device),
            patch("agent.kasa_power_meter._extract_watts", return_value=120.0),
        ):
            result = await meter.poll()

        assert result == pytest.approx(120.0)
        assert meter.last_watts == pytest.approx(120.0)
        assert len(meter._samples) == 1

    @pytest.mark.asyncio
    async def test_poll_when_disabled_returns_none(self):
        meter = KasaPowerMeter(ip="")
        result = await meter.poll()
        assert result is None
        assert len(meter._samples) == 0

    @pytest.mark.asyncio
    async def test_poll_appends_multiple_samples(self, meter):
        mock_device = AsyncMock()
        mock_device.update = AsyncMock()
        watts_seq = [100.0, 110.0, 105.0]

        for w in watts_seq:
            with (
                patch.object(meter, "_get_device", return_value=mock_device),
                patch("agent.kasa_power_meter._extract_watts", return_value=w),
            ):
                await meter.poll()

        assert len(meter._samples) == 3
        assert meter.last_watts == pytest.approx(105.0)


# ---------------------------------------------------------------------------
# KasaPowerMeter.poll — fail-quiet
# ---------------------------------------------------------------------------


class TestPollFailQuiet:
    @pytest.fixture
    def meter(self):
        return KasaPowerMeter(ip="192.168.1.50")

    @pytest.mark.asyncio
    async def test_exception_returns_none(self, meter):
        with patch.object(meter, "_get_device", side_effect=RuntimeError("boom")):
            result = await meter.poll()
        assert result is None

    @pytest.mark.asyncio
    async def test_exception_resets_device_handle(self, meter):
        """After a poll failure, _device is cleared so next poll re-discovers."""
        meter._device = MagicMock()  # simulate a previously cached device
        with patch.object(meter, "_get_device", side_effect=OSError("network")):
            await meter.poll()
        assert meter._device is None

    @pytest.mark.asyncio
    async def test_import_error_is_quiet(self, meter):
        """python-kasa not installed → RuntimeError → returns None, no raise."""
        with patch.object(
            meter,
            "_get_device",
            side_effect=RuntimeError("python-kasa is not installed"),
        ):
            result = await meter.poll()
        assert result is None


# ---------------------------------------------------------------------------
# KasaPowerMeter.average_watts_since
# ---------------------------------------------------------------------------


class TestAverageWattsSince:
    def test_no_samples_returns_none(self):
        meter = KasaPowerMeter(ip="192.168.1.50")
        assert meter.average_watts_since(time.monotonic()) is None

    def test_no_samples_in_window_returns_last_watts(self):
        meter = KasaPowerMeter(ip="192.168.1.50")
        meter._last_watts = 80.0
        future = time.monotonic() + 1000.0
        assert meter.average_watts_since(future) == pytest.approx(80.0)

    def test_single_sample_returns_that_sample(self):
        meter = KasaPowerMeter(ip="192.168.1.50")
        t = time.monotonic()
        meter._samples.append((t, 100.0))
        result = meter.average_watts_since(t - 1.0)
        assert result == pytest.approx(100.0)

    def test_two_equal_samples_returns_that_value(self):
        meter = KasaPowerMeter(ip="192.168.1.50")
        t = time.monotonic()
        meter._samples.append((t, 100.0))
        meter._samples.append((t + 10.0, 100.0))
        result = meter.average_watts_since(t - 1.0)
        assert result == pytest.approx(100.0)

    def test_trapezoid_two_different_samples(self):
        """Average of 100 W for 0 s and 200 W for 10 s → 150 W."""
        meter = KasaPowerMeter(ip="192.168.1.50")
        t = time.monotonic()
        meter._samples.append((t, 100.0))
        meter._samples.append((t + 10.0, 200.0))
        result = meter.average_watts_since(t - 1.0)
        assert result == pytest.approx(150.0)

    def test_trapezoid_three_samples(self):
        """Ramp 0→100→200 W at t=0, t=10, t=20 → TWA = 100 W."""
        meter = KasaPowerMeter(ip="192.168.1.50")
        t = time.monotonic()
        meter._samples.append((t, 0.0))
        meter._samples.append((t + 10.0, 100.0))
        meter._samples.append((t + 20.0, 200.0))
        # TWA = ((0+100)/2 * 10 + (100+200)/2 * 10) / 20
        #     = (500 + 1500) / 20 = 100
        result = meter.average_watts_since(t - 1.0)
        assert result == pytest.approx(100.0)

    def test_samples_before_since_excluded(self):
        """Samples before 'since' are ignored."""
        meter = KasaPowerMeter(ip="192.168.1.50")
        t = time.monotonic()
        meter._samples.append((t - 100.0, 999.0))  # old, excluded
        meter._samples.append((t, 50.0))
        meter._samples.append((t + 10.0, 50.0))
        result = meter.average_watts_since(t - 1.0)
        # Only the two recent samples are in the window
        assert result == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# build_kasa_meter
# ---------------------------------------------------------------------------


class TestBuildKasaMeter:
    def test_disabled_when_ip_empty(self):
        settings = MagicMock()
        settings.kasa_plug_ip = ""
        settings.kasa_username = ""
        settings.kasa_password = ""
        meter = build_kasa_meter(settings)
        assert meter.enabled is False

    def test_enabled_with_ip(self):
        settings = MagicMock()
        settings.kasa_plug_ip = "10.0.0.5"
        settings.kasa_username = "user@example.com"
        settings.kasa_password = "secret"
        meter = build_kasa_meter(settings)
        assert meter.enabled is True
        assert meter._ip == "10.0.0.5"
        assert meter._username == "user@example.com"
