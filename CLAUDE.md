# HADCD Node Agent — developer & agent orientation

Read this first. This repo is **public and AGPL-3.0** (`dakkonsol/hadcd-agent`)
so prospective hosts can audit exactly what runs on their hardware. History is
kept clean and secret-free by design.

## What the agent is

The software that runs **on a host's machine** in the HADCD network. It polls the
dispatcher over an **outbound HTTPS** connection, reports the machine's heat
demand / capacity / health, and runs the workloads the dispatcher assigns
(AI inference, media generation, mining) in Docker containers — pausing customer
work appropriately (e.g. when the host is using remote desktop). It never accepts
inbound connections and never exposes the host directly.

## Layout

- `agent/` — the package. Entry point is `python -m agent run`
  (`__main__.py` also has `wifi-provision` and the phone-friendly `provision`
  wizard). Key files:
  - `agent.py` — the main poll/heartbeat loop; reports capacity + heat demand,
    scans cached models (`list_cached_models`) for warm-model routing.
  - `rental_session_handler.py` — rental **session lifecycle** + stale-active-
    session recovery (reconciles DB `active` sessions against Docker on restart;
    the `/lost` dispatcher callback settles a vanished container).
  - `session_source.py`, `executor.py`, `provisioner.py`, `watchdog.py`,
    `image_cache.py`, `blob_client.py`, `storage_server.py`.
  - integrations: `tailscale.py`, `wifi_provision.py`, `vast_provider.py`,
    `ecobee_setup.py`, `heat_source.py`, `kasa_power_meter.py`, `weather.py`.
- `scripts/` — `install-agent.sh`, `install-ha.sh`, `vast-register.sh` (base
  installers; not yet the staged idempotent bootstrap — that lives in the
  `dakkonsol/hadcd` `node/` tree).
- `hadcd_workloads/`, `docs/`.

## Run / verify

```
python -m agent run                                          # run against the configured backend
docker compose run --rm --entrypoint pytest agent tests/     # tests (agent/pytest.ini: pythonpath = .., asyncio_mode = auto)
```

Tests are container-targeted (the package imports from one dir up at `/app/agent`).

## Deployment reality (important)

- Deployed to **`/opt/hadcd-agent`** (a deployed copy with **no `.git`**), run by
  `hadcd-agent.service` (systemd): `WorkingDirectory=/opt/hadcd-agent`,
  `ExecStart=…/.venv/bin/python -m agent run`,
  `EnvironmentFile=/etc/hadcd-agent/agent.env`, `User=hadcd-agent`.
- Config (`/etc/hadcd-agent/agent.env`, non-secret keys): `NODE_ROLE=always_on`,
  `GPU_CAPACITY=1`, `GPU_VRAM_GB=24`, `COMFYUI_IMAGE=hadcd/comfyui:vesta-media-v8`.
  Secret keys (`ENROLLMENT_TOKENS`, `HADCD_API`, …) live here too — never read or
  print values.
- Canonical source = `dakkonsol/hadcd-agent` `main`. The deployed copy must map to
  a committed revision; capture any node-only deltas back to git before changing
  the runtime (this was done through commit `481cb58`).

## Invariants & landmines

- **AGPL, public, secret-free history.** Never commit tokens, keys, or host
  identity. Config stays in `agent.env`, not the repo.
- **Never restart the agent while a rental session container is active** — it can
  orphan a customer's GPU session. Installers/activation must check first.
- **Back up before mutating config; provide a tested rollback.** The node keeps
  timestamped `/etc/hadcd-agent/agent.env.before-*` backups — preserve them.
- **ComfyUI internal port is `18188`** on the deployed runtime (a stale dev
  checkout used `8188`). Validators/tools must use `18188`.
- Commit in small reviewed units; don't push or touch the live node without
  explicit owner approval.
