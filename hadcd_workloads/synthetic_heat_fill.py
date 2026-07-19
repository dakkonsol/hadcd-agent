"""Phase 9c — Synthetic heat-fill handler (Tier 3).

Produces waste heat via pure CPU/GPU computation — no mining binary,
no wallet address, no external service required.  It is always
available as a heat source on any node.

Tier position
-------------
Tier 3 means the dispatcher prefers any Tier 1 (operator work) or
Tier 2 (mining fill) task over this one.  The fill_injector keeps a
synthetic_heat_fill task queued whenever heat is demanded, so nodes
without mining configuration still deliver heat continuously.

CPU burn
--------
Uses ``multiprocessing`` to launch real OS processes — one per
configured thread — each running a tight 64-bit integer arithmetic
loop.  The GIL is irrelevant because each worker is a full process.
``SYNTHETIC_HEAT_THREADS=0`` (default) uses all logical cores minus
one, leaving headroom for OS tasks (same policy as XMRig).

GPU burn (optional)
-------------------
If ``SYNTHETIC_HEAT_GPU=true`` and PyTorch with CUDA is available,
a background thread performs repeated large-matrix multiplications
on ``cuda:0``.  If torch is not installed, or there is no CUDA
device, the GPU burn is silently skipped — only CPU burn runs.

No credentials, no income
--------------------------
This handler does not touch any wallet address or API key.  There is
nothing to log that requires CRA treatment.  The CSV session log only
records timing, thread count, and whether GPU burn was active.

Agent env vars (all optional — defaults are provided):

    SYNTHETIC_HEAT_THREADS   CPU threads.  0 (default) = cpu_count − 1.
    SYNTHETIC_HEAT_GPU       "true" to attempt GPU burn via PyTorch.
    SYNTHETIC_HEAT_POLL_SEC  Deadline-check cadence (default 1.0 s).
    SYNTHETIC_HEAT_LOG       CSV session log path.
"""

from __future__ import annotations

import csv
import ctypes
import logging
import multiprocessing
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from hadcd_workloads.registry import register

logger = logging.getLogger("hadcd.workloads.synthetic_heat_fill")


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


def _default_threads() -> int:
    try:
        return max(1, multiprocessing.cpu_count() - 1)
    except Exception:
        return 1


# ---------------------------------------------------------------------------
# CPU worker — runs in a child process
# ---------------------------------------------------------------------------

def _cpu_burn_worker(stop_flag: "ctypes.c_bool", _seed: int) -> None:  # type: ignore[name-defined]
    """Tight integer arithmetic loop until stop_flag is set.

    The loop avoids optimisation by mixing multiplication, addition, and
    a modulo that the optimiser cannot constant-fold.  The result is
    never stored or transmitted — it is pure heat.
    """
    x: int = _seed or 1
    while not stop_flag.value:
        for _ in range(10_000):
            x = (x * 6_364_136_223_846_793_005 + 1_442_695_040_888_963_407) % (2**63)


# ---------------------------------------------------------------------------
# GPU burn — runs in a daemon thread
# ---------------------------------------------------------------------------

def _gpu_burn_thread(stop_event: threading.Event) -> None:
    """Repeated large MatMul on cuda:0 until stop_event is set."""
    try:
        import torch  # type: ignore[import]
        if not torch.cuda.is_available():
            logger.debug("synthetic_heat_fill: CUDA not available — GPU burn skipped")
            return
        device = torch.device("cuda:0")
        # 4096×4096 fp32 — enough to saturate most consumer GPUs.
        a = torch.randn(4096, 4096, device=device, dtype=torch.float32)
        b = torch.randn(4096, 4096, device=device, dtype=torch.float32)
        logger.debug("synthetic_heat_fill: GPU burn started on %s", device)
        while not stop_event.is_set():
            _ = torch.matmul(a, b)
        logger.debug("synthetic_heat_fill: GPU burn stopped")
    except Exception as exc:
        logger.debug("synthetic_heat_fill: GPU burn unavailable: %s", exc)


# ---------------------------------------------------------------------------
# Session CSV logging
# ---------------------------------------------------------------------------

_CSV_HEADER = [
    "start_utc", "end_utc", "duration_sec",
    "threads", "gpu_burn_active",
]


def _log_session(
    log_path: str,
    start: datetime,
    end: datetime,
    threads: int,
    gpu_active: bool,
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
            threads,
            gpu_active,
        ])


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

@register("synthetic_heat_fill")
def run_synthetic_heat_fill(args: dict) -> dict:
    """Burn CPU (and optionally GPU) for ``duration_sec`` seconds.

    Always succeeds — no binary or wallet required.  If the GPU burn
    cannot start (no torch, no CUDA), it is silently skipped.
    """
    duration_sec: float = float(args.get("duration_sec", 1800))
    threads: int = _env_int("SYNTHETIC_HEAT_THREADS", 0)
    if threads <= 0:
        threads = _default_threads()

    want_gpu: bool = _env("SYNTHETIC_HEAT_GPU", "false").lower() == "true"
    poll_sec: float = _env_float("SYNTHETIC_HEAT_POLL_SEC", 1.0)
    log_path: str = _env(
        "SYNTHETIC_HEAT_LOG",
        "/var/lib/hadcd-agent/synthetic_heat_sessions.csv",
    )

    start_time = datetime.now(timezone.utc)
    deadline = time.monotonic() + duration_sec

    logger.info(
        "synthetic_heat_fill: starting burn (threads=%d, gpu=%s, duration=%.0fs)",
        threads, want_gpu, duration_sec,
    )

    # --- GPU burn (daemon thread — dies automatically if main exits) ------
    gpu_stop = threading.Event()
    gpu_thread: threading.Thread | None = None
    gpu_active = False
    if want_gpu:
        gpu_thread = threading.Thread(
            target=_gpu_burn_thread,
            args=(gpu_stop,),
            daemon=True,
            name="synthetic-heat-gpu",
        )
        gpu_thread.start()
        gpu_active = True  # optimistic; _gpu_burn_thread silently no-ops if unavailable

    # --- CPU burn (child processes) ----------------------------------------
    # A shared ctypes boolean in shared memory lets the main process signal
    # all workers to stop without needing a Queue or Pipe per process.
    stop_flag = multiprocessing.Value(ctypes.c_bool, False)
    workers: list[multiprocessing.Process] = []
    for i in range(threads):
        p = multiprocessing.Process(
            target=_cpu_burn_worker,
            args=(stop_flag, i + 1),
            daemon=True,
            name=f"synthetic-heat-cpu-{i}",
        )
        p.start()
        workers.append(p)

    # --- Run until deadline ------------------------------------------------
    try:
        while time.monotonic() < deadline:
            time.sleep(min(poll_sec, max(0.0, deadline - time.monotonic())))
    finally:
        # Signal workers to stop.
        stop_flag.value = True  # type: ignore[attr-defined]
        gpu_stop.set()

        # Give workers up to 5 s to exit, then kill them.
        for p in workers:
            p.join(timeout=5)
            if p.is_alive():
                p.kill()
                p.join(timeout=1)

        if gpu_thread is not None and gpu_thread.is_alive():
            gpu_thread.join(timeout=5)

    end_time = datetime.now(timezone.utc)
    actual_sec = (end_time - start_time).total_seconds()

    _log_session(log_path, start_time, end_time, threads, gpu_active and want_gpu)

    logger.info(
        "synthetic_heat_fill: session complete (actual=%.0fs, threads=%d, gpu=%s)",
        actual_sec, threads, gpu_active,
    )

    return {
        "threads": threads,
        "gpu_burn_active": gpu_active and want_gpu,
        "duration_requested_sec": duration_sec,
        "actual_sec": round(actual_sec),
        "session_start": start_time.isoformat(),
        "session_end": end_time.isoformat(),
    }
