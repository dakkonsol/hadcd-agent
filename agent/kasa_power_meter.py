"""Kasa KP125M smart plug power meter.

Polls a Kasa KP125M (or compatible energy-monitoring plug) for real-time
wattage via the python-kasa library.  Used by the agent to report actual
measured power instead of the task's estimated power — required for
CRA-quality heat-session earnings records.

The KP125M uses TP-Link's newer KLAP protocol, which requires a TP-Link
account (email + password).  Older plugs (KP115, KP125 pre-KLAP) work
without credentials; set KASA_USERNAME and KASA_PASSWORD to empty strings
in that case.

Configuration (agent env vars):
  KASA_PLUG_IP           LAN IP of the plug, e.g. 192.168.1.50
                         Leave empty to disable — agent falls back to
                         the task's estimated power.
  KASA_USERNAME          TP-Link account email. Required for KP125M.
  KASA_PASSWORD          TP-Link account password. Required for KP125M.
  KASA_POLL_INTERVAL_SEC How often to sample (default 10 s).

Wiring: plug the node's power supply into the Kasa plug. The meter
then measures total node draw.  At AGENT_CONCURRENCY=1 (the default),
this equals the single running task's draw.

Thread safety: all methods are async-safe and intended for use from
a single asyncio event loop.  Do not share a KasaPowerMeter across
threads.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import TYPE_CHECKING, Deque, Tuple

if TYPE_CHECKING:
    pass  # avoid circular imports

logger = logging.getLogger("hadcd.agent.kasa")

# Rolling window size: 30 min at 10 s/sample = 180 samples.
# Enough to cover even the longest normal task without unbounded growth.
_WINDOW_MAX = 180

_WS_PER_KWH = 3_600_000.0  # watt-seconds per kilowatt-hour

# Per-poll timeout in seconds.  Must be less than KASA_POLL_INTERVAL_SEC.
_POLL_TIMEOUT_SEC = 8.0

# Type alias: (monotonic_timestamp_sec, watts)
_Sample = Tuple[float, float]


class KasaPowerMeter:
    """Real-time wattmeter backed by a Kasa KP125M (or compatible) plug.

    Lifecycle:
      1. ``poll()`` is called every ``KASA_POLL_INTERVAL_SEC`` from the
         agent's ``_kasa_loop`` background coroutine.
      2. Each successful poll caches a ``(monotonic_time, watts)`` sample.
      3. ``average_watts_since(t)`` returns the time-weighted average
         watts over all samples recorded after time ``t``.  Used in
         ``_run_assignment`` to compute per-task measured power.
      4. ``last_watts`` is the most recent sample, for quick access.

    Failure behaviour (fail-quiet):
      - On any error (ImportError, network, auth, timeout) the meter
        logs a single warning, returns None, and resets the device
        handle so the next poll reattempts discovery.
      - ``average_watts_since`` falls back to ``last_watts`` (the last
        good reading) when no samples fall in the requested window,
        rather than returning None.
      - The agent always uses ``measured or nominal`` so a bad Kasa
        reading degrades gracefully to the estimated value.
    """

    def __init__(
        self,
        ip: str,
        username: str = "",
        password: str = "",
    ) -> None:
        self._ip = ip
        self._username = username
        self._password = password
        self._device = None  # lazily initialised on first poll
        self._samples: Deque[_Sample] = deque(maxlen=_WINDOW_MAX)
        self._last_watts: float | None = None
        self._warned = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """True if a plug IP is configured."""
        return bool(self._ip)

    @property
    def last_watts(self) -> float | None:
        """Most recent power reading in watts, or None if unavailable."""
        return self._last_watts

    async def poll(self) -> float | None:
        """Sample the plug once and cache the result.

        Returns the current power draw in watts, or None on any failure.
        Internally retries device discovery on a fresh connection after
        any error so transient failures recover automatically.
        """
        if not self.enabled:
            return None
        try:
            device = await self._get_device()
            await device.update()
            watts = _extract_watts(device)
            if watts is not None:
                self._last_watts = watts
                self._samples.append((time.monotonic(), watts))
                self._warned = False
                logger.debug("kasa: %.1f W (ip=%s)", watts, self._ip)
            return watts
        except Exception as exc:
            if not self._warned:
                logger.warning(
                    "kasa: poll failed (ip=%s): %s — "
                    "falling back to estimated power",
                    self._ip,
                    exc,
                )
                self._warned = True
            # Discard the device handle so next poll re-discovers.
            self._device = None
            return None

    def average_watts_since(self, since_monotonic: float) -> float | None:
        """Time-weighted average wattage over [since_monotonic, now].

        Returns None if the meter has never produced a reading.
        Falls back to ``last_watts`` when no samples fall in the window
        (e.g. the task started before the first successful poll).
        """
        in_window = [
            (ts, w) for ts, w in self._samples if ts >= since_monotonic
        ]

        if not in_window:
            # No samples in window — best effort: return the last reading.
            return self._last_watts

        if len(in_window) == 1:
            return in_window[0][1]

        # Trapezoid integration: true time-weighted average.
        total_ws = 0.0
        total_sec = 0.0
        for i in range(1, len(in_window)):
            t0, w0 = in_window[i - 1]
            t1, w1 = in_window[i]
            dt = t1 - t0
            total_ws += (w0 + w1) / 2.0 * dt
            total_sec += dt

        return total_ws / total_sec if total_sec > 0 else in_window[-1][1]

    async def aclose(self) -> None:
        """Release the device connection gracefully."""
        if self._device is not None:
            try:
                await self._device.disconnect()
            except Exception:
                pass
            self._device = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_device(self):
        """Return the cached device, re-discovering if necessary."""
        if self._device is not None:
            return self._device

        try:
            from kasa import Credentials, Discover
        except ImportError as exc:
            raise RuntimeError(
                "python-kasa is not installed — "
                "add it to agent/requirements.txt and reinstall: "
                "pip install python-kasa"
            ) from exc

        creds = (
            Credentials(self._username, self._password)
            if self._username
            else None
        )
        device = await Discover.discover_single(
            self._ip,
            credentials=creds,
        )
        self._device = device
        logger.info(
            "kasa: connected to %s (%s) at %s",
            getattr(device, "alias", "unknown"),
            getattr(device, "model", "unknown"),
            self._ip,
        )
        return device


# ---------------------------------------------------------------------------
# Module-level helper (also used in tests without a KasaPowerMeter instance)
# ---------------------------------------------------------------------------


def _extract_watts(device) -> float | None:
    """Extract current power draw in watts from a python-kasa device object.

    Tries the modern Module.Energy API first (python-kasa ≥ 0.7), then
    falls back to the legacy emeter_realtime dict (older versions and
    older firmware).  Returns None when neither path yields a value.
    """
    # --- Modern API (python-kasa ≥ 0.7): Module.Energy ---------------
    try:
        from kasa import Module  # type: ignore[import]
        energy = device.modules.get(Module.Energy)
        if energy is not None:
            w = getattr(energy, "current_power", None)
            if w is not None:
                return float(w)
    except (ImportError, AttributeError):
        pass

    # --- Legacy API: emeter_realtime dict ----------------------------
    try:
        rt = device.emeter_realtime
        if isinstance(rt, dict):
            if "power" in rt:
                return float(rt["power"])
            if "power_mw" in rt:
                return float(rt["power_mw"]) / 1000.0
    except AttributeError:
        pass

    return None


# ---------------------------------------------------------------------------
# Factory (mirrors the build_source / build_session_source pattern)
# ---------------------------------------------------------------------------


def build_kasa_meter(settings) -> "KasaPowerMeter":
    """Construct a KasaPowerMeter from AgentSettings.

    Returns a disabled meter (enabled=False) when KASA_PLUG_IP is empty,
    so callers can always call ``meter.poll()`` / ``meter.last_watts``
    without checking whether Kasa is configured.
    """
    return KasaPowerMeter(
        ip=settings.kasa_plug_ip,
        username=settings.kasa_username,
        password=settings.kasa_password,
    )
