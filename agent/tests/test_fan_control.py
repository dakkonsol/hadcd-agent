"""Tests for steady, tier-based GPU fan control (agent/fan_control.py)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agent.fan_control import (
    FanController,
    FanTier,
    GpuReading,
    HelperBackend,
    build_fan_controller,
    parse_reading,
    select_tier,
)

LEVELS = {FanTier.IDLE: 30, FanTier.FILL: 40, FanTier.RENTAL: 50}


class FakeBackend:
    def __init__(self, fail_set: bool = False, target: int | None = None) -> None:
        self.calls: list[tuple[str, int | None]] = []
        self.fail_set = fail_set
        self.target = target  # what get_speed() reports; None = unknown

    def get_speed(self) -> int | None:
        return self.target

    def set_speed(self, pct: int) -> bool:
        self.calls.append(("set", pct))
        return not self.fail_set

    def restore_auto(self) -> bool:
        self.calls.append(("auto", None))
        return True

    @property
    def sets(self) -> list[int]:
        return [p for c, p in self.calls if c == "set"]


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _reading(temp: float = 60.0, util: float = 90.0, fan: float | None = 40.0) -> GpuReading:
    return GpuReading(temp_c=temp, util_pct=util, fan_pct=fan)


def _controller(backend, clock, ramp_sec: float = 30.0) -> FanController:
    return FanController(
        backend,
        levels=LEVELS,
        ramp_sec=ramp_sec,
        guard_temp_c=80.0,
        guard_release_c=72.0,
        clock=clock,
    )


# --- tier selection ---------------------------------------------------


def _tier(**overrides) -> FanTier:
    kwargs = dict(
        rental_sessions=0, vast_unlisting=False, vast_listed=False,
        gpu_util_pct=0.0, rental_util_pct=50.0, fill_running=False,
    )
    kwargs.update(overrides)
    return select_tier(**kwargs)


def test_idle_when_nothing_runs():
    assert _tier() is FanTier.IDLE


def test_fill_when_task_or_miner_running():
    assert _tier(fill_running=True) is FanTier.FILL


def test_rental_session_beats_fill():
    assert _tier(rental_sessions=1, fill_running=True) is FanTier.RENTAL


def test_vast_unlisting_means_renter_on_card():
    assert _tier(vast_unlisting=True) is FanTier.RENTAL


def test_vast_listed_uses_utilisation_to_detect_renter():
    assert _tier(vast_listed=True, gpu_util_pct=10.0) is FanTier.IDLE
    assert _tier(vast_listed=True, gpu_util_pct=75.0) is FanTier.RENTAL
    assert _tier(vast_listed=True, gpu_util_pct=None) is FanTier.IDLE


def test_utilisation_ignored_when_not_listed():
    # High util from our own fill work must not count as a Vast renter.
    assert _tier(gpu_util_pct=99.0, fill_running=True) is FanTier.FILL


# --- nvidia-smi parsing -------------------------------------------------


def test_parse_reading():
    assert parse_reading("63, 97, 45\n") == GpuReading(63.0, 97.0, 45.0)


def test_parse_reading_without_fan():
    assert parse_reading("63, 97, [N/A]") == GpuReading(63.0, 97.0, None)


@pytest.mark.parametrize("text", ["", "garbage", "x, y, z"])
def test_parse_reading_bad_input(text):
    assert parse_reading(text) is None


# --- controller -----------------------------------------------------------


def test_takes_control_from_current_speed_and_ramps():
    backend, clock = FakeBackend(), Clock()
    fan = _controller(backend, clock, ramp_sec=30.0)

    fan.tick(FanTier.RENTAL, _reading(fan=20.0), host_session_active=False)
    assert backend.sets == [20]          # starts where the driver left it
    assert fan.mode == "fixed"

    clock.t = 15.0                       # half way through a 20 -> 50 ramp
    fan.tick(FanTier.RENTAL, _reading(), host_session_active=False)
    assert backend.sets[-1] == 35

    clock.t = 30.0
    fan.tick(FanTier.RENTAL, _reading(), host_session_active=False)
    assert backend.sets[-1] == 50

    clock.t = 60.0
    fan.tick(FanTier.RENTAL, _reading(), host_session_active=False)
    assert backend.sets[-1] == 50 and len(backend.sets) == 3  # no repeat writes


def test_ramps_down_between_tiers():
    backend, clock = FakeBackend(), Clock()
    fan = _controller(backend, clock, ramp_sec=10.0)
    fan.tick(FanTier.RENTAL, _reading(fan=50.0), host_session_active=False)
    clock.t = 5.0
    fan.tick(FanTier.IDLE, _reading(), host_session_active=False)
    assert backend.sets[-1] == 50        # new ramp starts from current level
    clock.t = 10.0
    fan.tick(FanTier.IDLE, _reading(), host_session_active=False)
    assert backend.sets[-1] == 40
    clock.t = 15.0
    fan.tick(FanTier.IDLE, _reading(), host_session_active=False)
    assert backend.sets[-1] == 30


def test_zero_ramp_jumps_straight_to_level():
    backend, clock = FakeBackend(), Clock()
    fan = _controller(backend, clock, ramp_sec=0.0)
    fan.tick(FanTier.FILL, _reading(fan=0.0), host_session_active=False)
    assert backend.sets == [40]


def test_missing_fan_reading_starts_at_target():
    backend, clock = FakeBackend(), Clock()
    fan = _controller(backend, clock)
    fan.tick(FanTier.FILL, _reading(fan=None), host_session_active=False)
    assert backend.sets == [40]


def test_takes_control_from_nvml_target_not_nvidia_smi_reading():
    # RTX 3090 seen reporting fan.speed 0 % while its fans spin: the
    # backend's NVML target is the trustworthy starting point.
    backend, clock = FakeBackend(target=70), Clock()
    fan = _controller(backend, clock, ramp_sec=20.0)
    fan.tick(FanTier.IDLE, _reading(temp=70.0, fan=0.0), host_session_active=False)
    assert backend.sets == [70]          # eases down from 70, no drop to 0
    clock.t = 10.0
    fan.tick(FanTier.IDLE, _reading(fan=0.0), host_session_active=False)
    assert backend.sets[-1] == 50        # half way from 70 to 30


def test_get_speed_error_falls_back_to_reading():
    class Raising(FakeBackend):
        def get_speed(self):
            raise RuntimeError("boom")

    backend, clock = Raising(), Clock()
    fan = _controller(backend, clock)
    fan.tick(FanTier.RENTAL, _reading(fan=25.0), host_session_active=False)
    assert backend.sets == [25]
    assert fan.mode == "fixed"


def test_backend_without_get_speed_still_works():
    class Minimal:
        def __init__(self):
            self.sets = []

        def set_speed(self, pct):
            self.sets.append(pct)
            return True

        def restore_auto(self):
            return True

    backend, clock = Minimal(), Clock()
    _controller(backend, clock).tick(
        FanTier.FILL, _reading(fan=22.0), host_session_active=False,
    )
    assert backend.sets == [22]


def test_heat_guard_hands_back_with_hysteresis():
    backend, clock = FakeBackend(), Clock()
    fan = _controller(backend, clock)
    fan.tick(FanTier.IDLE, _reading(temp=60.0), host_session_active=False)
    assert fan.mode == "fixed"

    fan.tick(FanTier.IDLE, _reading(temp=81.0), host_session_active=False)
    assert fan.mode == "auto"
    assert backend.calls[-1] == ("auto", None)

    fan.tick(FanTier.IDLE, _reading(temp=75.0), host_session_active=False)
    assert fan.mode == "auto"            # still above the release point

    fan.tick(FanTier.IDLE, _reading(temp=70.0, fan=65.0), host_session_active=False)
    assert fan.mode == "fixed"
    assert backend.sets[-1] == 65        # resumes from the driver's speed


def test_host_session_hands_back_to_driver():
    backend, clock = FakeBackend(), Clock()
    fan = _controller(backend, clock)
    fan.tick(FanTier.FILL, _reading(), host_session_active=False)
    fan.tick(FanTier.FILL, _reading(), host_session_active=True)
    assert fan.mode == "auto"
    assert backend.calls[-1] == ("auto", None)


def test_no_reading_fails_safe_to_driver():
    backend, clock = FakeBackend(), Clock()
    fan = _controller(backend, clock)
    fan.tick(FanTier.FILL, _reading(), host_session_active=False)
    fan.tick(FanTier.FILL, None, host_session_active=False)
    assert fan.mode == "auto"


def test_backend_failure_disables_and_restores():
    backend, clock = FakeBackend(fail_set=True), Clock()
    fan = _controller(backend, clock)
    fan.tick(FanTier.FILL, _reading(), host_session_active=False)
    assert fan.mode == "disabled"
    assert backend.calls[-1] == ("auto", None)
    n = len(backend.calls)
    fan.tick(FanTier.RENTAL, _reading(), host_session_active=False)
    assert len(backend.calls) == n       # no further commands


def test_close_restores_driver_curve():
    backend, clock = FakeBackend(), Clock()
    fan = _controller(backend, clock)
    fan.tick(FanTier.FILL, _reading(), host_session_active=False)
    fan.close()
    assert backend.calls[-1] == ("auto", None)
    assert fan.mode == "auto"


def test_close_while_auto_issues_nothing():
    backend, clock = FakeBackend(), Clock()
    _controller(backend, clock).close()
    assert backend.calls == []


# --- helper backend / builder ----------------------------------------


def test_helper_backend_command_line(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert HelperBackend("/usr/local/sbin/hadcd-gpu-fan", 1).set_speed(42)
    assert seen["cmd"] == ["sudo", "-n", "/usr/local/sbin/hadcd-gpu-fan", "--gpu", "1", "set", "42"]
    assert HelperBackend("/usr/local/sbin/hadcd-gpu-fan", 0).restore_auto()
    assert seen["cmd"][-1] == "auto"


@pytest.mark.parametrize(
    "rc, stdout, expected",
    [(0, "40\n", 40), (0, "130\n", 100), (0, "garbage\n", None), (3, "", None)],
)
def test_helper_backend_get_speed(monkeypatch, rc, stdout, expected):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, rc, stdout, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert HelperBackend("/usr/local/sbin/hadcd-gpu-fan", 0).get_speed() == expected
    assert seen["cmd"][-1] == "get"


def test_helper_backend_reports_failure(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "sudo: a password is required"),
    )
    assert HelperBackend("/x", 0).set_speed(40) is False


class _Settings:
    fan_control_enabled = True
    fan_control_backend = "helper"
    fan_control_helper = "/usr/local/sbin/hadcd-gpu-fan"
    fan_gpu_index = 0
    fan_idle_pct = 30
    fan_fill_pct = 40
    fan_rental_pct = 50
    fan_ramp_sec = 30.0
    fan_guard_temp_c = 80.0
    fan_guard_release_c = 72.0


def test_builder_disabled_by_default():
    from agent.config import AgentSettings

    assert AgentSettings().fan_control_enabled is False
    s = _Settings()
    s.fan_control_enabled = False
    assert build_fan_controller(s) is None


def test_builder_helper_backend():
    assert isinstance(build_fan_controller(_Settings()), FanController)


def test_builder_unknown_backend():
    s = _Settings()
    s.fan_control_backend = "bogus"
    assert build_fan_controller(s) is None


# --- root helper script argument validation ---------------------------

_HELPER = Path(__file__).resolve().parents[2] / "scripts" / "hadcd-gpu-fan"


@pytest.mark.parametrize(
    "args",
    [[], ["set"], ["set", "101"], ["set", "-1"], ["set", "4O"], ["--gpu", "x", "auto"],
     ["--gpu", "99", "auto"], ["auto", "extra"], ["get", "extra"], ["rm", "-rf"]],
)
def test_helper_script_rejects_bad_args(args):
    out = subprocess.run(
        [sys.executable, str(_HELPER), *args], capture_output=True, text=True,
    )
    assert out.returncode == 2
