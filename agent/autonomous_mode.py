"""Autonomous fallback mining — runs when the dispatcher is offline.

When the HADCD backend cannot be reached for
``AUTONOMOUS_FALLBACK_AFTER_SEC`` consecutive seconds (default 60,
roughly 6 missed heartbeats), the agent enters autonomous mode:

Mining
------
The local BMS (thermostat / heat source) is polled directly.
While heat is demanded (``measured_kw > 0``):
  * GPU mining — runs the ``gpu_mining_fill`` handler (T-Rex → NiceHash)
  * CPU mining — runs the ``p2pool_fill`` handler (XMRig → P2Pool)
Both run concurrently for ``AUTONOMOUS_CHUNK_SEC`` seconds (default 120).
After each chunk the thermostat is re-checked; mining stops as soon as
heat is no longer demanded.

Fallback heat sources (Phase 18k)
----------------------------------
When both mining handlers are skipped (not configured), the agent falls
back to one of two secondary heat sources, tried in priority order:

  1. HA space heater — if ``HA_HEATER_ENTITY_ID`` is set, a POST to the
     local Home Assistant REST API turns on the entity (typically a
     switch controlling a physical space heater).  HA runs on the LAN
     so this works even when the internet is down.  The heater is left
     on for ``AUTONOMOUS_CHUNK_SEC`` seconds between demand re-checks,
     and is explicitly turned off when heat is no longer demanded or
     when the agent exits autonomous mode.

  2. Synthetic heat fill — last resort: pure CPU/GPU burn that is always
     available without a wallet or external binary.  Used only when
     neither mining nor an HA heater is configured.

Vast.AI (Phase 15b/15c)
-----------------------
Mining and Vast.AI rental are mutually exclusive — the GPU cannot serve
both at once.  The heartbeat loop in ``agent.py`` reads the VastProvider
state before starting or continuing the miner:

  LISTED / LISTING   → warm weather → GPU belongs to the renter → no mining
  UNLISTED / UNLISTING → cold weather → GPU is free → mine from thermostat

This means the miner is automatically paused if Phase 15b lists the machine
on Vast.AI (weather warmed up mid-offline period), and resumes once the
VastProvider transitions back to UNLISTED (cold window returns or renter
finishes).  The miner itself has no direct Vast.AI dependency.

Reconnect
---------
The heartbeat loop calls ``stop()`` on first successful heartbeat.
The current mining chunk is allowed to finish (up to AUTONOMOUS_CHUNK_SEC
seconds); then normal task dispatch resumes.  If the HA heater is on when
``stop()`` is called it is turned off in the ``finally`` block before the
task exits.

Session logging
---------------
Autonomous sessions are appended to the same CSV files as dispatched
sessions (``MINING_PAYOUT_LOG`` / ``CPU_MINING_PAYOUT_LOG``), so payout
reconciliation works identically.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from agent.heat_source import HeatSource

logger = logging.getLogger("hadcd.agent.autonomous")


class AutonomousMiner:
    """Manages mining directly from the local thermostat when offline."""

    def __init__(
        self,
        chunk_sec: float = 120.0,
        poll_sec: float = 30.0,
        ha_url: str = "",
        ha_token: str = "",
        ha_heater_entity_id: str = "",
    ) -> None:
        self._chunk_sec = chunk_sec
        self._poll_sec = poll_sec
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._ha_url = ha_url.rstrip("/")
        self._ha_token = ha_token
        self._ha_heater_entity_id = ha_heater_entity_id

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def _ha_configured(self) -> bool:
        return bool(self._ha_url and self._ha_token and self._ha_heater_entity_id)

    async def start(self, heat_source: "HeatSource") -> None:
        """Enter autonomous mode."""
        if self.active:
            return
        logger.warning(
            "\n"
            "  ============================================================\n"
            "   DISPATCHER OFFLINE — autonomous mode active\n"
            "   Mining driven by local thermostat until reconnected.\n"
            "  ============================================================"
        )
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop(heat_source))

    async def stop(self) -> None:
        """Exit autonomous mode and wait for any running chunk to finish."""
        if not self.active:
            return
        logger.info(
            "autonomous: dispatcher back online — "
            "waiting for current chunk to finish, then resuming normal dispatch"
        )
        self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=self._chunk_sec + 30)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _heater_service(self, service: str) -> None:
        """POST to HA REST API to turn_on or turn_off the heater entity.

        Uses the homeassistant domain so both switch and input_boolean
        entities are supported without knowing the specific domain.
        Failures are logged and swallowed — a missed heater call should
        not abort the autonomous loop.
        """
        if not self._ha_configured:
            return
        url = f"{self._ha_url}/api/services/homeassistant/{service}"
        headers = {
            "Authorization": f"Bearer {self._ha_token}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url,
                    json={"entity_id": self._ha_heater_entity_id},
                    headers=headers,
                )
                resp.raise_for_status()
                logger.info(
                    "autonomous ha-heater: %s %s (HTTP %s)",
                    service,
                    self._ha_heater_entity_id,
                    resp.status_code,
                )
        except Exception as exc:
            logger.warning("autonomous ha-heater: %s failed: %s", service, exc)

    async def _loop(self, heat_source: "HeatSource") -> None:
        loop = asyncio.get_running_loop()
        heater_on = False

        try:
            while not self._stop_event.is_set():
                # Read the thermostat.
                try:
                    reading = await heat_source.read()
                    demanded = reading.measured_kw > 0
                except Exception as exc:
                    logger.warning("autonomous: heat source read failed: %s", exc)
                    demanded = False

                if not demanded:
                    if heater_on:
                        await self._heater_service("turn_off")
                        heater_on = False
                        logger.info(
                            "autonomous ha-heater: heat no longer demanded — heater off"
                        )
                    else:
                        logger.debug(
                            "autonomous: no heat demand — idle (poll again in %.0fs)",
                            self._poll_sec,
                        )
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=self._poll_sec
                        )
                        break  # stop_event fired
                    except asyncio.TimeoutError:
                        continue

                # Heat is demanded — run one mining chunk.
                logger.info(
                    "autonomous: heat demanded (%.2f kW) — "
                    "starting %.0fs mining chunk",
                    reading.measured_kw,
                    self._chunk_sec,
                )
                args = {"duration_sec": self._chunk_sec}
                gpu_fut = loop.run_in_executor(None, _run_gpu, args)
                cpu_fut = loop.run_in_executor(None, _run_cpu, args)
                results = await asyncio.gather(gpu_fut, cpu_fut, return_exceptions=True)

                gpu_res, cpu_res = results
                gpu_skipped = isinstance(gpu_res, Exception) or (
                    isinstance(gpu_res, dict) and gpu_res.get("skipped")
                )
                cpu_skipped = isinstance(cpu_res, Exception) or (
                    isinstance(cpu_res, dict) and cpu_res.get("skipped")
                )

                if gpu_skipped and cpu_skipped:
                    # Neither mining handler is configured.  Try the HA space
                    # heater first; fall back to synthetic fill if not set up.
                    if self._ha_configured:
                        if not heater_on:
                            await self._heater_service("turn_on")
                            heater_on = True
                        logger.info(
                            "autonomous ha-heater: mining not configured — "
                            "holding heater on for %.0fs then re-checking demand",
                            self._chunk_sec,
                        )
                        try:
                            await asyncio.wait_for(
                                self._stop_event.wait(), timeout=self._chunk_sec
                            )
                            break  # stop_event fired while waiting
                        except asyncio.TimeoutError:
                            pass  # chunk elapsed; loop back to re-check demand
                    else:
                        logger.info(
                            "autonomous: mining not configured — "
                            "falling back to synthetic heat fill"
                        )
                        await loop.run_in_executor(None, _run_synthetic, args)

                # Loop immediately — re-check demand before starting next chunk.

        finally:
            # Always turn the heater off on exit, regardless of why the loop ended.
            if heater_on:
                logger.info("autonomous ha-heater: exiting — turning heater off")
                await self._heater_service("turn_off")

        logger.info("autonomous: mode exited cleanly")


# ---------------------------------------------------------------------------
# Handler shims (run in executor threads — blocking is fine here)
# ---------------------------------------------------------------------------

def _run_gpu(args: dict) -> dict:
    """Call the gpu_mining_fill handler in a thread."""
    try:
        from hadcd_workloads.gpu_mining_fill import run_gpu_mining_fill  # type: ignore[import]
        result = run_gpu_mining_fill(args)
        if result.get("skipped"):
            logger.debug("autonomous gpu: skipped (%s)", result.get("reason"))
        else:
            logger.info(
                "autonomous gpu: chunk done (active=%ss)",
                result.get("active_mining_sec", "?"),
            )
        return result
    except Exception as exc:
        logger.warning("autonomous gpu: handler error: %s", exc)
        return {"error": str(exc)}


def _run_cpu(args: dict) -> dict:
    """Call the p2pool_fill handler in a thread."""
    try:
        from hadcd_workloads.p2pool_fill import run_p2pool_fill  # type: ignore[import]
        result = run_p2pool_fill(args)
        if result.get("skipped"):
            logger.debug("autonomous cpu: skipped (%s)", result.get("reason"))
        else:
            logger.info(
                "autonomous cpu: chunk done (active=%ss)",
                result.get("active_mining_sec", "?"),
            )
        return result
    except Exception as exc:
        logger.warning("autonomous cpu: handler error: %s", exc)
        return {"error": str(exc)}


def _run_synthetic(args: dict) -> dict:
    """Call the synthetic_heat_fill handler in a thread.

    Last-resort fallback when neither mining nor an HA heater is configured.
    Pure CPU/GPU burn — no wallet, no external binary, always available.
    """
    try:
        from hadcd_workloads.synthetic_heat_fill import run_synthetic_heat_fill  # type: ignore[import]
        result = run_synthetic_heat_fill(args)
        logger.info(
            "autonomous synthetic: chunk done (%.0fs)",
            result.get("duration_requested_sec", args.get("duration_sec", "?")),
        )
        return result
    except Exception as exc:
        logger.warning("autonomous synthetic: handler error: %s", exc)
        return {"error": str(exc)}
