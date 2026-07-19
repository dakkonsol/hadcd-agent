"""Agent configuration, loaded from the process environment.

All settings are env-driven so the agent can run identically from a
docker-compose service, a systemd unit, or a Windows Service. The
node's *identity* (its `node_id` + bearer token) is **not** here —
that lives in the JSON state file (see `agent/state.py`) so it
survives restarts without operator intervention.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Backend connection ------------------------------------------
    # URL of the central server. Default works inside docker-compose.
    hadcd_api: str = "http://backend:8000"

    # The backend's ENROLLMENT_TOKENS env var is a comma-separated list
    # to support rotation. The agent uses the first value; reading via
    # the same env name keeps a single source of truth in .env.
    enrollment_tokens: str = ""

    @property
    def enrollment_token(self) -> str:
        for piece in self.enrollment_tokens.split(","):
            piece = piece.strip()
            if piece:
                return piece
        return ""

    # --- Identity persistence ----------------------------------------
    # Where to keep the node_id + bearer token across restarts. The
    # default fits a containerised deployment; on a host, point this
    # at e.g. /var/lib/hadcd-agent/state.json with appropriate perms.
    agent_state_file: str = "/var/lib/hadcd-agent/state.json"

    # --- Declared capabilities (sent once at enrollment) -------------
    node_name: str = "HADCD Agent"
    node_type: str = "office"  # community_centre | pool | arena | office
    max_power_kw: float = 5.0
    cpu_capacity: int = 2
    gpu_capacity: int = 0
    # Total GPU VRAM in GB across all GPUs on this node.  Reported at
    # enrollment and refreshed on every heartbeat so the session assigner
    # and Layer 3 planner always have an accurate value without requiring
    # re-enrollment after a hardware change.
    # Leave None (default) for CPU-only nodes (gpu_capacity = 0).
    # Example: 8.0 for an RTX 4060 8 GB; 24.0 for an RTX 4090.
    gpu_vram_gb: float | None = None
    ram_gb: float = 8.0
    bandwidth_mbps: float = 100.0
    # Phase 26 — relative inference performance: tokens/sec generating
    # with the network's reference model+quantisation on this hardware.
    # Reported at enrollment and refreshed on every heartbeat. Leave
    # None if unbenchmarked — the dispatcher then never routes
    # perf-constrained work here, and treats the node as the weakest
    # (first-choice) candidate for unconstrained work.
    perf_score: float | None = None

    # --- Phase 8e — zone pairing ------------------------------------
    # Physical zone this node heats. Free-text label shown in the
    # operator UI, e.g. "Living Room", "Office", "Bedroom 2". Blank
    # means "no zone assigned". Operators can also update it from the
    # UI without re-enrolling the agent.
    zone_name: str = ""
    # When true, the dispatcher only routes work to this node when it
    # is currently reporting non-zero heat demand — the node heats only
    # its own zone and never acts as overflow for another room's demand.
    require_own_demand: bool = False

    # --- Execution ---------------------------------------------------
    # How many offloaded tasks to run concurrently. Default 1 matches a
    # "one building, one accelerator" model; raise it if the host has
    # spare cores AND the BMS reports enough demand for multiple
    # simultaneous burns.
    agent_concurrency: int = 1

    # --- Container task security floor -------------------------------
    # Extra host-path prefixes that dispatched container tasks may
    # bind-mount, os.pathsep-separated (';' on Windows, ':' on Linux).
    # The agent's own blob-staging temp dirs are always mountable; every
    # other payload-supplied host path is rejected unless it sits under
    # one of these prefixes. This is deliberately a *local* setting —
    # the dispatcher can never widen it. Leave empty (default) unless
    # this node runs trusted operator tasks that mount host data dirs.
    container_mount_allowlist: str = ""
    # When true, every container task on this node runs with the full
    # hardened floor (cap_drop=ALL, no-new-privileges, unprivileged,
    # isolated bridge network) even if the dispatcher marked it as a
    # trusted operator task. Recommended for independent/BYO hosts that
    # do not operate their own dispatcher.
    container_require_hardened: bool = False

    # --- Loop cadences (real seconds; the backend's thresholds use the
    # same scale, so do not compress here) ---------------------------
    heartbeat_interval_sec: float = 10.0
    demand_interval_sec: float = 15.0
    work_poll_interval_sec: float = 6.0

    # --- BMS data source (Phase 7c — pluggable HeatSource) ----------
    # Selects the heat-demand adapter. 'file' reads a JSON file the
    # BMS (or a bridging script) writes; 'http' polls a URL that
    # returns the same JSON shape (suitable for REST-faced BMS systems
    # or for a sidecar that translates BACnet/Modbus into JSON).
    # Future adapters slot in here without changing the agent itself.
    bms_source: str = "file"

    # --- File adapter -----------------------------------------------
    bms_file: str = "/var/lib/hadcd-agent/bms.json"

    # --- HTTP adapter -----------------------------------------------
    # URL to GET each demand tick. Required when bms_source == "http".
    bms_http_url: str = ""
    # Optional Authorization header to send verbatim, e.g.
    # "Bearer xxx" or "Basic xxx". Many BMS APIs need this.
    bms_http_auth_header: str = ""
    # Per-request timeout. Short so a slow BMS does not stall the
    # demand loop — a missed tick is recoverable, a stuck one is not.
    bms_http_timeout_sec: float = 5.0

    # --- Ecobee adapter (Phase 8c) ----------------------------------
    # Required when bms_source == "ecobee". ECOBEE_API_KEY is the
    # developer-app client ID from ecobee.com/developers; not
    # secret in the strong sense (similar to OAuth client_id
    # elsewhere). The refresh token IS secret — it lives in
    # ECOBEE_STATE_FILE and is rotated on every refresh.
    ecobee_api_key: str = ""
    ecobee_state_file: str = "/var/lib/hadcd-agent/ecobee_state.json"
    # Which thermostat to read. Use 'python -m agent.ecobee_setup' to
    # discover the IDs registered against your account; the setup
    # flow also persists it to the state file for convenience.
    ecobee_thermostat_id: str = ""
    # How much heat-demand to report when the thermostat is actively
    # calling for heat. Ecobee tells us "heat is on / off"; HADCD
    # works in kW. This is the operator's declared demand value —
    # typically close to the node's max heating contribution.
    ecobee_demand_when_heating_kw: float = 5.0
    # Per-request timeout for Ecobee API calls. Slightly more
    # generous than http because Ecobee's API is occasionally slow.
    ecobee_timeout_sec: float = 10.0

    # How long the BMS expects to keep wanting heat after the reading
    # was written. Surfaced to the dispatcher as expected_window_sec
    # unless the reading itself supplies one.
    bms_default_window_sec: int = 1800

    # --- Interactive-session detection (Phase 10g) ------------------
    # Detects when the user is interactively using this machine
    # (gaming, a live AI app, anything streamed via Sunshine) so the
    # dispatcher pauses fill-tier work (mining, synthetic heat-fill)
    # and doesn't compete with them. Reported to the backend on each
    # heartbeat.
    #   none     — no detector; fill tiers run whenever heat is
    #              demanded. Default. The agent logs a loud advisory
    #              at startup recommending Sunshine.
    #   sunshine — poll a local Sunshine instance's /api/connections.
    session_source: str = "none"

    # Sunshine's local web API. Default is Sunshine's standard HTTPS
    # port on loopback. Sunshine serves a self-signed cert; the
    # adapter does not verify TLS for this loopback call.
    sunshine_url: str = "https://localhost:47990"
    # Sunshine's admin credentials double as the API credentials
    # (HTTP Basic). Username defaults to Sunshine's default; the
    # password is required when session_source=sunshine.
    sunshine_username: str = "sunshine"
    sunshine_password: str = ""
    sunshine_timeout_sec: float = 5.0

    # --- Home Assistant adapter (Phase 8d) -------------------------
    # Reads heat demand from a local Home Assistant instance. No
    # developer registration needed — authentication is a long-lived
    # access token generated from the HA profile page.
    #   Settings → Your profile → Long-lived access tokens → Create
    #
    # Required when BMS_SOURCE=homeassistant:
    #   HA_TOKEN      — the long-lived token (treat as a password)
    #   HA_ENTITY_ID  — the climate entity to read, e.g.
    #                   "climate.living_room" or "climate.ecobee_home"
    #                   Find it in HA: Settings → Devices & Services →
    #                   Entities, filter by domain "climate".
    ha_url: str = "http://homeassistant.local:8123"
    ha_token: str = ""
    ha_entity_id: str = ""
    # kW to report when hvac_action=="heating". Set close to the
    # node's max heating contribution — same guidance as for Ecobee.
    ha_demand_when_heating_kw: float = 5.0
    ha_timeout_sec: float = 5.0
    # Temperature unit Home Assistant is configured in. "C" for
    # Celsius (default, correct for Canada and most HA installs);
    # "F" for Fahrenheit — the adapter converts to Celsius.
    ha_temperature_unit: str = "C"
    # --- Home Assistant space heater (Phase 18k) --------------------
    # Entity ID of a switch or input_boolean that controls a physical
    # space heater on the local HA network.  When set, the autonomous
    # offline fallback turns this heater on instead of running a
    # synthetic CPU/GPU burn — the house stays warm without wearing
    # out the node's hardware.  HA runs on the LAN so it is reachable
    # even when the internet (and the HADCD dispatcher) is down.
    #
    # Priority when offline and heat is demanded but mining is not
    # configured:
    #   1. GPU + CPU mining handlers (earns money, heats via compute)
    #   2. HA space heater (this setting) — real heater, no GPU wear
    #   3. Synthetic heat fill — last resort CPU/GPU burn
    #
    # Requires HA_TOKEN to already be set (same token used by the
    # BMS adapter when BMS_SOURCE=homeassistant).
    #
    # Example — a smart-plug-controlled space heater:
    #   HA_HEATER_ENTITY_ID=switch.office_space_heater
    ha_heater_entity_id: str = ""

    # --- Building hub vacancy setback (Phase 22) --------------------
    # Thermostat setpoint (°C) to hold while the building manager has
    # marked this node's room vacant on the Building view. Used when
    # the dispatcher doesn't supply a per-node setback_temp_c. 13 °C
    # keeps pipes safe and recovers quickly when the room is re-opened.
    setback_temp_c: float = 13.0

    # --- Blob storage (Phase 10c) ----------------------------------
    # Where the agent downloads input blobs before mounting them into
    # container tasks, and where it reads output blobs before uploading
    # them back to the backend. This directory is also the Docker
    # bind-mount source for blob volumes in container tasks — it must
    # be on the same filesystem that the Docker daemon sees.
    #   Linux:   /var/lib/hadcd-agent/blobs
    #   Windows: C:\ProgramData\hadcd-agent\blobs
    blob_storage_dir: str = "/var/lib/hadcd-agent/blobs"

    # --- Image cache (Phase 10d) ------------------------------------
    # Comma-separated list of Docker image references to pre-pull at
    # agent startup. Images are pulled in the background after
    # enrollment so they're warm before the first matching task
    # arrives. AI images (Whisper, Stable Diffusion, Ollama) are
    # 3-20 GB; a cold pull on a home connection takes 10-30 min and
    # would make the first task appear to hang. Pre-pulling amortises
    # that cost.
    #   Example: ollama/ollama:latest,ghcr.io/openai/whisper:latest
    docker_prepull_images: str = ""

    # Maximum total disk space (in GiB) that Docker image layers may
    # occupy before the agent prunes dangling and unused images.
    # Checked after each container task completes. Set to 0 to
    # disable automatic GC entirely (not recommended on small disks).
    # Dangling images (untagged) are always safe to remove. Unused
    # images (tagged, but not used by a running container) are removed
    # only when the total exceeds this budget.
    docker_image_budget_gb: float = 50.0

    # --- GPU mining fill (Phase 9b — T-Rex Miner → NiceHash) --------
    # Wallet addresses are deliberately NOT in the task ledger — they
    # live here in the agent environment only. Only the first 8 chars
    # are written to the CSV session log.
    #
    # Leave NICEHASH_TREX_PATH empty to disable GPU mining on this
    # node; the handler skips gracefully if not configured.
    # T-Rex GitHub: https://github.com/trexminer/T-Rex
    nicehash_trex_path: str = ""
    nicehash_wallet: str = ""
    nicehash_worker_name: str = ""  # defaults to hostname at runtime
    nicehash_pool_host: str = "auto.nicehash.com"
    nicehash_pool_port: int = 9200
    # GPU device index to pass to excavator. 0 = first GPU.
    mining_gpu_index: int = 0
    # Non-miner GPU utilisation (%) that triggers a miner pause.
    mining_gpu_pressure_pct: float = 20.0
    # Utilisation below which mining is resumed after a pause.
    mining_gpu_resume_pct: float = 10.0
    # How often (seconds) to poll nvidia-smi for GPU pressure.
    mining_poll_interval_sec: float = 10.0
    # CSV path for per-session GPU mining records (CRA-ready log).
    mining_payout_log: str = "/var/lib/hadcd-agent/gpu_mining_sessions.csv"

    # --- Media workload (ComfyUI image/video/3D/audio) --------------
    # Leave COMFYUI_MODELS_PATH empty to disable media on this node: the
    # agent then reports media_capable=false and the dispatcher never sends
    # it a `media` session. When set, it is the host directory bind-mounted
    # into the ComfyUI container's models dir (and the target directory for
    # remote model sync). Media sessions are only ever dispatched to
    # operator-owned nodes — see WORKLOAD_POLICY.md §4a.
    comfyui_models_path: str = ""
    comfyui_image: str = "ghcr.io/ai-dock/comfyui:latest"

    # --- CPU mining fill (Phase 9b — XMRig / P2Pool) ----------------
    # Leave XMRIG_PATH empty to disable CPU mining on this node.
    xmrig_path: str = ""
    xmr_wallet_address: str = ""
    p2pool_node_url: str = "p2pool.io:3333"  # p2pool.io:3334 = main chain
    xmrig_worker_name: str = ""  # defaults to hostname at runtime
    # 0 = auto: all logical cores minus one (leaves headroom for OS).
    xmrig_threads: int = 0
    cpu_mining_poll_sec: float = 15.0
    # CSV path for per-session CPU mining records (CRA-ready log).
    cpu_mining_payout_log: str = "/var/lib/hadcd-agent/cpu_mining_sessions.csv"

    # --- Synthetic heat fill (Phase 9c) --------------------------------
    # Tier 3 fallback: pure CPU/GPU burn when no mining is configured.
    # Always available — no wallet, no binary, no external service.
    # SYNTHETIC_HEAT_THREADS=0 means "all logical cores minus one".
    synthetic_heat_threads: int = 0
    # Set to "true" to also burn the GPU via PyTorch MatMul (requires
    # torch with CUDA; silently skipped if unavailable).
    synthetic_heat_gpu: str = "false"
    # How often the burn loop checks the deadline (seconds).
    synthetic_heat_poll_sec: float = 1.0
    # CSV path for per-session records.
    synthetic_heat_log: str = "/var/lib/hadcd-agent/synthetic_heat_sessions.csv"

    # --- Geographic location (Phase 11a) ----------------------------
    # WGS-84 decimal coordinates of this node's physical location.
    # Required for weather-driven Vast.AI provider windows (Phase 11).
    # Passed to the backend at registration; the operator can also edit
    # them via the UI without re-enrolling.
    # Leave blank/null if Vast.AI integration is not planned.
    node_latitude: float | None = None
    node_longitude: float | None = None
    # Human-readable location label shown in the operator UI,
    # e.g. "Ottawa, ON, CA". Cosmetic — not used by the scheduler.
    node_location_label: str | None = None

    # --- Vast.AI provider (Phase 11c) --------------------------------
    # Set VASTAI_API_KEY + VASTAI_MACHINE_ID to enable weather-driven
    # Vast.AI rental windows.  When either is empty the entire provider
    # integration is skipped and the agent runs as pure heat-fill mode.
    #
    # Prerequisites:
    #   1. Register as a Vast.AI host at https://vast.ai/become-a-host
    #   2. pip install vastai  (or ensure `vastai` CLI is in PATH)
    #   3. Find your machine ID: vastai show machines
    #
    # Security: the API key grants full control over your Vast.AI
    # account. Store it only in the agent's .env file, never in code.
    vastai_api_key: str = ""
    vastai_machine_id: str = ""

    # Path / name of the vastai CLI binary. "vastai" assumes it is on
    # PATH (the normal result of `pip install vastai`). Override with
    # an absolute path if the binary is not on the agent's PATH.
    vastai_cmd: str = "vastai"

    # How often the agent checks its Vast.AI schedule and reconciles
    # the machine's listing state (seconds). 60 s gives a 1-minute
    # reaction time after each WeatherPoller update, which is fast
    # enough given that cold windows are hours long.
    vast_check_interval_sec: float = 60.0

    # How many minutes before a cold window starts to proactively list
    # the machine.  A brief head-start lets Vast.AI propagate the offer
    # before the window opens.  10 minutes is a good default.
    vast_pre_list_minutes: float = 10.0

    # Phase 11d — rental session log.  Each completed rental cycle
    # (LISTED → UNLISTED) is appended to this CSV with columns:
    #   listed_at, unlisted_at, duration_hrs, machine_id
    # Actual income must be confirmed from the Vast.AI dashboard.
    # Leave empty to disable session logging.
    vast_payout_log: str = "/var/lib/hadcd-agent/vast_sessions.csv"

    # --- Vast.AI autonomous weather thresholds (Phase 15b) ---------
    # When the dispatcher is offline, the agent fetches weather directly
    # from Open-Meteo and makes its own listing decisions. These should
    # match the backend's weather_cold_threshold_c / weather_min_cold_hours
    # settings (configured from the operator UI) so the node behaves
    # identically whether the dispatcher is online or not.
    vast_cold_threshold_c: float = 10.0  # °C — list on Vast.AI when outdoor temp drops below this
    vast_min_cold_hours: float = 2.0     # min contiguous cold hours to act on
    # Safety floor for the unlist (hot) threshold.  The effective threshold is
    # max(vast_hot_threshold_c, thermostat_setpoint_c) — whichever is higher.
    #   • Thermostat healthy at 20 °C → unlist above 20 °C (setpoint wins).
    #   • Thermostat buggy at 5 °C   → floor of 16 °C wins; node won't keep
    #     listing into warm weather because of a glitched setpoint.
    #   • No setpoint reported        → 16 °C floor used directly.
    # Must match backend weather_hot_threshold_c for identical offline behaviour.
    vast_hot_threshold_c: float | None = 16.0  # floor °C; effective = max(this, setpoint)
    vast_min_hot_hours: float = 2.0             # min contiguous hot hours to act on

    # How often to refresh weather from Open-Meteo while operating offline
    # (seconds). 3600 = once per hour — fine for hour-scale cold windows.
    vast_weather_refresh_sec: float = 3600.0

    # --- Kasa KP125M smart plug power meter (Phase 16b) ------------
    # Real-time wattage measurement for CRA-quality earnings records.
    # The node's power supply is plugged into the Kasa plug; the meter
    # measures total node draw, which equals the single running task's
    # draw at AGENT_CONCURRENCY=1 (the default).
    #
    # KP125M uses TP-Link's KLAP protocol and requires credentials.
    # Older plugs (KP115, KP125 without the M) work without credentials
    # — leave KASA_USERNAME and KASA_PASSWORD empty in that case.
    #
    # Leave KASA_PLUG_IP empty to disable; the agent falls back to the
    # task's estimated power (previous behaviour).
    kasa_plug_ip: str = ""
    kasa_username: str = ""        # TP-Link account email (required for KP125M)
    kasa_password: str = ""        # TP-Link account password (required for KP125M)
    # How often to poll the plug. 10 s is fine for heat accounting;
    # faster polling adds no useful precision for multi-minute tasks.
    kasa_poll_interval_sec: float = 10.0

    # --- Node role (Phase 18j) --------------------------------------
    # "standard"  : thermostat-controlled node; sessions assigned only
    #               when the heat source reports demand_kw > 0.  Reports
    #               needs_heat=True/False on each heartbeat.  Vast.AI
    #               listing follows the weather schedule as normal.
    # "always_on" : dedicated always-on server (no thermostat, no
    #               Vast.AI).  Sessions assigned as fallback when no
    #               standard node is demanding heat.  Always reports
    #               needs_heat=False.
    node_role: str = "standard"

    # --- Asset storage (Phase 20a) ----------------------------------
    # Absolute path to the directory this node contributes as shared
    # storage for the HADCD asset library (LoRAs, documents, datasets).
    # Leave empty to opt this node out of storage contribution.
    # Examples:
    #   /mnt/data          — a dedicated data partition
    #   /var/lib/hadcd-assets — a subdirectory on the main drive
    storage_path: str = ""

    # --- Phase 20c — P2P storage server port ------------------------
    # The port this node's storage HTTP server listens on.  Other nodes
    # with Tailscale access connect directly to
    #   http://{tailscale_ip}:{storage_server_port}/pool/{sha256}
    # to fetch replicas without going through the backend relay.
    # Change this only if 8015 conflicts with another service on the host.
    storage_server_port: int = 8015

    # --- Autonomous offline fallback (Phase 15a) --------------------
    # When the dispatcher has been unreachable for this many consecutive
    # seconds (roughly N / heartbeat_interval_sec missed heartbeats),
    # the agent enters autonomous mode: it drives GPU + CPU mining
    # directly from the local thermostat, without waiting for task
    # assignments.  Default 60 s = ~6 missed heartbeats.
    autonomous_fallback_after_sec: int = 60

    # Duration of each mining chunk while in autonomous mode.  After
    # each chunk the thermostat is re-checked; if heat is no longer
    # demanded the miners stop.  On reconnect the current chunk is
    # allowed to finish before normal dispatch resumes.
    autonomous_chunk_sec: int = 120
