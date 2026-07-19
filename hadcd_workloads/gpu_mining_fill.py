"""GPU mining fill handler — T-Rex Miner → NiceHash stratum (Phase 9b).

Runs the T-Rex miner binary for the task's declared duration, then exits
cleanly.  T-Rex is an NVIDIA-native Linux miner that speaks NiceHash's
standard stratum protocol — payouts still land in BTC on the NiceHash
dashboard.  NiceHash's own Excavator binary is Windows-only, so T-Rex is
the Linux replacement.

T-Rex GitHub: https://github.com/trexminer/T-Rex

Pause-on-GPU-pressure
---------------------
The handler polls ``nvidia-smi`` every ``MINING_POLL_INTERVAL_SEC``
seconds.  If it detects a CUDA compute process that is NOT the miner
(i.e. the user launched a game, a local AI app, etc.), it suspends
the miner process to yield the GPU, and resumes it when the pressure
clears.  This is a secondary safeguard: the primary gate is the
dispatcher's ``fill_gating.should_pause_fill_tiers`` check which
prevents fill tasks from reaching a node while a Sunshine session is
active.

Income logging
--------------
Each session is appended to a CSV at ``MINING_PAYOUT_LOG`` (default
``/var/lib/hadcd-agent/gpu_mining_sessions.csv``).  The CSV captures
start time, duration, worker name, and GPU model so payout
reconciliation against the NiceHash dashboard is straightforward.
Actual BTC payout amounts must be confirmed from the NiceHash API or
dashboard — the miner does not expose a per-session payout figure.

Agent env vars (NOT task args — wallet addresses must not flow through
the task ledger):

    NICEHASH_TREX_PATH          Path to the t-rex executable.
                                Required; handler skips gracefully
                                if missing.
    NICEHASH_WALLET             BTC payout address.  Required.
    NICEHASH_WORKER_NAME        Worker label in the NiceHash dashboard.
                                Default: the machine's hostname.
    NICEHASH_POOL_HOST          NiceHash stratum host.
                                Default: auto.nicehash.com
    NICEHASH_POOL_PORT          Stratum port. Default: 9200.
    NICEHASH_ALGO               Mining algorithm passed to T-Rex.
                                Default: ethash (NiceHash DaggerHashimoto).
    MINING_GPU_INDEX            GPU index to mine on. Default: 0.
    MINING_GPU_PRESSURE_PCT     Non-miner GPU utilisation that triggers
                                a pause. Default: 20.
    MINING_GPU_RESUME_PCT       Utilisation below which mining resumes.
                                Default: 10.
    MINING_POLL_INTERVAL_SEC    GPU-pressure poll cadence. Default: 10.
    MINING_PAYOUT_LOG           CSV session log path.
"""

from __future__ import annotations

import csv
import logging
import os
import platform
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from hadcd_workloads.registry import register

logger = logging.getLogger("hadcd.workloads.gpu_mining_fill")

# ---------------------------------------------------------------------------
# Config helpers (read from environment at handler call time so the handler
# works in the agent process without importing agent.config directly)
# ---------------------------------------------------------------------------

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# nvidia-smi helpers
# ---------------------------------------------------------------------------

def _compute_pids_excluding(miner_pid: int) -> list[int]:
    """PIDs of CUDA compute processes that are NOT the miner."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        pids = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pid = int(line)
                if pid != miner_pid:
                    pids.append(pid)
            except ValueError:
                pass
        return pids
    except Exception:
        return []


def _gpu_model() -> str:
    """GPU model string for the session log."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip().split("\n")[0].strip() or "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Process suspend / resume (cross-platform via psutil if available,
# otherwise via SIGSTOP/SIGCONT on POSIX)
# ---------------------------------------------------------------------------

def _suspend(proc: "subprocess.Popen") -> None:
    try:
        import psutil  # type: ignore[import]
        psutil.Process(proc.pid).suspend()
    except ImportError:
        if platform.system() != "Windows":
            import signal
            os.kill(proc.pid, signal.SIGSTOP)


def _resume(proc: "subprocess.Popen") -> None:
    try:
        import psutil  # type: ignore[import]
        psutil.Process(proc.pid).resume()
    except ImportError:
        if platform.system() != "Windows":
            import signal
            os.kill(proc.pid, signal.SIGCONT)


# ---------------------------------------------------------------------------
# Session CSV logging
# ---------------------------------------------------------------------------

_CSV_HEADER = [
    "start_utc", "end_utc", "duration_sec",
    "worker_name", "gpu_model", "pool_host", "wallet_prefix",
]


def _log_session(
    log_path: str,
    start: datetime,
    end: datetime,
    worker: str,
    gpu: str,
    pool: str,
    wallet: str,
) -> None:
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(_CSV_HEADER)
        w.writerow([
            start.isoformat(),
            end.isoformat(),
            round((end - start).total_seconds()),
            worker,
            gpu,
            pool,
            # Only log first 8 chars of wallet — enough to confirm the
            # right address without exposing the full key in logs.
            wallet[:8] + "…" if len(wallet) > 8 else wallet,
        ])


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

@register("gpu_mining_fill")
def run_gpu_mining_fill(args: dict) -> dict:
    """Run NiceHash excavator for ``duration_sec`` seconds.

    Returns immediately with a skip result if the excavator binary is
    not configured — nodes without a NiceHash setup just won't run this
    handler, which is fine.
    """
    duration_sec: float = float(args.get("duration_sec", 1800))

    trex_path = _env("NICEHASH_TREX_PATH")
    wallet = _env("NICEHASH_WALLET")

    if not trex_path:
        logger.info(
            "gpu_mining_fill: NICEHASH_TREX_PATH not set — skipping "
            "(node not configured for NiceHash mining)"
        )
        return {"skipped": True, "reason": "NICEHASH_TREX_PATH not configured"}

    if not wallet:
        logger.warning(
            "gpu_mining_fill: NICEHASH_WALLET not set — skipping"
        )
        return {"skipped": True, "reason": "NICEHASH_WALLET not configured"}

    if not Path(trex_path).is_file():
        logger.warning(
            "gpu_mining_fill: t-rex not found at %s — skipping",
            trex_path,
        )
        return {"skipped": True, "reason": f"t-rex not found: {trex_path}"}

    worker = _env("NICEHASH_WORKER_NAME") or socket.gethostname()
    pool_host = _env("NICEHASH_POOL_HOST") or "auto.nicehash.com"
    pool_port = _env_int("NICEHASH_POOL_PORT", 9200)
    algo = _env("NICEHASH_ALGO") or "ethash"
    gpu_index = _env_int("MINING_GPU_INDEX", 0)
    pressure_pct = _env_float("MINING_GPU_PRESSURE_PCT", 20.0)
    resume_pct = _env_float("MINING_GPU_RESUME_PCT", 10.0)
    poll_sec = _env_float("MINING_POLL_INTERVAL_SEC", 10.0)
    log_path = _env(
        "MINING_PAYOUT_LOG",
        "/var/lib/hadcd-agent/gpu_mining_sessions.csv",
    )

    # T-Rex CLI: -a <algo> -o stratum+tcp://host:port -u wallet.worker -p x
    #            --gpu-id <index> --no-watchdog --no-color
    # NiceHash auto-selects the most profitable algo on the pool side when
    # using auto.nicehash.com:9200.  Override NICEHASH_ALGO in agent.env
    # to pin a specific algorithm (e.g. kawpow, autolykos2).
    cmd = [
        trex_path,
        "-a", algo,
        "-o", f"stratum+tcp://{pool_host}:{pool_port}",
        "-u", f"{wallet}.{worker}",
        "-p", "x",
        "--gpu-id", str(gpu_index),
        "--no-watchdog",   # HADCD manages the process lifecycle
        "--no-color",      # cleaner logs in journald
        "--api-bind-http", "0",  # disable T-Rex HTTP API — not needed
    ]

    gpu_model = _gpu_model()
    start_time = datetime.now(timezone.utc)
    deadline = time.monotonic() + duration_sec
    paused = False
    total_paused_sec: float = 0.0
    pause_start: float = 0.0

    logger.info(
        "gpu_mining_fill: starting t-rex (worker=%s, gpu=%s, algo=%s, "
        "pool=%s:%d, duration=%.0fs)",
        worker, gpu_model, algo, pool_host, pool_port, duration_sec,
    )

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        while time.monotonic() < deadline:
            time.sleep(min(poll_sec, deadline - time.monotonic()))

            # Check for non-miner GPU pressure.
            other_pids = _compute_pids_excluding(proc.pid)
            has_pressure = len(other_pids) > 0

            if has_pressure and not paused:
                logger.info(
                    "gpu_mining_fill: GPU pressure detected (pids=%s) — "
                    "suspending miner",
                    other_pids,
                )
                _suspend(proc)
                paused = True
                pause_start = time.monotonic()

            elif not has_pressure and paused:
                logger.info("gpu_mining_fill: GPU pressure cleared — resuming miner")
                _resume(proc)
                total_paused_sec += time.monotonic() - pause_start
                paused = False

            # Exit early if the miner died unexpectedly.
            if proc.poll() is not None:
                logger.warning(
                    "gpu_mining_fill: t-rex exited early (rc=%d)",
                    proc.returncode,
                )
                break

    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    end_time = datetime.now(timezone.utc)
    active_sec = (end_time - start_time).total_seconds() - total_paused_sec

    _log_session(
        log_path, start_time, end_time,
        worker, gpu_model, pool_host, wallet,
    )

    logger.info(
        "gpu_mining_fill: session complete "
        "(active=%.0fs, paused=%.0fs, total=%.0fs)",
        active_sec, total_paused_sec,
        (end_time - start_time).total_seconds(),
    )

    return {
        "worker": worker,
        "gpu_model": gpu_model,
        "pool_host": pool_host,
        "duration_requested_sec": duration_sec,
        "active_mining_sec": round(active_sec),
        "paused_sec": round(total_paused_sec),
        "session_start": start_time.isoformat(),
        "session_end": end_time.isoformat(),
    }
