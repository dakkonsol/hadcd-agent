"""The agent's task executor.

Runs offloaded tasks in a `ProcessPoolExecutor`, mirroring the
central server's local executor pattern (see
`backend/app/executor/worker.py`). Process isolation matters for two
reasons: a handler that crashes or hangs does not take the agent
down with it, and the pool's worker processes can be killed cleanly
when shutting down or honouring a recall.

The handler set is exactly the central server's, courtesy of the
shared `hadcd_workloads` package.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

from hadcd_workloads import run_registered

logger = logging.getLogger("hadcd.agent.executor")


@dataclass
class TaskOutcome:
    """The outcome of running one offloaded task."""

    success: bool
    duration_sec: float
    result: dict | None
    error: str | None


class AgentExecutor:
    """A bounded subprocess pool. One instance per agent."""

    def __init__(self, concurrency: int) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self.concurrency = concurrency
        # 'spawn' for the same reason the backend uses it: fork-from-
        # asyncio is a known deadlock source, and spawn starts each
        # worker clean.
        self._pool = ProcessPoolExecutor(
            max_workers=concurrency,
            mp_context=mp.get_context("spawn"),
        )

    async def run(self, task_type: str, args: dict) -> TaskOutcome:
        """Run one task to completion and return its outcome.

        Wall-clock duration is measured around the pool submission, so
        time spent queueing behind concurrency-limit-N other tasks is
        counted — that is what the backend will record as the run.
        """
        loop = asyncio.get_running_loop()
        start = time.monotonic()
        try:
            result = await loop.run_in_executor(
                self._pool, run_registered, task_type, args
            )
        except Exception as exc:
            duration = time.monotonic() - start
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("task %s failed: %s", task_type, error)
            return TaskOutcome(
                success=False, duration_sec=duration, result=None, error=error
            )
        duration = time.monotonic() - start
        return TaskOutcome(
            success=True, duration_sec=duration, result=result, error=None
        )

    def shutdown(self) -> None:
        # Don't wait — the agent's outer drain logic gives in-flight
        # tasks a grace period and the pool's workers terminate when
        # the parent exits anyway.
        self._pool.shutdown(wait=False, cancel_futures=True)
