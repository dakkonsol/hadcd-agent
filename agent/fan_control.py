"""Steady, tier-based GPU fan control.

Most GPU fan noise on a heat node comes from *bursts*: the card's 0 dB
mode stops the fans below ~50-60 C, then spins them up hard as the card
warms, then stops them again. HADCD workloads are long and sustained,
so instead of reacting to temperature the agent sets a fixed fan level
for the kind of work it knows it is running (feed-forward), and ramps
gradually between levels:

  idle    -> FAN_IDLE_PCT    (nothing running)
  fill    -> FAN_FILL_PCT    (mining / offloaded inference / heat fill)
  rental  -> FAN_RENTAL_PCT  (a customer rental session, or a Vast.AI renter)

Safety: fixed speeds have no thermal feedback, so control is handed back
to the driver's automatic fan curve when

  * the GPU reaches FAN_GUARD_TEMP_C (held until it cools below
    FAN_GUARD_RELEASE_C), or
  * the host is using the machine interactively (Sunshine session), or
  * the agent stops.

Only GPU fans are controlled. Case and AIO fans stay on fixed BIOS
settings (see the node build guide).

Setting a GPU fan speed needs root, but the agent runs unprivileged, so
the default "helper" backend calls a small root-owned script through
``sudo -n`` (see scripts/hadcd-gpu-fan and docs/gpu-fan-control.md). The
"nvml" backend sets speeds in-process and only works if the agent runs
as root. Reading temperature / utilisation / fan speed uses
``nvidia-smi``, which needs no privileges.

Disabled by default (FAN_CONTROL_ENABLED=false).
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

logger = logging.getLogger("hadcd.fan_control")


class FanTier(str, Enum):
    IDLE = "idle"
    FILL = "fill"
    RENTAL = "rental"


@dataclass(frozen=True)
class GpuReading:
    temp_c: float
    util_pct: float
    fan_pct: float | None  # None when the card reports no fan reading


def select_tier(
    *,
    rental_sessions: int,
    vast_unlisting: bool,
    vast_listed: bool,
    gpu_util_pct: float | None,
    rental_util_pct: float,
    fill_running: bool,
) -> FanTier:
    """Pick the fan tier from what the agent knows it is running.

    * A customer rental session, or Vast.AI draining an active renter
      (UNLISTING), is the rental tier.
    * While listed on Vast.AI the agent cannot see whether a renter is
      on the card, so GPU utilisation at or above ``rental_util_pct``
      is treated as a renter.
    * Any in-flight dispatcher task or autonomous mining is fill.
    """
    if rental_sessions > 0 or vast_unlisting:
        return FanTier.RENTAL
    if vast_listed and gpu_util_pct is not None and gpu_util_pct >= rental_util_pct:
        return FanTier.RENTAL
    if fill_running:
        return FanTier.FILL
    return FanTier.IDLE


# --- I/O backends ------------------------------------------------------


class FanBackend(Protocol):
    def set_speed(self, pct: int) -> bool: ...
    def restore_auto(self) -> bool: ...
    def get_speed(self) -> int | None:
        """Current *target* fan speed (0..100), or None if unknown."""
        ...


def read_gpu(gpu_index: int, timeout: float = 5.0) -> GpuReading | None:
    """Read temperature, utilisation and fan speed via nvidia-smi."""
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "-i", str(gpu_index),
                "--query-gpu=temperature.gpu,utilization.gpu,fan.speed",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return parse_reading(out.stdout)


def parse_reading(text: str) -> GpuReading | None:
    """Parse one nvidia-smi CSV line: ``temp, util, fan``."""
    line = text.strip().splitlines()[0] if text.strip() else ""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 3:
        return None
    try:
        temp = float(parts[0])
        util = float(parts[1])
    except ValueError:
        return None
    try:
        fan: float | None = float(parts[2])
    except ValueError:  # "[N/A]" on cards without a fan reading
        fan = None
    return GpuReading(temp_c=temp, util_pct=util, fan_pct=fan)


class HelperBackend:
    """Set speeds via ``sudo -n <helper> set <pct>`` / ``<helper> auto``."""

    def __init__(self, helper_path: str, gpu_index: int, timeout: float = 10.0) -> None:
        self._helper = helper_path
        self._gpu = gpu_index
        self._timeout = timeout

    def _run(self, *args: str) -> str | None:
        """Run the helper; return its stdout on success, None on failure."""
        cmd = ["sudo", "-n", self._helper, "--gpu", str(self._gpu), *args]
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self._timeout, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("fan: helper call failed: %s", exc)
            return None
        if out.returncode != 0:
            logger.warning(
                "fan: helper exited %d: %s",
                out.returncode, (out.stderr or out.stdout).strip(),
            )
            return None
        return out.stdout

    def set_speed(self, pct: int) -> bool:
        return self._run("set", str(int(pct))) is not None

    def restore_auto(self) -> bool:
        return self._run("auto") is not None

    def get_speed(self) -> int | None:
        out = self._run("get")
        if out is None:
            return None
        try:
            return _clamp_pct(int(out.strip()))
        except ValueError:
            logger.warning("fan: helper 'get' printed %r", out.strip())
            return None


class NvmlBackend:
    """In-process NVML control. Requires the agent to run as root."""

    def __init__(self, gpu_index: int) -> None:
        import pynvml  # nvidia-ml-py; imported lazily so it stays optional

        self._nvml = pynvml
        pynvml.nvmlInit()
        self._handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_index)
        self._fans = pynvml.nvmlDeviceGetNumFans(self._handle)

    def set_speed(self, pct: int) -> bool:
        try:
            for fan in range(self._fans):
                self._nvml.nvmlDeviceSetFanSpeed_v2(self._handle, fan, int(pct))
            return True
        except self._nvml.NVMLError as exc:
            logger.warning("fan: NVML set failed: %s", exc)
            return False

    def restore_auto(self) -> bool:
        try:
            for fan in range(self._fans):
                self._nvml.nvmlDeviceSetDefaultFanSpeed_v2(self._handle, fan)
            return True
        except self._nvml.NVMLError as exc:
            logger.warning("fan: NVML restore failed: %s", exc)
            return False

    def get_speed(self) -> int | None:
        try:
            targets = [
                self._nvml.nvmlDeviceGetTargetFanSpeed(self._handle, fan)
                for fan in range(self._fans)
            ]
        except self._nvml.NVMLError as exc:
            logger.warning("fan: NVML target read failed: %s", exc)
            return None
        return _clamp_pct(round(sum(targets) / len(targets))) if targets else None


# --- controller ----------------------------------------------------------


class FanController:
    """Feed-forward fan levels per tier with gradual ramps and a heat guard.

    Call :meth:`tick` periodically with the current tier, GPU reading and
    whether the host is using the machine. All I/O goes through the
    injected backend, so the logic is unit-testable.
    """

    def __init__(
        self,
        backend: FanBackend,
        *,
        levels: dict[FanTier, int],
        ramp_sec: float,
        guard_temp_c: float,
        guard_release_c: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend = backend
        self._levels = {t: _clamp_pct(p) for t, p in levels.items()}
        self._ramp_sec = max(ramp_sec, 0.0)
        self._guard_temp = guard_temp_c
        self._guard_release = min(guard_release_c, guard_temp_c)
        self._clock = clock

        self._auto = True           # driver curve in charge (start state)
        self._guard_tripped = False
        self._current: float | None = None   # last commanded pct
        self._applied: int | None = None
        self._target: int | None = None
        self._rate = 0.0            # pct per second for the active ramp
        self._last_tick: float | None = None
        self._backend_failed = False

    @property
    def mode(self) -> str:
        if self._backend_failed:
            return "disabled"
        return "auto" if self._auto else "fixed"

    @property
    def commanded_pct(self) -> int | None:
        return None if self._auto else self._applied

    def tick(
        self,
        tier: FanTier,
        reading: GpuReading | None,
        *,
        host_session_active: bool,
    ) -> None:
        if self._backend_failed:
            return
        now = self._clock()
        dt = 0.0 if self._last_tick is None else max(now - self._last_tick, 0.0)
        self._last_tick = now

        # Heat guard with hysteresis. With no reading, fail safe to auto.
        if reading is None:
            self._hand_back("no GPU reading")
            return
        if reading.temp_c >= self._guard_temp:
            if not self._guard_tripped:
                logger.warning(
                    "fan: GPU at %.0f C >= guard %.0f C — handing fans back "
                    "to the driver curve", reading.temp_c, self._guard_temp,
                )
            self._guard_tripped = True
        elif self._guard_tripped and reading.temp_c < self._guard_release:
            logger.info("fan: GPU cooled to %.0f C — resuming tier control", reading.temp_c)
            self._guard_tripped = False

        if self._guard_tripped:
            self._hand_back("heat guard")
            return
        if host_session_active:
            self._hand_back("host session active")
            return

        target = self._levels[tier]
        if self._auto:
            # Take over from the driver curve, starting from where the
            # driver has the fans so the transition is a ramp, not a jump.
            start = self._start_speed(reading, target)
            self._auto = False
            self._current = float(start)
            self._applied = None
            self._target = None
            logger.info("fan: taking control at %.0f%% (tier=%s)", start, tier.value)

        if target != self._target:
            # A new ramp starts now; time before the tier change doesn't count.
            dt = 0.0
            self._target = target
            span = abs(target - (self._current or 0.0))
            self._rate = span / self._ramp_sec if self._ramp_sec > 0 else float("inf")

        cur = self._current if self._current is not None else float(target)
        if self._rate == float("inf"):
            cur = float(target)
        else:
            step = self._rate * dt
            if cur < target:
                cur = min(cur + step, float(target))
            elif cur > target:
                cur = max(cur - step, float(target))
        self._current = cur

        pct = _clamp_pct(round(cur))
        if pct != self._applied:
            if self._backend.set_speed(pct):
                self._applied = pct
            else:
                self._fail("set_speed failed")

    def close(self) -> None:
        """Hand the fans back to the driver (agent shutdown)."""
        if not self._backend_failed and not self._auto:
            self._backend.restore_auto()
        self._auto = True

    # --- internals ---

    def _start_speed(self, reading: GpuReading, target: int) -> float:
        """Where the driver currently has the fans.

        Prefer the backend's NVML *target* speed: some cards (seen on an
        RTX 3090) report 0 % in nvidia-smi's fan.speed even while the
        fans spin, which would make every hand-over start from 0. Fall
        back to the nvidia-smi reading, then to the tier target.
        """
        get_speed = getattr(self._backend, "get_speed", None)
        if get_speed is not None:
            try:
                commanded = get_speed()
            except Exception:  # never let a read failure stop control
                logger.debug("fan: get_speed raised", exc_info=True)
                commanded = None
            if commanded is not None:
                return float(commanded)
        if reading.fan_pct is not None:
            return float(reading.fan_pct)
        return float(target)

    def _hand_back(self, reason: str) -> None:
        if self._auto:
            return
        logger.info("fan: restoring driver fan curve (%s)", reason)
        if not self._backend.restore_auto():
            self._fail("restore_auto failed")
            return
        self._auto = True
        self._applied = None
        self._target = None

    def _fail(self, reason: str) -> None:
        # Stop issuing commands; try once to leave the driver in charge.
        logger.error(
            "fan: %s — disabling fan control for this run (check the "
            "helper/sudoers setup in docs/gpu-fan-control.md)", reason,
        )
        self._backend_failed = True
        try:
            self._backend.restore_auto()
        except Exception:  # pragma: no cover - best effort
            pass


def _clamp_pct(pct: float) -> int:
    return int(max(0, min(100, pct)))


def build_fan_controller(settings) -> FanController | None:
    """Build a controller from AgentSettings, or None when disabled."""
    if not settings.fan_control_enabled:
        return None
    backend_name = (settings.fan_control_backend or "helper").lower()
    try:
        if backend_name == "nvml":
            backend: FanBackend = NvmlBackend(settings.fan_gpu_index)
        elif backend_name == "helper":
            backend = HelperBackend(settings.fan_control_helper, settings.fan_gpu_index)
        else:
            logger.error("fan: unknown FAN_CONTROL_BACKEND=%r — fan control off", backend_name)
            return None
    except Exception:
        logger.exception("fan: backend %s unavailable — fan control off", backend_name)
        return None
    return FanController(
        backend,
        levels={
            FanTier.IDLE: settings.fan_idle_pct,
            FanTier.FILL: settings.fan_fill_pct,
            FanTier.RENTAL: settings.fan_rental_pct,
        },
        ramp_sec=settings.fan_ramp_sec,
        guard_temp_c=settings.fan_guard_temp_c,
        guard_release_c=settings.fan_guard_release_c,
    )
