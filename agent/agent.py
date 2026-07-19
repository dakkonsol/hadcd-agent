"""The Agent class — protocol + concurrent loops.

Mirrors `simulator/node.py` in shape, with two production differences:
  * Persistent identity (enroll once, then resume from a state file).
  * Real execution via a subprocess pool against `hadcd_workloads`,
    rather than a sleep-and-pretend.

Four concurrent loops, plus one short-lived asyncio task per pulled
work assignment:

  heartbeat — POST /api/nodes/{id}/heartbeat every 10s
  demand    — POST /api/nodes/{id}/heat_demand every 15s from BMS file
  work      — GET  /api/work?node_id=... every 6s, dispatch each
              assignment to the executor; POST /api/work/{id}/result
              with the real outcome
  (no separate thermo loop — the BMS supplies real temperatures.)

A graceful stop signal stops new work pulls, lets in-flight tasks
finish (or cancels them after a grace window), and exits cleanly.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path

import httpx

from agent.autonomous_mode import AutonomousMiner
from agent.blob_client import BlobClient, BlobClientError, safe_blob_name
from agent.config import AgentSettings
from agent.executor import AgentExecutor
from agent.heat_source import HeatSource, build_source
from agent.image_cache import build_image_cache
from agent.kasa_power_meter import KasaPowerMeter, build_kasa_meter
from agent.rental_session_handler import RentalSessionHandler
from agent.session_source import SessionSource, build_session_source
from agent.state import AgentState
from agent.tailscale import check_tailscale_status, log_tailscale_advisory
from agent.vast_provider import VastProvider, VastProviderState, build_vast_provider
from agent.watchdog import notify_ready, notify_watchdog

logger = logging.getLogger("hadcd.agent")

# Real-seconds grace given to in-flight task runs after stop is signalled.
_DRAIN_GRACE_SEC = 30.0


def _get_local_ip() -> str | None:
    """Return the node's LAN IP address, or None on failure.

    Uses a UDP connect to a public address (no packets are actually sent)
    to ask the OS which local interface it would use, which gives us the
    LAN IP without needing to parse ``ip addr`` or ``ifconfig`` output.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return None


def _get_public_ip() -> str | None:
    """Return the node's public WAN IP via api.ipify.org, or None on failure."""
    import urllib.request
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as resp:
            return resp.read().decode().strip()
    except Exception:
        return None


def _get_storage_stats(path: str) -> tuple[float, float] | None:
    """Return (used_gb, free_gb) for the filesystem containing *path*.

    Returns None if the path is empty, does not exist, or stat fails.
    """
    import shutil
    if not path:
        return None
    try:
        usage = shutil.disk_usage(path)
        used_gb = (usage.total - usage.free) / 1024 ** 3
        free_gb = usage.free / 1024 ** 3
        return round(used_gb, 2), round(free_gb, 2)
    except Exception:
        return None


def _storage_kw(path: str) -> dict:
    """Return heartbeat kwargs for storage stats, or empty dict if no path."""
    stats = _get_storage_stats(path)
    if stats is None:
        return {}
    used_gb, free_gb = stats
    return {"storage_used_gb": used_gb, "storage_free_gb": free_gb}


class Agent:
    def __init__(
        self,
        settings: AgentSettings,
        state: AgentState,
    ) -> None:
        self.settings = settings
        self.state = state
        self._run_tasks: set[asyncio.Task] = set()

    # --- enrollment / identity ----------------------------------------

    async def ensure_enrolled(self, client: httpx.AsyncClient) -> None:
        """Enroll if no persistent identity is present; otherwise resume."""
        if self.state.enrolled:
            logger.info(
                "resuming as node %s (from %s)",
                self.state.node_id,
                self.state.path,
            )
            return
        if not self.settings.enrollment_token:
            raise RuntimeError(
                "no persistent identity and no enrollment token to enroll with — "
                "set ENROLLMENT_TOKENS"
            )
        s = self.settings
        resp = await client.post(
            "/api/nodes/register",
            json={
                "name": s.node_name,
                "node_type": s.node_type,
                "max_power_kw": s.max_power_kw,
                "cpu_capacity": s.cpu_capacity,
                "gpu_capacity": s.gpu_capacity,
                # Phase 18l — VRAM declared at enrollment; also refreshed
                # on every heartbeat so it stays current without re-enrolling.
                "gpu_vram_gb": s.gpu_vram_gb,
                "ram_gb": s.ram_gb,
                "bandwidth_mbps": s.bandwidth_mbps,
                # Phase 26 — declared performance for best-fit dispatch.
                "perf_score": s.perf_score,
                # Phase 8e — zone pairing
                "zone_name": s.zone_name or None,
                "require_own_demand": s.require_own_demand,
                # Phase 11a — geographic location for weather-driven Vast.AI windows.
                "latitude": s.node_latitude,
                "longitude": s.node_longitude,
                "location_label": s.node_location_label,
            },
            headers={"Authorization": f"Bearer {s.enrollment_token}"},
        )
        resp.raise_for_status()
        body = resp.json()
        self.state.node_id = body["id"]
        self.state.node_token = body["token"]
        self.state.save()
        logger.info(
            "enrolled as node %s (state persisted to %s)",
            self.state.node_id,
            self.state.path,
        )

    # --- HTTP helpers -------------------------------------------------

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.state.node_token}"}

    # --- main runner --------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Run the agent until `stop`. Drains in-flight tasks on exit."""
        s = self.settings
        # Container mount/hardening policy is enforced inside the workload
        # package (hadcd_workloads.container), which runs in spawned worker
        # processes. Export it to the process env so workers inherit it even
        # when the settings came from a .env file rather than the real env.
        if s.container_mount_allowlist:
            os.environ["HADCD_MOUNT_ALLOWLIST"] = s.container_mount_allowlist
        if s.container_require_hardened:
            os.environ["HADCD_REQUIRE_HARDENED"] = "1"
        executor = AgentExecutor(s.agent_concurrency)
        heat_source = build_source(s)
        session_source = build_session_source(s)
        self._image_cache = build_image_cache(s)
        vast_provider = build_vast_provider(s)
        self._kasa_meter: KasaPowerMeter = build_kasa_meter(s)
        if self._kasa_meter.enabled:
            logger.info(
                "kasa: power meter enabled (ip=%s) — "
                "measured watts will replace estimated power in task results",
                s.kasa_plug_ip,
            )
        else:
            logger.debug("kasa: power meter disabled (KASA_PLUG_IP not set)")
        # Phase 15a / 18k: autonomous offline fallback — mines from local
        # thermostat when the dispatcher is unreachable.  If mining is not
        # configured, falls back to the HA space heater (if set) before
        # resorting to synthetic CPU/GPU burn.
        autonomous_miner = AutonomousMiner(
            chunk_sec=s.autonomous_chunk_sec,
            ha_url=s.ha_url,
            ha_token=s.ha_token,
            ha_heater_entity_id=s.ha_heater_entity_id,
        )
        self._log_session_advisory(s)
        ts_status = check_tailscale_status()
        log_tailscale_advisory(ts_status)
        # Phase 14a: detect network addresses once at startup so the
        # heartbeat loop can report them to the dashboard.
        self._local_ip: str | None = _get_local_ip()
        self._tailscale_ip: str | None = ts_status.tailscale_ip if ts_status.connected else None
        self._public_ip: str | None = _get_public_ip()
        # Phase 20a: storage stats refreshed each heartbeat.
        self._storage_path: str = s.storage_path or ""
        # Phase 21a: dependency health report — run once at startup, refresh
        # every hour so the dashboard reflects changes without restarting.
        from agent.dep_check import check_all as _dep_check_all
        self._dep_status: list[dict] = _dep_check_all(
            trex_path=s.nicehash_trex_path or None,
            xmrig_path=s.xmrig_path or None,
            vastai_cmd=s.vastai_cmd or None,
            check_vastai=bool(s.vastai_api_key),
            check_mining=bool(s.nicehash_wallet or s.xmr_wallet_address),
        )
        self._dep_check_counter: int = 0  # refreshed every _DEP_CHECK_INTERVAL heartbeats
        # Phase 17d: tracks whether we have just applied a pending config
        # and need to ack it on the next heartbeat.
        self._pending_config_applied: bool = False
        # Media model-sync: acked once the queued models are downloaded.
        self._pending_media_models_applied: bool = False
        # Phase 18j: latest heat-demand state from the demand loop.
        # Written by _demand_loop; read by _heartbeat_loop so we avoid
        # double-calling the heat source (which may make API requests).
        # always_on nodes never set this True.
        self._needs_heat: bool = False
        # Phase 22: building-hub vacancy override, read from each
        # heartbeat response. While True the demand loop reports zero
        # kW (room temp still reported for the Building view) and the
        # autonomous fallback never heats the empty room.
        self._heat_override_active: bool = False
        try:
            async with httpx.AsyncClient(
                base_url=s.hadcd_api, timeout=30.0
            ) as client:
                await self._wait_for_backend(client)
                await self.ensure_enrolled(client)
                # Phase 18c: rental session container manager. Constructed
                # after enrollment: the node id and bearer token live in
                # AgentState (persisted identity), not in settings, and are
                # only guaranteed present once ensure_enrolled returns.
                self._rental_sessions = RentalSessionHandler(
                    node_id=str(self.state.node_id) if self.state.node_id else "",
                    dispatcher_url=s.hadcd_api,
                    node_token=str(self.state.node_token or ""),
                    # Session ports are tailnet-only: published on the
                    # Tailscale IP, or loopback (unreachable) without it.
                    publish_host=self._tailscale_ip or "127.0.0.1",
                    # Phase 26 — record served models so the heartbeat's
                    # cached_models list tracks the shared Ollama volume.
                    on_model_cached=self.state.record_cached_model,
                    # Media (ComfyUI) opt-in: host models dir + image. Empty = disabled.
                    media_models_path=s.comfyui_models_path,
                    comfyui_image=s.comfyui_image,
                )
                # Phase 13b: signal READY=1 to systemd so it knows the
                # agent is fully live (enrolled + loops about to start).
                # No-op outside systemd (NOTIFY_SOCKET not set).
                notify_ready()
                # Pre-pull configured images in a thread so the event loop
                # stays free. This runs concurrently with the main loops —
                # a slow pull does not delay heartbeats or work polling.
                loop = asyncio.get_running_loop()
                loop.run_in_executor(None, self._image_cache.prepull_all)
                # Phase 20c: start the P2P storage server in a daemon thread
                # so other Tailscale-connected nodes can fetch files directly.
                # Bound to the Tailscale IP: peers reach it over the tailnet
                # only, so the content-hash-as-capability model holds even on
                # nodes with unfirewalled LAN/WAN interfaces. Without
                # Tailscale it binds loopback (unreachable to peers) rather
                # than exposing pool files to the local network.
                if self._storage_path:
                    from agent.storage_server import StorageServer
                    _pool_dir = Path(self._storage_path) / "pool"
                    _pool_dir.mkdir(parents=True, exist_ok=True)
                    _bind_host = self._tailscale_ip or "127.0.0.1"
                    if not self._tailscale_ip:
                        logger.warning(
                            "storage: Tailscale not connected — P2P server "
                            "bound to loopback; peers cannot fetch from this "
                            "node until Tailscale is up and the agent restarts"
                        )
                    _storage_srv = StorageServer(
                        _pool_dir, s.storage_server_port, host=_bind_host
                    )
                    _storage_srv.start()
                loops = [
                    # Phase 12a: pass vast_provider so heartbeat can
                    # report the live Vast.AI state in every ping.
                    # Phase 15a: also pass heat_source + autonomous_miner
                    # so the heartbeat loop can trigger fallback mining.
                    self._heartbeat_loop(
                        client, session_source, stop, vast_provider,
                        heat_source, autonomous_miner,
                    ),
                    self._demand_loop(client, heat_source, stop),
                    self._work_loop(client, executor, stop),
                ]
                # Phase 18j: always_on nodes have no thermostat and no
                # Vast.AI listing.  Standard nodes run the vast loop as before.
                if s.node_role != "always_on":
                    loops.append(
                        # Phase 15e: pass heat_source so the vast loop can
                        # read the thermostat setpoint for the dynamic hot
                        # threshold.
                        self._vast_loop(client, vast_provider, stop, heat_source)
                    )
                # Phase 16b: Kasa power meter loop — only started when a
                # plug IP is configured. Disabled nodes use estimated power.
                if self._kasa_meter.enabled:
                    loops.append(self._kasa_loop(self._kasa_meter, stop))
                # Phase 20b/20c: storage transfer loop — only started when this
                # node has opted in to storage contribution.
                # Phase GC: garbage-collect stale pool files every 6 hours.
                if self._storage_path:
                    loops.append(self._transfer_loop(client, stop))
                    loops.append(self._storage_gc_loop(client, stop))
                await asyncio.gather(*loops)
                await self._drain_in_flight()
        finally:
            # Stop any running autonomous mining chunk before tearing down
            # the heat source so the miner doesn't outlive its BMS reader.
            await autonomous_miner.stop()
            await heat_source.aclose()
            await session_source.aclose()
            await self._kasa_meter.aclose()
            executor.shutdown()

    @staticmethod
    def _log_session_advisory(s: AgentSettings) -> None:
        """Loud advisory when no interactive-session detector is set.

        Without one, fill-tier work (mining, synthetic heat-fill) has
        no way to know the user is gaming / using AI apps, so it would
        compete with them for the GPU. We don't refuse to start — heat
        delivery shouldn't hard-depend on Sunshine — but we make the
        gap impossible to miss in the logs.
        """
        if (s.session_source or "none").lower() in ("none", "", "null"):
            logger.warning(
                "\n"
                "  ============================================================\n"
                "   No interactive-session detector configured\n"
                "   (SESSION_SOURCE=none).\n"
                "\n"
                "   Fill-tier work (mining, synthetic heat-fill) cannot tell\n"
                "   when you are using this machine, so it may compete with\n"
                "   your games / AI apps for the GPU.\n"
                "\n"
                "   Strongly recommended: install Sunshine and set\n"
                "   SESSION_SOURCE=sunshine. See docs/sunshine-setup.md.\n"
                "  ============================================================"
            )

    async def _wait_for_backend(
        self, client: httpx.AsyncClient, attempts: int = 30
    ) -> None:
        for _ in range(attempts):
            try:
                resp = await client.get("/api/health")
                if (
                    resp.status_code == 200
                    and resp.json().get("database") == "ok"
                ):
                    return
            except Exception:
                pass
            await asyncio.sleep(1.0)
        raise RuntimeError(f"backend not reachable at {self.settings.hadcd_api}")

    # --- loops --------------------------------------------------------

    async def _heartbeat_loop(
        self,
        client: httpx.AsyncClient,
        session_source: SessionSource,
        stop: asyncio.Event,
        vast_provider: "VastProvider",
        heat_source: "HeatSource",
        autonomous_miner: "AutonomousMiner",
    ) -> None:
        s = self.settings
        interval = s.heartbeat_interval_sec
        # Phase 15a: track when the dispatcher first went offline so we
        # know when to cross the autonomous-fallback threshold.
        _offline_since: float | None = None
        while not stop.is_set():
            # Poll the session detector first so the heartbeat carries
            # an up-to-date session_active. is_active() is fail-quiet —
            # it never raises — so this can't break the heartbeat.
            session_active = await session_source.is_active()
            # Phase 12a: include the Vast.AI provider state so the
            # operator dashboard can show a live listing badge.
            vast_state = vast_provider.state.value if vast_provider.enabled else None
            # Phase 18j: report current heat-demand state so the session
            # assigner can prefer thermally-active standard nodes.
            # always_on nodes always report False (no thermostat).
            needs_heat = self._needs_heat
            _hb_ok = False
            try:
                resp = await client.post(
                    f"/api/nodes/{self.state.node_id}/heartbeat",
                    json={
                        "session_active": session_active,
                        "vast_state": vast_state,
                        "needs_heat": needs_heat,
                        # Phase 18l: refresh VRAM on every heartbeat so the
                        # session assigner and planner stay current without
                        # requiring re-enrollment after a hardware change.
                        "gpu_vram_gb": s.gpu_vram_gb,
                        # Phase 26: performance figure + warm-model list so
                        # the session assigner can best-fit and route to a
                        # node that already holds the requested model.
                        "perf_score": s.perf_score,
                        "cached_models": self.state.cached_models,
                        # Media opt-in: media_capable is true only when the
                        # operator configured COMFYUI_MODELS_PATH. Gates the
                        # dispatcher's operator-only media routing.
                        "media_capable": bool(s.comfyui_models_path),
                        # Phase 14a: report IPs so the dashboard can show them.
                        "local_ip": self._local_ip,
                        "tailscale_ip": self._tailscale_ip,
                        "public_ip": self._public_ip,
                        # Phase 20a: asset storage contribution.
                        "storage_path": self._storage_path or None,
                        **(_storage_kw(self._storage_path)),
                        # Phase 17c: report discovered Vast.AI machine ID.
                        "vastai_machine_id": s.vastai_machine_id or None,
                        # Phase 17d: ack when we have just applied a pending config.
                        "pending_config_applied": self._pending_config_applied,
                        "pending_media_models_applied": self._pending_media_models_applied,
                        # Phase 21a: dependency health — refresh every hour.
                        "dep_status": self._dep_status,
                    },
                    headers=self._auth(),
                )
                if resp.status_code == 200:
                    _hb_ok = True
                    try:
                        data = resp.json()
                        # Phase 12b: read the heartbeat response to pick up any
                        # operator override set from the dashboard.  The response
                        # is a NodeRead which now includes `vast_override`.
                        if vast_provider.enabled:
                            vast_provider.set_override(data.get("vast_override"))
                        # Phase 22: building-hub vacancy override. While
                        # vacant, the backend also substitutes
                        # vast_override="unlist" above, so any active
                        # Vast.AI rental drains gracefully as the
                        # node's last job — never terminated.
                        await self._apply_heat_override(
                            data.get("heat_override"),
                            data.get("setback_temp_c"),
                            heat_source,
                        )
                        # Phase 17d: check for a pending config blob queued by
                        # the operator from the dashboard.
                        pending = data.get("pending_config")
                        if pending and isinstance(pending, dict):
                            self._apply_pending_config(pending)
                            self._pending_config_applied = True
                        elif not pending and self._pending_config_applied:
                            # Backend confirmed it received our ack — restart
                            # to pick up the new env vars.
                            self._pending_config_applied = False
                            logger.info(
                                "heartbeat: pending config ack confirmed — "
                                "restarting agent to apply new env"
                            )
                            asyncio.get_event_loop().call_later(2, self._do_restart)
                        # Media model-sync: download any queued ComfyUI model
                        # files into the media models dir, then ack so the
                        # backend clears the queue.
                        media_models = data.get("pending_media_models")
                        if media_models and isinstance(media_models, list):
                            asyncio.ensure_future(
                                self._sync_media_models(media_models)
                            )
                            self._pending_media_models_applied = True
                        elif not media_models and self._pending_media_models_applied:
                            self._pending_media_models_applied = False
                        # Phase 18c: handle rental session assignments.
                        sessions = data.get("sessions", [])
                        if sessions:
                            asyncio.ensure_future(
                                self._rental_sessions.handle_sessions(sessions)
                            )
                    except Exception:
                        logger.debug("heartbeat: could not parse response body")
            except Exception:
                logger.exception("heartbeat failed")

            # Phase 21a: refresh dep_status hourly (every _DEP_CHECK_INTERVAL beats).
            self._dep_check_counter += 1
            if self._dep_check_counter >= self._DEP_CHECK_INTERVAL:
                self._dep_check_counter = 0
                from agent.dep_check import check_all as _dep_check_all
                self._dep_status = _dep_check_all(
                    trex_path=s.nicehash_trex_path or None,
                    xmrig_path=s.xmrig_path or None,
                    vastai_cmd=s.vastai_cmd or None,
                    check_vastai=bool(s.vastai_api_key),
                    check_mining=bool(s.nicehash_wallet or s.xmr_wallet_address),
                )

            # --- Phase 15a/15c: autonomous offline fallback -----------
            #
            # Full 4-condition matrix:
            #
            #   Online  + Cold  → dispatcher assigns tasks; VastProvider unlisted.
            #   Online  + Warm  → dispatcher lists on Vast.AI; no mining.
            #   Offline + Cold  → Phase 15b unlists Vast.AI; mine autonomously.
            #   Offline + Warm  → Phase 15b lists on Vast.AI; do NOT mine.
            #
            # "Cold" = outdoor temp < VAST_COLD_THRESHOLD_C for ≥ VAST_MIN_COLD_HOURS.
            # "Warm" = everything else (strictly the complement; one threshold, binary).
            #
            # Phase 15b (_vast_loop) drives the VastProvider state, which already
            # encodes the warm/cold decision.  We read it here so the two loops
            # stay decoupled:
            #   LISTED / LISTING   → warm weather  → GPU belongs to Vast.AI renter
            #   UNLISTED / UNLISTING → cold weather → GPU is free for local mining
            #
            # Note: when VASTAI_API_KEY is not configured, vast_provider is disabled
            # and its state is always UNLISTED, so mining is never suppressed.
            _vast_renting = vast_provider.enabled and vast_provider.state in (
                VastProviderState.LISTED,
                VastProviderState.LISTING,
            )

            if _hb_ok:
                # Dispatcher reachable — stop autonomous mode if it was running.
                if autonomous_miner.active:
                    await autonomous_miner.stop()
                _offline_since = None
            elif _vast_renting:
                # Offline + Warm: Vast.AI has (or is seeking) a renter — GPU is
                # not ours to mine with.  Stop any miner that may still be running
                # from an earlier cold period.
                if autonomous_miner.active:
                    logger.info(
                        "autonomous: Vast.AI is %s — pausing mining while warm",
                        vast_provider.state.value,
                    )
                    await autonomous_miner.stop()
            else:
                # Offline + Cold (or no Vast.AI): mine from local thermostat.
                # Phase 22: never while the room is marked vacant — the
                # last-known override holds until the dispatcher is
                # reachable again and says otherwise.
                if _offline_since is None:
                    _offline_since = time.monotonic()
                offline_sec = time.monotonic() - _offline_since
                if (
                    offline_sec >= s.autonomous_fallback_after_sec
                    and not autonomous_miner.active
                    and not self._heat_override_active
                ):
                    await autonomous_miner.start(heat_source)

            await _sleep_or_stop(stop, interval)
            # Phase 13b: ping the systemd watchdog after each sleep so it
            # knows the event loop is alive.  Called unconditionally —
            # even a failed heartbeat means the loop is still running.
            # No-op when NOTIFY_SOCKET is not set (dev / Docker mode).
            notify_watchdog()

    async def _apply_heat_override(
        self,
        override: str | None,
        setback_temp_c: float | None,
        heat_source: HeatSource,
    ) -> None:
        """Phase 22 — react to the building manager's vacancy switch.

        Room marked vacant ("setback"): remember the current thermostat
        setpoint (persisted, so a restart can still restore it), then
        drop the thermostat to the setback temperature. Room re-opened
        (override cleared): restore the remembered setpoint so it
        pre-warms for the next guest.

        Sources that can't write setpoints (file/http) still get the
        demand-zeroing in _demand_loop — HADCD stops heating either
        way; only the conventional-heat cut needs a writable source.
        """
        active = override == "setback"
        if active == self._heat_override_active:
            return
        self._heat_override_active = active

        if active:
            target = (
                setback_temp_c
                if setback_temp_c is not None
                else self.settings.setback_temp_c
            )
            try:
                # Don't clobber an already-saved setpoint: after a
                # restart mid-vacancy the thermostat is reading the
                # SETBACK temperature, not the one we need to restore.
                if self.state.saved_setpoint_c is None:
                    reading = await heat_source.read()
                    if reading.setpoint_c is not None:
                        self.state.saved_setpoint_c = reading.setpoint_c
                        self.state.save()
            except Exception:
                logger.exception(
                    "heat override: could not capture current setpoint"
                )
            if await heat_source.set_setpoint(target):
                logger.info(
                    "heat override: room vacant — thermostat dropped to %.1f °C",
                    target,
                )
            else:
                logger.info(
                    "heat override: room vacant — demand reporting paused "
                    "(heat source does not support setpoint writes)"
                )
        else:
            saved = self.state.saved_setpoint_c
            if saved is not None:
                if await heat_source.set_setpoint(saved):
                    logger.info(
                        "heat override cleared — thermostat restored to %.1f °C",
                        saved,
                    )
                self.state.saved_setpoint_c = None
                try:
                    self.state.save()
                except Exception:
                    logger.exception("heat override: could not persist state")
            else:
                logger.info("heat override cleared — resuming normal dispatch")

    async def _demand_loop(
        self,
        client: httpx.AsyncClient,
        heat_source: HeatSource,
        stop: asyncio.Event,
    ) -> None:
        interval = self.settings.demand_interval_sec
        while not stop.is_set():
            reading = await heat_source.read()
            # Phase 22: while the room is vacant, report zero demand so
            # no layer of the dispatcher sees this node as heatable.
            # Room temp / setpoint still flow through so the Building
            # view shows live readings for the vacant room.
            if self._heat_override_active:
                reading = dataclasses.replace(
                    reading, measured_kw=0.0, expected_window_sec=None
                )
            # Phase 18j: cache for the heartbeat loop so we don't double-call
            # the heat source (Ecobee makes API requests on each read).
            if self.settings.node_role != "always_on":
                self._needs_heat = reading.measured_kw > 0
            try:
                await client.post(
                    f"/api/nodes/{self.state.node_id}/heat_demand",
                    json=reading.to_api_payload(),
                    headers=self._auth(),
                )
            except Exception:
                logger.exception("heat-demand post failed")
            await _sleep_or_stop(stop, interval)

    async def _work_loop(
        self,
        client: httpx.AsyncClient,
        executor: AgentExecutor,
        stop: asyncio.Event,
    ) -> None:
        interval = self.settings.work_poll_interval_sec
        while not stop.is_set():
            try:
                resp = await client.get(
                    "/api/work",
                    params={"node_id": self.state.node_id},
                    headers=self._auth(),
                )
                resp.raise_for_status()
                for assignment in resp.json():
                    task = asyncio.create_task(
                        self._run_assignment(client, executor, assignment)
                    )
                    self._run_tasks.add(task)
                    task.add_done_callback(self._run_tasks.discard)
            except Exception:
                logger.exception("work poll failed")
            await _sleep_or_stop(stop, interval)

    async def _post_vast_rental(
        self,
        client: httpx.AsyncClient,
        machine_id: str,
        listed_at,
        unlisted_at,
        listing_price_dph: float | None = None,
    ) -> None:
        """Phase 12c / 21b — POST a completed Vast.AI rental session to the backend."""
        body: dict = {
            "machine_id": machine_id,
            "listed_at": listed_at.isoformat(),
            "unlisted_at": unlisted_at.isoformat(),
        }
        if listing_price_dph is not None:
            body["listing_price_dph"] = listing_price_dph
        try:
            resp = await client.post(
                f"/api/nodes/{self.state.node_id}/vast_rentals",
                json=body,
                headers=self._auth(),
            )
            if resp.status_code == 201:
                logger.info(
                    "vast: rental session recorded (machine=%s, %.2f h)",
                    machine_id,
                    (unlisted_at - listed_at).total_seconds() / 3600,
                )
            else:
                logger.warning(
                    "vast: rental POST returned HTTP %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception:
            logger.exception("vast: rental POST failed")

    async def _vast_loop(
        self,
        client: httpx.AsyncClient,
        provider: VastProvider,
        stop: asyncio.Event,
        heat_source: "HeatSource",
    ) -> None:
        """Phase 11c — poll the Vast.AI schedule and drive listing state.

        When the provider is disabled (no API key / machine ID), this loop
        exits immediately so it does not consume resources.

        On each tick:
          1. Fetch GET /api/nodes/{id}/vast_schedule — the backend's weather-
             derived listing decision.
          2. Call provider.update(schedule) in a thread (VastAiCli uses
             blocking subprocess calls).

        Phase 12c: attach a rental callback before starting so the provider
        can POST completed sessions to the backend when they close.

        Phase 15b: when the backend is unreachable, fall back to a locally
        computed schedule derived from Open-Meteo weather data.  This keeps
        the Vast.AI listing decision fully autonomous — nodes list and unlist
        based on weather regardless of dispatcher availability.
        """
        if not provider.enabled:
            logger.debug("vast: provider disabled — skipping vast_loop")
            return

        # Inject the rental callback now that we have a live client.
        async def _rental_cb(machine_id, listed_at, unlisted_at, listing_price_dph=None):
            await self._post_vast_rental(client, machine_id, listed_at, unlisted_at, listing_price_dph)

        provider._rental_callback = _rental_cb

        s = self.settings
        interval = s.vast_check_interval_sec
        # Phase 15b: cache the last locally-computed schedule so we don't
        # hit Open-Meteo on every tick (only once per vast_weather_refresh_sec).
        _local_schedule: dict | None = None
        _local_schedule_age: float = float("inf")  # seconds since last fetch

        while not stop.is_set():
            schedule: dict | None = None
            try:
                resp = await client.get(
                    f"/api/nodes/{self.state.node_id}/vast_schedule",
                    headers=self._auth(),
                )
                resp.raise_for_status()
                schedule = resp.json()
            except Exception:
                logger.debug(
                    "vast: backend schedule unavailable — "
                    "falling back to local weather"
                )

            if schedule is None:
                # --- Phase 15b: autonomous fallback via Open-Meteo -------
                _local_schedule_age += interval
                if (
                    _local_schedule is None
                    or _local_schedule_age >= s.vast_weather_refresh_sec
                ):
                    _local_schedule = await self._local_vast_schedule(
                        s, heat_source
                    )
                    _local_schedule_age = 0.0
                schedule = _local_schedule

            if schedule is not None:
                # Drive state machine in a thread (subprocess calls are blocking).
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, provider.update, schedule)
            await _sleep_or_stop(stop, interval)

    async def _local_vast_schedule(
        self, s: "AgentSettings", heat_source: "HeatSource"
    ) -> "dict | None":
        """Phase 15b/15e — build a Vast.AI schedule from a direct Open-Meteo fetch.

        Returns None when lat/lon are not configured or the weather fetch fails.
        The returned dict has the same shape as ``/api/nodes/{id}/vast_schedule``
        so ``VastProvider.update()`` can consume it unchanged.

        Phase 15e: hot threshold is computed dynamically as thermostat setpoint
        minus 2 °C when the BMS reports a setpoint.  This means the node stops
        listing on Vast.AI once outdoor temperature approaches the indoor target —
        the building no longer benefits from additional GPU heat at that point.
        Falls back to ``VAST_HOT_THRESHOLD_C`` (static config) when no setpoint
        is available (e.g. FileHeatSource without a ``setpoint_c`` field, or the
        agent just started and hasn't had a BMS tick yet).
        """
        if not s.node_latitude or not s.node_longitude:
            logger.debug(
                "vast: no lat/lon configured — cannot compute local weather schedule"
            )
            return None
        try:
            from agent.weather import build_vast_schedule, fetch_forecast
            forecast = await fetch_forecast(s.node_latitude, s.node_longitude)

            # --- Phase 15e: dynamic hot threshold from thermostat setpoint ---
            # Effective threshold = max(floor, setpoint).  The floor guards
            # against a buggy thermostat reporting a very low setpoint that
            # would otherwise keep the node listed into warm weather.
            floor: float = s.vast_hot_threshold_c if s.vast_hot_threshold_c is not None else 16.0
            hot_threshold_c: float = floor
            try:
                reading = await heat_source.read()
                if reading.setpoint_c is not None:
                    hot_threshold_c = max(floor, reading.setpoint_c)
                    logger.debug(
                        "vast: dynamic hot threshold %.1f°C "
                        "(max(floor=%.1f°C, setpoint=%.1f°C))",
                        hot_threshold_c,
                        floor,
                        reading.setpoint_c,
                    )
                else:
                    logger.debug(
                        "vast: no thermostat setpoint — using floor %.1f°C as hot threshold",
                        floor,
                    )
            except Exception as exc:
                logger.debug(
                    "vast: could not read thermostat setpoint for hot threshold "
                    "(using floor %.1f°C): %s",
                    floor,
                    exc,
                )

            sched = build_vast_schedule(
                forecast,
                threshold_c=s.vast_cold_threshold_c,
                hot_threshold_c=hot_threshold_c,
                min_hours=s.vast_min_cold_hours,
            )
            logger.info(
                "vast: local weather schedule — should_list=%s, "
                "hot_threshold=%.1f°C, next_window=%s",
                sched["should_list"],
                hot_threshold_c if hot_threshold_c is not None else float("nan"),
                sched.get("next_window_start") or "none",
            )
            return sched
        except Exception as exc:
            logger.warning("vast: local weather fetch failed: %s", exc)
            return None

    # --- Kasa power meter loop ---------------------------------------

    async def _kasa_loop(
        self,
        meter: KasaPowerMeter,
        stop: asyncio.Event,
    ) -> None:
        """Phase 16b — poll the Kasa plug every kasa_poll_interval_sec.

        Runs only when KASA_PLUG_IP is set.  poll() is fail-quiet; any
        error is logged once by the meter and the loop continues.
        A per-poll asyncio timeout prevents a hung TCP connection from
        stalling the loop indefinitely.
        """
        interval = self.settings.kasa_poll_interval_sec
        timeout = max(interval - 2.0, 3.0)  # always leave slack for sleep
        while not stop.is_set():
            try:
                await asyncio.wait_for(meter.poll(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "kasa: poll timed out after %.0fs — "
                    "device may be unreachable",
                    timeout,
                )
            await _sleep_or_stop(stop, interval)

    # --- per-assignment runner ---------------------------------------

    async def _run_assignment(
        self,
        client: httpx.AsyncClient,
        executor: AgentExecutor,
        assignment: dict,
    ) -> None:
        """Execute one offloaded task and POST the result.

        Phase 10c adds blob pre/post hooks for container tasks:
          * Input blobs declared in ``args.input_blobs`` are downloaded
            to a temp directory before the container starts.
          * ``args.volumes`` is extended with the bind-mount specs so
            the container can read those files at the declared paths.
          * If ``args.output_paths`` is set, a writable output dir is
            created and its path is passed as ``args.output_dir``; after
            the container exits, any files it wrote there are uploaded
            as blobs and their IDs are included in the task result.
          * The temp directory is always removed in a finally block.

        Power is reported as the task's *estimated* power: the agent
        has no wattmeter integration yet, and the central server records
        this as the heat actually delivered.
        """
        task_id = assignment["task_id"]
        task_type = assignment["payload"].get("task_type")
        args = dict(assignment["payload"].get("args") or {})
        nominal_power_w = assignment["estimated_power_w"]
        # Phase 16b: record start time for Kasa per-task power averaging.
        task_start_monotonic = time.monotonic()

        logger.info("running task %s (type=%s)", task_id, task_type)

        # --- Phase 10c: blob pre-processing (container tasks only) ---
        tmp_dir: Path | None = None
        output_dir: Path | None = None
        blob_client = BlobClient(
            client=client,
            node_token=str(self.state.node_token),
            blob_storage_dir=self.settings.blob_storage_dir,
        )

        if task_type == "container":
            input_blobs = args.pop("input_blobs", None) or []
            wants_output = bool(args.get("output_paths"))

            # Normalise every blob filename to a safe basename up front so the
            # download destination (blob_client) and the bind-mount source
            # (below) are derived from the same trusted value. Blob filenames
            # trace back to an untrusted client upload; a path here would be an
            # arbitrary host write / mount on this node.
            for spec in input_blobs:
                spec["filename"] = safe_blob_name(
                    spec.get("filename"), str(spec.get("blob_id"))
                )

            if input_blobs or wants_output:
                tmp_dir = Path(tempfile.mkdtemp(prefix="hadcd-blobs-"))
                logger.debug("blob temp dir: %s", tmp_dir)

            if input_blobs:
                input_dir = tmp_dir / "input"
                input_dir.mkdir()
                try:
                    await blob_client.download_all(input_blobs, input_dir)
                except BlobClientError as exc:
                    # Fail the task cleanly — cannot proceed without inputs.
                    logger.error(
                        "task %s: blob download failed: %s", task_id, exc
                    )
                    await self._post_result(
                        client, task_id, nominal_power_w, 0.0,
                        success=False, error=f"blob download failed: {exc}",
                    )
                    if tmp_dir:
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                    return

                # Build volume specs for the container handler.
                existing_volumes = list(args.get("volumes") or [])
                for spec in input_blobs:
                    blob_filename = spec.get("filename") or spec["blob_id"]
                    existing_volumes.append({
                        "host_path": str(input_dir / blob_filename),
                        "container_path": spec.get(
                            "container_path",
                            f"/input/{blob_filename}",
                        ),
                        "mode": "ro",
                    })
                args["volumes"] = existing_volumes

            if wants_output:
                output_dir = tmp_dir / "output"
                output_dir.mkdir()
                args["output_dir"] = str(output_dir)

        # --- run the handler -----------------------------------------
        # tmp_dir (downloaded inputs + container outputs) must survive
        # until the blob upload below, so its cleanup lives in this
        # finally: it runs on success, on any exception, and on
        # cancellation — honouring the "always removed" contract above.
        try:
            outcome = await executor.run(task_type, args)

            # --- Phase 10c: blob post-processing ---------------------
            extra_result: dict = {}
            if output_dir and outcome.success:
                try:
                    blob_ids = await blob_client.upload_dir(
                        output_dir, task_id=str(task_id)
                    )
                    if blob_ids:
                        extra_result["output_blob_ids"] = blob_ids
                        logger.info(
                            "task %s: uploaded %d output blob(s): %s",
                            task_id, len(blob_ids), blob_ids,
                        )
                except BlobClientError as exc:
                    # Log but don't fail the task — the computation succeeded.
                    logger.warning(
                        "task %s: output blob upload failed: %s", task_id, exc
                    )
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        # --- post result to backend ----------------------------------
        result_payload = outcome.result
        if extra_result and isinstance(result_payload, dict):
            result_payload = {**result_payload, **extra_result}
        elif extra_result:
            result_payload = extra_result

        # Phase 16b: use Kasa-measured average watts when available.
        # Falls back to the task's estimated power when the meter is
        # disabled or has not yet produced a reading.
        measured_power_w = self._kasa_meter.average_watts_since(
            task_start_monotonic
        )
        actual_power_w = measured_power_w if measured_power_w is not None else nominal_power_w
        if measured_power_w is not None:
            logger.debug(
                "task %s: using measured %.1f W (estimated was %.1f W)",
                task_id,
                measured_power_w,
                nominal_power_w,
            )

        await self._post_result(
            client,
            task_id,
            actual_power_w,
            outcome.duration_sec,
            success=outcome.success,
            error=outcome.error if not outcome.success else None,
            result=result_payload if outcome.success else None,
        )
        logger.info(
            "task %s finished: %s (%.2fs, %.1f W)",
            task_id,
            "ok" if outcome.success else outcome.error,
            outcome.duration_sec,
            actual_power_w,
        )

        # Phase 13a: if this was a fill-tier task that returned session data,
        # post a heat session record to the backend earnings ledger.
        # Phase 16b: pass measured_kwh so the backend stores real kWh
        # instead of the node's declared max_power_kw estimate.
        _FILL_TYPES = {"gpu_mining_fill", "p2pool_fill", "synthetic_heat_fill"}
        if (
            outcome.success
            and task_type in _FILL_TYPES
            and isinstance(outcome.result, dict)
            and "session_start" in outcome.result
            and "session_end" in outcome.result
        ):
            # Compute measured kWh: avg_watts * seconds / 3_600_000.
            measured_kwh: float | None = None
            if measured_power_w is not None:
                measured_kwh = measured_power_w * outcome.duration_sec / 3_600_000.0

            await self._post_heat_session(
                client, task_id, task_type, outcome.result,
                measured_kwh=measured_kwh,
            )

        # Phase 10d: run image GC in a thread so it never blocks the event loop.
        # maybe_gc is a no-op when budget==0 or Docker is unreachable.
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, self._image_cache.maybe_gc)

    # --- result posting helper ----------------------------------------

    async def _post_result(
        self,
        client: httpx.AsyncClient,
        task_id,
        actual_power_w: float,
        actual_duration_sec: float,
        *,
        success: bool,
        error: str | None = None,
        result=None,
    ) -> None:
        """POST a task result to the backend.

        Used both by the normal path at the end of ``_run_assignment`` and
        by early-exit paths (e.g. blob-download failure) that need to mark
        the task as failed before the executor ever runs.
        """
        body: dict = {
            "node_id": self.state.node_id,
            "actual_power_w": actual_power_w,
            "actual_duration_sec": round(actual_duration_sec, 3),
        }
        if success:
            body.update(success=True, result=result)
        else:
            body.update(success=False, error=error)
        try:
            resp = await client.post(
                f"/api/work/{task_id}/result",
                json=body,
                headers=self._auth(),
            )
            if resp.status_code != 200:
                logger.warning(
                    "result for task %s rejected: HTTP %s — %s",
                    task_id,
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception:
            logger.exception("posting result for %s failed", task_id)

    async def _post_heat_session(
        self,
        client: httpx.AsyncClient,
        task_id,
        task_type: str,
        result: dict,
        *,
        measured_kwh: float | None = None,
    ) -> None:
        """POST a completed fill-tier session to the earnings ledger (Phase 13a).

        The backend calculates ``estimated_heat_kwh`` from the node's declared
        ``max_power_kw``; we only send the timing and metadata fields.

        Phase 16b: when ``measured_kwh`` is provided (Kasa plug reading),
        it is sent to the backend so the real metered value replaces the
        max_power_kw estimate for CRA-quality records.

        Failures are logged and swallowed — the main task result has already
        been posted successfully, and a missing earnings record is recoverable.
        """
        node_id = str(self.state.node_id)
        duration_sec = result.get("actual_sec") or result.get("duration_requested_sec") or 0

        body: dict = {
            "task_id": str(task_id),
            "source": task_type,
            "session_start": result["session_start"],
            "session_end": result["session_end"],
            "duration_sec": float(duration_sec),
        }

        # Phase 16b: real measured kWh from the Kasa plug (opt-in).
        if measured_kwh is not None:
            body["measured_kwh"] = round(measured_kwh, 6)

        # Fill in optional fields when the handler supplied them.
        if "active_mining_sec" in result:
            body["active_sec"] = float(result["active_mining_sec"])
        if "worker" in result:
            body["worker"] = result["worker"]
        if "gpu_model" in result:
            body["gpu_model"] = result["gpu_model"]
        if "threads" in result:
            body["threads"] = int(result["threads"])
        if "gpu_burn_active" in result:
            body["gpu_burn_active"] = bool(result["gpu_burn_active"])

        try:
            resp = await client.post(
                f"/api/nodes/{node_id}/heat_sessions",
                json=body,
                headers=self._auth(),
            )
            if resp.status_code == 201:
                logger.debug(
                    "heat session posted for task %s (source=%s, duration=%.0fs)",
                    task_id, task_type, duration_sec,
                )
            else:
                logger.warning(
                    "heat session post for task %s returned HTTP %s: %s",
                    task_id, resp.status_code, resp.text[:200],
                )
        except Exception:
            logger.exception("posting heat session for task %s failed", task_id)

    # --- Phase 17d: remote config push helpers -----------------------

    async def _sync_media_models(self, specs: list) -> None:
        """Download queued ComfyUI model files into the media models dir.

        Each spec is {url, rel_path}. Files land under COMFYUI_MODELS_PATH so
        the ComfyUI session container (which bind-mounts that dir) can use
        them. Idempotent: a file that already exists at the right size is
        skipped, so re-delivery of the same queue is cheap.
        """
        models_root = (self.settings.comfyui_models_path or "").strip()
        if not models_root:
            logger.warning(
                "media model-sync requested but COMFYUI_MODELS_PATH is unset — skipping"
            )
            return
        root = Path(models_root)
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            url = str(spec.get("url", "")).strip()
            rel_path = str(spec.get("rel_path", "")).strip().lstrip("/\\")
            if not url or not rel_path:
                continue
            # Contain the write to the models root — reject path traversal.
            dest = (root / rel_path).resolve()
            try:
                dest.relative_to(root.resolve())
            except ValueError:
                logger.warning("media model-sync: rejected unsafe rel_path %r", rel_path)
                continue
            if dest.exists() and dest.stat().st_size > 0:
                logger.info("media model-sync: %s already present — skipping", rel_path)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".part")
            try:
                async with httpx.AsyncClient(timeout=None, follow_redirects=True) as c:
                    async with c.stream("GET", url) as r:
                        r.raise_for_status()
                        with open(tmp, "wb") as f:
                            async for chunk in r.aiter_bytes(1024 * 1024):
                                f.write(chunk)
                tmp.replace(dest)
                logger.info("media model-sync: downloaded %s", rel_path)
            except Exception as exc:
                logger.error("media model-sync: failed %s: %s", rel_path, exc)
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass

    def _apply_pending_config(self, config: dict) -> None:
        """Write pending config key-value pairs to agent.env."""
        env_candidates = [
            Path("/etc/hadcd-agent/agent.env"),
            Path(".env"),
        ]
        target = next((p for p in env_candidates if p.exists()), None)
        if target is None:
            logger.warning(
                "heartbeat: pending config received but no agent.env found — skipping"
            )
            return
        try:
            lines = target.read_text().splitlines()
            updated: set = set()
            new_lines = []
            for line in lines:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key = stripped.split("=", 1)[0].strip()
                    if key in config:
                        new_lines.append(f"{key}={config[key]}")
                        updated.add(key)
                        continue
                new_lines.append(line)
            # Append any keys not already present in the file
            remainder = {k: v for k, v in config.items() if k not in updated}
            if remainder:
                new_lines.append("")
                new_lines.append("# --- set by HADCD remote config push ---")
                for k, v in remainder.items():
                    new_lines.append(f"{k}={v}")
            target.write_text("\n".join(new_lines) + "\n")
            logger.info(
                "heartbeat: applied pending config (%d key(s)) to %s",
                len(config),
                target,
            )
        except OSError as exc:
            logger.warning("heartbeat: could not write pending config: %s", exc)

    def _do_restart(self) -> None:
        """Replace the current process with a fresh copy to pick up new agent.env."""
        import os
        import sys
        logger.info("heartbeat: restarting agent process to apply remote config")
        try:
            # Try systemctl first (production systemd service)
            import subprocess
            result = subprocess.run(
                ["systemctl", "restart", "hadcd-agent"],
                timeout=5,
                capture_output=True,
            )
            if result.returncode == 0:
                return
        except Exception:
            pass
        # Fallback: exec-replace this process (dev / non-systemd)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # --- Phase 20b: distributed storage transfer loop ----------------

    _TRANSFER_POLL_SEC = 30.0   # how often to poll for new jobs
    _TRANSFER_CHUNK = 4 * 1024 * 1024  # 4 MB per chunk

    async def _transfer_loop(
        self,
        client: httpx.AsyncClient,
        stop: asyncio.Event,
    ) -> None:
        """Poll for and service storage transfer jobs.

        Called only when STORAGE_PATH is configured.  Each job is a
        request from the backend to replicate a file to this node; we
        download it chunk-by-chunk and ack when done.
        """
        while not stop.is_set():
            try:
                resp = await client.get(
                    "/api/storage/transfer-jobs",
                    headers=self._auth(),
                    timeout=15.0,
                )
                if resp.status_code == 200:
                    jobs = resp.json()
                    for job in jobs:
                        if stop.is_set():
                            break
                        await self._service_transfer_job(client, job)
                elif resp.status_code not in (401, 403):
                    logger.debug(
                        "storage: transfer-jobs poll returned HTTP %s",
                        resp.status_code,
                    )
            except Exception as exc:
                logger.warning("storage: transfer loop error: %s", exc)
            await _sleep_or_stop(stop, self._TRANSFER_POLL_SEC)

    async def _service_transfer_job(
        self,
        client: httpx.AsyncClient,
        job: dict,
    ) -> None:
        """Download and store one asset — P2P direct first, relay fallback.

        Phase 20c: if the backend knows a stored replica's Tailscale IP,
        the job response includes ``source_tailscale_ip`` / ``source_port``.
        We try a single streaming GET from that peer.  On any failure we
        fall back to the original chunked-relay path so transfers always
        succeed even if the peer is temporarily unreachable.

        Files are written to a .tmp staging path first and renamed
        atomically so a partial download never looks complete.
        """
        job_id: str = job["id"]
        sha256: str = job["sha256"]
        filename: str = job["filename"]
        chunks_total: int = int(job.get("chunks_total") or 1)
        size_bytes: int = int(job.get("size_bytes") or 0)
        source_ip: str | None = job.get("source_tailscale_ip")
        source_port: int = int(job.get("source_port") or 8015)

        pool_dir = Path(self._storage_path) / "pool"
        try:
            pool_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("storage: cannot create pool dir %s: %s", pool_dir, exc)
            return

        dest_path = pool_dir / sha256

        # Already have it — just ack and move on
        if dest_path.exists():
            try:
                await client.post(
                    f"/api/storage/transfer-jobs/{job_id}/ack",
                    headers=self._auth(),
                    timeout=10.0,
                )
            except Exception:
                pass
            return

        tmp_path = pool_dir / f"{sha256}.tmp"

        # ── Phase 20c: try P2P direct fetch ──────────────────────────
        # Skip P2P if source is ourselves (avoid self-loopback) or no IP.
        own_ts_ip = self._tailscale_ip
        if source_ip and source_ip != own_ts_ip:
            try:
                ok = await self._fetch_from_peer(
                    source_ip, source_port, sha256, size_bytes, dest_path, tmp_path, filename
                )
                if ok:
                    await client.post(
                        f"/api/storage/transfer-jobs/{job_id}/ack",
                        headers=self._auth(),
                        timeout=10.0,
                    )
                    return
            except Exception as exc:
                logger.warning(
                    "storage: P2P fetch from %s failed, falling back to relay: %s",
                    source_ip, exc,
                )
                tmp_path.unlink(missing_ok=True)

        # ── Relay fallback: chunked download through backend ─────────
        h = __import__("hashlib").sha256()
        try:
            with open(tmp_path, "wb") as fh:
                for seq in range(chunks_total):
                    resp = await client.get(
                        f"/api/storage/transfer-jobs/{job_id}/chunk/{seq}",
                        headers=self._auth(),
                        timeout=120.0,
                    )
                    if resp.status_code == 200:
                        data = resp.content
                        fh.write(data)
                        h.update(data)
                    else:
                        raise RuntimeError(
                            f"chunk {seq} returned HTTP {resp.status_code}"
                        )

            got = h.hexdigest()
            if got != sha256:
                raise ValueError(f"SHA-256 mismatch: expected {sha256[:8]}, got {got[:8]}")

            tmp_path.rename(dest_path)
            logger.info(
                "storage: stored %s (%s, %.1f MB) via relay",
                filename, sha256[:8], size_bytes / (1024 ** 2),
            )

            await client.post(
                f"/api/storage/transfer-jobs/{job_id}/ack",
                headers=self._auth(),
                timeout=10.0,
            )

        except Exception as exc:
            logger.error(
                "storage: transfer job %s failed (%s): %s",
                job_id[:8], filename, exc,
            )
            tmp_path.unlink(missing_ok=True)
            try:
                await client.post(
                    f"/api/storage/transfer-jobs/{job_id}/error",
                    json={"message": str(exc)[:500]},
                    headers=self._auth(),
                    timeout=10.0,
                )
            except Exception:
                pass

    async def _fetch_from_peer(
        self,
        ip: str,
        port: int,
        sha256: str,
        size_bytes: int,
        dest_path: Path,
        tmp_path: Path,
        filename: str,
    ) -> bool:
        """Stream a file directly from another node's Tailscale IP.

        Returns True on success, False if the peer returned a non-200
        status (treated as a soft failure so the relay path is tried).
        Raises on network/IO errors so the caller can log + fall back.
        """
        import hashlib as _hashlib
        url = f"http://{ip}:{port}/pool/{sha256}"
        h = _hashlib.sha256()

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=180.0, write=30.0, pool=5.0)
        ) as peer:
            async with peer.stream("GET", url) as resp:
                if resp.status_code != 200:
                    logger.debug(
                        "storage: peer %s returned HTTP %s for %s",
                        ip, resp.status_code, sha256[:8],
                    )
                    return False
                with open(tmp_path, "wb") as fh:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        fh.write(chunk)
                        h.update(chunk)

        got = h.hexdigest()
        if got != sha256:
            raise ValueError(
                f"SHA-256 mismatch: expected {sha256[:8]}, got {got[:8]}"
            )

        tmp_path.rename(dest_path)
        logger.info(
            "storage: P2P stored %s (%s, %.1f MB) from %s",
            filename, sha256[:8], size_bytes / (1024 ** 2), ip,
        )
        return True

    # --- Storage GC (Phase 20b/20c) -----------------------------------

    _STORAGE_GC_INTERVAL_SEC = 6 * 3600  # every 6 hours
    # Refresh dep_status every 60 heartbeat cycles (~1 hour at 60s intervals).
    _DEP_CHECK_INTERVAL = 60

    async def _storage_gc_loop(
        self,
        client: httpx.AsyncClient,
        stop: asyncio.Event,
    ) -> None:
        """Periodically remove stale files from the local storage pool.

        Fetches the backend's pool manifest (list of active SHA-256 hashes)
        and deletes any files in STORAGE_PATH/pool/ that are not in the
        manifest.  Also removes orphaned .tmp files from interrupted transfers.

        Runs every 6 hours; the first run is deferred by the full interval so
        the node's pool is fully populated before GC touches it.
        """
        # Defer first run so the initial transfer jobs have time to complete.
        await _sleep_or_stop(stop, self._STORAGE_GC_INTERVAL_SEC)
        while not stop.is_set():
            try:
                resp = await client.get(
                    "/api/storage/pool-manifest",
                    headers=self._auth(),
                    timeout=30.0,
                )
                if resp.status_code == 200:
                    active: set[str] = set(resp.json())
                    pool_dir = Path(self._storage_path) / "pool"
                    if pool_dir.exists():
                        removed = 0
                        for fp in pool_dir.iterdir():
                            name = fp.name
                            # Remove stale temp files from interrupted transfers.
                            if name.endswith(".tmp"):
                                try:
                                    fp.unlink()
                                    removed += 1
                                except OSError:
                                    pass
                                continue
                            # Remove pool files whose asset has been deleted.
                            if len(name) == 64 and name not in active:
                                try:
                                    fp.unlink()
                                    removed += 1
                                    logger.info(
                                        "storage GC: removed stale file %s", name[:8]
                                    )
                                except OSError as exc:
                                    logger.warning(
                                        "storage GC: could not remove %s: %s", name, exc
                                    )
                        if removed:
                            logger.info(
                                "storage GC: removed %d stale file(s)", removed
                            )
                else:
                    logger.debug(
                        "storage GC: manifest returned HTTP %s", resp.status_code
                    )
            except Exception as exc:
                logger.warning("storage GC: error: %s", exc)

            await _sleep_or_stop(stop, self._STORAGE_GC_INTERVAL_SEC)

    async def _drain_in_flight(self) -> None:
        if not self._run_tasks:
            return
        logger.info(
            "draining %d in-flight task(s) (up to %.0fs)",
            len(self._run_tasks),
            _DRAIN_GRACE_SEC,
        )
        _, pending = await asyncio.wait(
            self._run_tasks, timeout=_DRAIN_GRACE_SEC
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Sleep for `seconds`, returning early if `stop` is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
