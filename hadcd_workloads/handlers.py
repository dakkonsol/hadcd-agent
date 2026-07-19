"""Built-in synthetic task handlers.

These exist so the system has real work to schedule and execute
during development, simulation, and node-agent verification.
`seconds` / `n` / `size` are the knobs the simulator and operator
tests use to shape task duration and CPU load.

Real workloads plug in as peers of this module: ship a Python module
that imports `hadcd_workloads.register` and decorates its handlers.
"""

from __future__ import annotations

import math
import random
import time

from hadcd_workloads.registry import register


@register("sleep")
def _sleep(args: dict) -> dict:
    """Idle for `seconds`. Models a task that waits rather than computes."""
    seconds = float(args.get("seconds", 1.0))
    time.sleep(seconds)
    return {"slept_seconds": seconds}


@register("cpu_burn")
def _cpu_burn(args: dict) -> dict:
    """Busy-loop for `seconds`, consuming real CPU.

    This is the primary 'produces waste heat' handler: the tight loop
    keeps a core fully occupied for the requested duration.
    """
    seconds = float(args.get("seconds", 1.0))
    deadline = time.monotonic() + seconds
    iterations = 0
    acc = 0.0
    while time.monotonic() < deadline:
        # A little arithmetic so the loop is not optimised away.
        acc += math.sqrt((iterations % 1000) + 1)
        iterations += 1
    return {"burned_seconds": seconds, "iterations": iterations}


@register("fib")
def _fib(args: dict) -> dict:
    """Compute the n-th Fibonacci number the slow (recursive) way.

    Intentionally the exponential recursive form so it is genuine CPU
    work. `n` is capped to keep a single task bounded.
    """
    n = int(args.get("n", 28))
    if n < 0 or n > 35:
        raise ValueError("fib 'n' must be between 0 and 35")

    def rec(k: int) -> int:
        return k if k < 2 else rec(k - 1) + rec(k - 2)

    return {"n": n, "value": rec(n)}


@register("matrix_multiply")
def _matrix_multiply(args: dict) -> dict:
    """Multiply two random `size`x`size` matrices in pure Python.

    Pure Python (no numpy) on purpose — keeps dependencies minimal and
    the CPU cost predictable. Cost scales as size^3.
    """
    size = int(args.get("size", 60))
    if size < 1 or size > 400:
        raise ValueError("matrix_multiply 'size' must be between 1 and 400")
    rng = random.Random(int(args.get("seed", 0)))

    a = [[rng.random() for _ in range(size)] for _ in range(size)]
    b = [[rng.random() for _ in range(size)] for _ in range(size)]
    c = [[0.0] * size for _ in range(size)]
    for i in range(size):
        a_row = a[i]
        c_row = c[i]
        for k in range(size):
            a_ik = a_row[k]
            b_row = b[k]
            for j in range(size):
                c_row[j] += a_ik * b_row[j]

    # Trace as a cheap, deterministic checksum of the result.
    checksum = sum(c[i][i] for i in range(size))
    return {"size": size, "trace_checksum": checksum}
