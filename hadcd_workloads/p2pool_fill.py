"""CPU mining fill handler — XMRig → P2Pool → Monero (Phase 9b).

Runs XMRig targeting a P2Pool node for the task's declared duration,
then exits cleanly.  The backend's fill_injector re-queues a fresh task
so heat demand is served continuously.

Why P2Pool?
-----------
P2Pool is a decentralised Monero mining pool that pays directly to your
wallet on a per-share-block basis.  There is no pool operator, no KYC,
and crucially no PPLNS share-rotation penalty for pause/resume cycling —
each share pays regardless of when you started.  This makes it ideal for
HADCD's intermittent heat-driven mining model.

Income logging
--------------
Each session is appended to a CSV at ``CPU_MINING_PAYOUT_LOG`` so you
have CRA-ready records of when mining ran.  Actual XMR amounts must be
reconciled from the P2Pool observer or your wallet — XMRig does not
expose per-session payout to stdout in a machine-readable way.

Agent env vars:

    XMRIG_PATH              Path to xmrig executable. Required.
    XMR_WALLET_ADDRESS      Monero wallet address for payouts. Required.
    P2POOL_NODE_URL         P2Pool stratum URL.
                            Default: p2pool.io:3333  (P2Pool mini;
                            appropriate for RTX-class GPUs on CPU mining.
                            Use p2pool.io:3334 for the main chain.)
    XMRIG_WORKER_NAME       Worker label shown in P2Pool observer.
                            Default: hostname.
    XMRIG_THREADS           CPU threads to use. Default: all logical
                            cores minus one (leaves headroom for the OS).
    CPU_MINING_POLL_SEC     How often to check if the task is still within
                            its deadline. Default: 15.
    CPU_MINING_PAYOUT_LOG   CSV session log path.
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

logger = logging.getLogger("hadcd.workloads.p2pool_fill")


# ---------------------------------------------------------------------------
# Config helpers
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
# CPU thread default: all cores minus one
# ---------------------------------------------------------------------------

def _default_threads() -> int:
    try:
        import multiprocessing
        return max(1, multiprocessing.cpu_count() - 1)
    except Exception:
        return 1


# ---------------------------------------------------------------------------
# Session CSV logging
# ---------------------------------------------------------------------------

_CSV_HEADER = [
    "start_utc", "end_utc", "duration_sec",
    "worker_name", "threads", "pool_url", "wallet_prefix",
]


def _log_session(
    log_path: str,
    start: datetime,
    end: datetime,
    worker: str,
    threads: int,
    pool_url: str,
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
            threads,
            pool_url,
            wallet[:8] + "…" if len(wallet) > 8 else wallet,
        ])


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

@register("p2pool_fill")
def run_p2pool_fill(args: dict) -> dict:
    """Run XMRig against P2Pool for ``duration_sec`` seconds.

    Returns immediately with a skip result if XMRig is not configured —
    nodes without a Monero setup just won't run this handler.
    """
    duration_sec: float = float(args.get("duration_sec", 1800))

    xmrig_path = _env("XMRIG_PATH")
    wallet = _env("XMR_WALLET_ADDRESS")

    if not xmrig_path:
        logger.info(
            "p2pool_fill: XMRIG_PATH not set — skipping "
            "(node not configured for P2Pool mining)"
        )
        return {"skipped": True, "reason": "XMRIG_PATH not configured"}

    if not wallet:
        logger.warning("p2pool_fill: XMR_WALLET_ADDRESS not set — skipping")
        return {"skipped": True, "reason": "XMR_WALLET_ADDRESS not configured"}

    if not Path(xmrig_path).is_file():
        logger.warning(
            "p2pool_fill: xmrig not found at %s — skipping", xmrig_path
        )
        return {"skipped": True, "reason": f"xmrig not found: {xmrig_path}"}

    worker = _env("XMRIG_WORKER_NAME") or socket.gethostname()
    pool_url = _env("P2POOL_NODE_URL") or "p2pool.io:3333"
    _t = _env_int("XMRIG_THREADS", 0)
    threads = _t if _t > 0 else _default_threads()  # 0 = auto: all cores minus one
    poll_sec = _env_float("CPU_MINING_POLL_SEC", 15.0)
    log_path = _env(
        "CPU_MINING_PAYOUT_LOG",
        "/var/lib/hadcd-agent/cpu_mining_sessions.csv",
    )

    # XMRig command line:
    # -o pool  -u wallet+worker  --coin monero  -t threads --no-color
    cmd = [
        xmrig_path,
        "--url", pool_url,
        "--user", f"{wallet}+{worker}",
        "--coin", "monero",
        "--threads", str(threads),
        "--no-color",
        "--log-file", os.devnull,
    ]

    start_time = datetime.now(timezone.utc)
    deadline = time.monotonic() + duration_sec

    logger.info(
        "p2pool_fill: starting xmrig (worker=%s, threads=%d, "
        "pool=%s, duration=%.0fs)",
        worker, threads, pool_url, duration_sec,
    )

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        while time.monotonic() < deadline:
            time.sleep(min(poll_sec, deadline - time.monotonic()))
            if proc.poll() is not None:
                logger.warning(
                    "p2pool_fill: xmrig exited early (rc=%d)",
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
    actual_sec = (end_time - start_time).total_seconds()

    _log_session(
        log_path, start_time, end_time,
        worker, threads, pool_url, wallet,
    )

    logger.info(
        "p2pool_fill: session complete (actual=%.0fs, threads=%d)",
        actual_sec, threads,
    )

    return {
        "worker": worker,
        "pool_url": pool_url,
        "threads": threads,
        "duration_requested_sec": duration_sec,
        "actual_sec": round(actual_sec),
        "session_start": start_time.isoformat(),
        "session_end": end_time.isoformat(),
    }
