"""Fake BMS demand writer for development and testing.

Writes synthetic heat-demand JSON to the BMS file that the agent's
``file`` adapter reads (``BMS_SOURCE=file``).  Useful for exercising
the full dispatch loop — agent reports demand, dispatcher routes a
task, GPU runs it, result comes back — without a real thermostat.

The heat from the GPU is real; only the demand signal is synthetic.

Usage::

    python -m agent.fake_demand [options]

Modes
-----
constant (default)
    Always write full demand.  The dispatcher sees the building as
    continuously calling for heat and routes work immediately.

off
    Always write zero demand.  Useful for testing the idle / no-work
    path or for cleanly stopping a previous fake run by leaving the
    file at zero.

cycle
    Alternate between full demand and zero on a wall-clock timer.
    Good for testing that the agent correctly transitions between
    "work routed" and "work recalled / not offered".

Examples
--------
Constant 5 kW demand to the default Windows path::

    python -m agent.fake_demand

Cycle — 5 min heating, 2 min idle, custom file::

    python -m agent.fake_demand --mode cycle --on 300 --off 120 \\
        --output C:/ProgramData/hadcd-agent/bms.json

Zero-out (stop a running test cleanly)::

    python -m agent.fake_demand --mode off --once

Docker (via docker-compose)::

    docker compose --profile fake-demand up fake-demand
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import sys
import tempfile
import time
from pathlib import Path


# ── defaults ──────────────────────────────────────────────────────────────────

def _default_bms_path() -> str:
    """Platform-appropriate default path, matching config.env.example."""
    if platform.system() == "Windows":
        return r"C:\ProgramData\hadcd-agent\bms.json"
    return "/var/lib/hadcd-agent/bms.json"


_DEFAULT_MEASURED_KW = 5.0
_DEFAULT_SETPOINT_C = 21.0
_DEFAULT_ROOM_TEMP_C = 18.5
_DEFAULT_EXPECTED_WINDOW_SEC = 1800
_DEFAULT_INTERVAL_SEC = 15
_DEFAULT_CYCLE_ON_SEC = 300   # 5 min heating
_DEFAULT_CYCLE_OFF_SEC = 120  # 2 min idle


# ── JSON helpers ───────────────────────────────────────────────────────────────

def _demand_payload(
    measured_kw: float,
    setpoint_c: float,
    room_temp_c: float,
    expected_window_sec: int,
) -> dict:
    return {
        "measured_kw": measured_kw,
        "setpoint_c": setpoint_c,
        "room_temp_c": room_temp_c,
        "expected_window_sec": expected_window_sec,
    }


def _zero_payload(
    setpoint_c: float,
    room_temp_c: float,
) -> dict:
    return {
        "measured_kw": 0.0,
        "setpoint_c": setpoint_c,
        "room_temp_c": room_temp_c,
        "expected_window_sec": 0,
    }


def write_payload(path: Path, payload: dict) -> None:
    """Write *payload* to *path* atomically (tmp-file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a sibling temp file first so the agent never reads a
    # partial write.
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── status line ───────────────────────────────────────────────────────────────

def _status(label: str, payload: dict, path: Path) -> None:
    kw = payload["measured_kw"]
    if kw > 0:
        icon = "🔥"
        state = f"HEATING  {kw:.1f} kW  →  {path}"
    else:
        icon = "❄️ "
        state = f"IDLE     0.0 kW  →  {path}"
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {icon}  {label:8s}  {state}", flush=True)


# ── modes ──────────────────────────────────────────────────────────────────────

def run_constant(
    path: Path,
    measured_kw: float,
    setpoint_c: float,
    room_temp_c: float,
    expected_window_sec: int,
    interval_sec: float,
    once: bool,
) -> None:
    payload = _demand_payload(measured_kw, setpoint_c, room_temp_c,
                              expected_window_sec)
    while True:
        write_payload(path, payload)
        _status("constant", payload, path)
        if once:
            break
        time.sleep(interval_sec)


def run_off(
    path: Path,
    setpoint_c: float,
    room_temp_c: float,
    interval_sec: float,
    once: bool,
) -> None:
    payload = _zero_payload(setpoint_c, room_temp_c)
    while True:
        write_payload(path, payload)
        _status("off", payload, path)
        if once:
            break
        time.sleep(interval_sec)


def run_cycle(
    path: Path,
    measured_kw: float,
    setpoint_c: float,
    room_temp_c: float,
    expected_window_sec: int,
    interval_sec: float,
    on_sec: float,
    off_sec: float,
) -> None:
    """Cycle between full demand and zero.

    The cycle ticks every *interval_sec* and switches phase based on
    elapsed wall-clock time, so the BMS file stays fresh regardless.
    """
    demand_payload = _demand_payload(measured_kw, setpoint_c, room_temp_c,
                                     expected_window_sec)
    idle_payload = _zero_payload(setpoint_c, room_temp_c)
    period = on_sec + off_sec
    cycle_start = time.monotonic()

    print(
        f"  Cycle: {on_sec:.0f}s heating  /  {off_sec:.0f}s idle"
        f"  (period {period:.0f}s)",
        flush=True,
    )

    while True:
        elapsed = (time.monotonic() - cycle_start) % period
        if elapsed < on_sec:
            phase_label = "cycle-ON"
            payload = demand_payload
            secs_left = on_sec - elapsed
        else:
            phase_label = "cycle-OFF"
            payload = idle_payload
            secs_left = period - elapsed

        write_payload(path, payload)
        _status(phase_label, payload, path)
        print(f"         phase ends in {secs_left:.0f}s", flush=True)
        time.sleep(interval_sec)


# ── CLI ────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m agent.fake_demand",
        description=(
            "Write synthetic heat-demand JSON to the BMS file so the agent "
            "reports demand without a real thermostat. "
            "Press Ctrl+C to stop."
        ),
    )
    p.add_argument(
        "--mode",
        choices=["constant", "off", "cycle"],
        default="constant",
        help=(
            "constant: always report full demand (default). "
            "off: always zero. "
            "cycle: alternate on/off on a timer."
        ),
    )
    p.add_argument(
        "--output", "-o",
        default=_default_bms_path(),
        metavar="PATH",
        help=f"BMS JSON file to write. Default: {_default_bms_path()}",
    )
    p.add_argument(
        "--measured-kw",
        type=float,
        default=_DEFAULT_MEASURED_KW,
        metavar="KW",
        help=f"Heat demand to report when heating. Default: {_DEFAULT_MEASURED_KW}",
    )
    p.add_argument(
        "--setpoint-c",
        type=float,
        default=_DEFAULT_SETPOINT_C,
        metavar="C",
        help=f"Thermostat setpoint in °C. Default: {_DEFAULT_SETPOINT_C}",
    )
    p.add_argument(
        "--room-temp-c",
        type=float,
        default=_DEFAULT_ROOM_TEMP_C,
        metavar="C",
        help=f"Room temperature in °C. Default: {_DEFAULT_ROOM_TEMP_C}",
    )
    p.add_argument(
        "--expected-window-sec",
        type=int,
        default=_DEFAULT_EXPECTED_WINDOW_SEC,
        metavar="SEC",
        help=(
            "How long the building expects to keep wanting heat. "
            f"Default: {_DEFAULT_EXPECTED_WINDOW_SEC}"
        ),
    )
    p.add_argument(
        "--interval",
        type=float,
        default=_DEFAULT_INTERVAL_SEC,
        metavar="SEC",
        help=f"Seconds between writes. Default: {_DEFAULT_INTERVAL_SEC}",
    )
    p.add_argument(
        "--on",
        type=float,
        default=_DEFAULT_CYCLE_ON_SEC,
        metavar="SEC",
        dest="cycle_on_sec",
        help=(
            f"[cycle mode] Seconds of heating per cycle. "
            f"Default: {_DEFAULT_CYCLE_ON_SEC}"
        ),
    )
    p.add_argument(
        "--off",
        type=float,
        default=_DEFAULT_CYCLE_OFF_SEC,
        metavar="SEC",
        dest="cycle_off_sec",
        help=(
            f"[cycle mode] Seconds of idle per cycle. "
            f"Default: {_DEFAULT_CYCLE_OFF_SEC}"
        ),
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Write once and exit (useful for one-shot setup or scripts).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    path = Path(args.output)

    # Graceful shutdown on SIGTERM (Docker stop sends this).
    _stop = [False]

    def _handle_sigterm(*_):  # pragma: no cover
        _stop[0] = True
        print("\n[fake-demand] SIGTERM received — writing zero and exiting.")
        write_payload(path, _zero_payload(args.setpoint_c, args.room_temp_c))
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    print(
        f"[fake-demand] mode={args.mode}  output={path}  "
        f"interval={args.interval}s"
    )
    print("[fake-demand] Press Ctrl+C to stop.\n")

    try:
        if args.mode == "constant":
            run_constant(
                path=path,
                measured_kw=args.measured_kw,
                setpoint_c=args.setpoint_c,
                room_temp_c=args.room_temp_c,
                expected_window_sec=args.expected_window_sec,
                interval_sec=args.interval,
                once=args.once,
            )
        elif args.mode == "off":
            run_off(
                path=path,
                setpoint_c=args.setpoint_c,
                room_temp_c=args.room_temp_c,
                interval_sec=args.interval,
                once=args.once,
            )
        elif args.mode == "cycle":
            run_cycle(
                path=path,
                measured_kw=args.measured_kw,
                setpoint_c=args.setpoint_c,
                room_temp_c=args.room_temp_c,
                expected_window_sec=args.expected_window_sec,
                interval_sec=args.interval,
                on_sec=args.cycle_on_sec,
                off_sec=args.cycle_off_sec,
            )
    except KeyboardInterrupt:
        print("\n[fake-demand] Interrupted — writing zero and exiting.")
        write_payload(path, _zero_payload(args.setpoint_c, args.room_temp_c))

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
