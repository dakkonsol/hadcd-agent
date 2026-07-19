# Installing the HADCD agent under systemd (Linux)

A walkthrough for installing the agent as a hardened systemd service.
For the operator-facing view (choosing values, getting an enrollment
token, first-run checks), see
[docs/deployment.md](../../../docs/deployment.md).

Tested against systemd 245+ (Ubuntu 20.04+, Debian 11+, RHEL 8+).

---

## Quick install (one command)

If you cloned the HADCD repo on the target node, the installer automates
all the steps below:

```sh
sudo bash scripts/install-agent.sh
# Follow the prompts; edit /etc/hadcd-agent/agent.env, then:
sudo systemctl start hadcd-agent
```

The manual walkthrough follows for reference and for non-standard setups.

---

## Layout

```
/opt/hadcd-agent/              the agent source tree (this repo, or just
                               the agent/ + hadcd_workloads/ directories)
  .venv/                       Python venv with the agent's deps
/etc/hadcd-agent/
  agent.env                    env file the unit reads (0640 root:hadcd-agent)
/var/lib/hadcd-agent/          writable state
  state.json                   the node's persistent identity (node_id + token)
  bms.json                     when BMS_SOURCE=file, the BMS writes here
```

---

## Before you start: install Sunshine (strongly recommended)

HADCD strongly recommends **Sunshine** for any node that also functions
as a desktop or gaming PC. It gives you:

1. Remote desktop / game streaming — Moonlight on your laptop, this
   machine does the GPU work.
2. Session detection — the agent learns when you are using the machine
   and pauses background mining / heat-fill so it never competes with
   you for the GPU.

```sh
# Debian/Ubuntu — grab the latest .deb from the releases page:
#   https://github.com/LizardByte/Sunshine/releases
sudo apt install ./sunshine-*.deb
# Then open https://localhost:47990 and set the admin password.
```

See [docs/sunshine-setup.md](../../../docs/sunshine-setup.md) for the
full walkthrough. The agent runs without Sunshine but logs a loud
advisory and fill-tier work has no way to detect interactive use.

---

## Manual steps

### 1. Create the service user

```sh
sudo useradd --system \
  --home /var/lib/hadcd-agent \
  --create-home \
  --shell /usr/sbin/nologin \
  hadcd-agent
# Add to the docker group so the agent can reach the Docker socket.
sudo usermod -aG docker hadcd-agent
```

### 2. Install Python 3.11+ and create the venv

```sh
sudo apt install python3.11 python3.11-venv   # Debian/Ubuntu
# or: sudo dnf install python3.11              # RHEL/Rocky
```

### 3. Copy the agent code

```sh
sudo mkdir -p /opt/hadcd-agent
sudo cp -r /path/to/checkout/agent         /opt/hadcd-agent/
sudo cp -r /path/to/checkout/hadcd_workloads /opt/hadcd-agent/
sudo chown -R root:hadcd-agent /opt/hadcd-agent
```

### 4. Create the venv and install dependencies

```sh
sudo python3.11 -m venv /opt/hadcd-agent/.venv
sudo /opt/hadcd-agent/.venv/bin/pip install -r /opt/hadcd-agent/agent/requirements.txt
```

### 5. Drop in the env file

```sh
sudo mkdir -p /etc/hadcd-agent
sudo cp /opt/hadcd-agent/agent/config.env.example /etc/hadcd-agent/agent.env
sudo chown root:hadcd-agent /etc/hadcd-agent/agent.env
sudo chmod 0640 /etc/hadcd-agent/agent.env
# Edit the file — at minimum set:
#   HADCD_API, ENROLLMENT_TOKENS, NODE_NAME, MAX_POWER_KW
# If Sunshine is installed, also set:
#   SESSION_SOURCE=sunshine
#   SUNSHINE_PASSWORD=<your Sunshine admin password>
sudo $EDITOR /etc/hadcd-agent/agent.env
```

### 6. Sanity-run in the foreground (recommended)

Before installing as a service, run once in the foreground as the agent
user to confirm the config is correct:

```sh
sudo -u hadcd-agent \
  env $(grep -v '^#' /etc/hadcd-agent/agent.env | xargs) \
  /opt/hadcd-agent/.venv/bin/python -m agent run
```

Watch for `enrolled as node <UUID>` followed by heartbeat 200s.
Press Ctrl-C to stop. The identity is now persisted to
`/var/lib/hadcd-agent/state.json`.

### 7. Install and start the service

```sh
sudo cp /opt/hadcd-agent/agent/deploy/systemd/hadcd-agent.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hadcd-agent
sudo systemctl status hadcd-agent     # should show "active (running)"
journalctl -u hadcd-agent -n 50 -f   # watch enrollment + first ticks
```

---

## Enable auto-start on AC power (power-outage recovery)

The node should boot automatically when its power bar is switched on —
no one should need to press the power button. This is a BIOS/UEFI
setting, not a software one.

**If your hardware has IPMI** (server boards, some workstations), the
installer sets this automatically via `ipmitool chassis policy always-on`.
You can also set it manually:

```sh
ipmitool chassis policy always-on
```

**For consumer mini PCs and desktops**, find the setting in BIOS:

| Manufacturer / board | BIOS path |
|---|---|
| **Intel NUC** | Power → After Power Failure → **Power On** |
| **ASUS (desktop / mini PC)** | Advanced → APM Configuration → Restore on AC Power Loss → **Power On** |
| **ASRock** | Advanced → ACPI Configuration → Restore after AC Power Loss → **Power On** |
| **Gigabyte** | Settings → Miscellaneous → AC Back → **Always On** |
| **Beelink / Minisforum (AMI BIOS)** | Chipset → South Bridge → AC Loss Power State → **Always On** |
| **MSI** | Settings → Advanced → Power Management → Restore after AC Power Loss → **Always On** |

> **What this achieves:** toggling the power strip on/off becomes the
> only on/off switch you need. If there's a utility outage the node
> reboots automatically when power is restored. No one is required on-site.

---

## What "hardened" means in this unit

The service file (Phase 13b) ships with several changes over the
initial placeholder:

| Setting | Old | New | Reason |
|---|---|---|---|
| `Type` | `simple` | `notify` | systemd waits for READY=1 before marking "active"; a restart that returns "active" truly means the agent is enrolled |
| `Restart` | `on-failure` | `always` | also restarts after a clean exit (a daemon should never stop voluntarily) |
| `RestartSec` | 5 s | 10 s | gives the backend/network time to recover |
| `WatchdogSec` | — | 90 s | kills and restarts a frozen asyncio loop |
| `StartLimitBurst` | — | 5 / 120 s | allows 5 rapid restarts then backs off |
| `ProtectSystem` | `strict` | `full` | `strict` blocks Docker socket access patterns on some distros |
| `SupplementaryGroups=docker` | — | added | Docker socket access without owning it |

---

## Platform quirks

- **SELinux (RHEL/Rocky/Alma).** If `journalctl -u hadcd-agent` shows
  `Permission denied` writing `state.json`, run:
  ```sh
  sudo chcon -R -t var_lib_t /var/lib/hadcd-agent
  ```
- **AppArmor (Ubuntu).** No extra profile needed.
- **`network-online.target` absent.** If the unit fails to start on
  some minimal installs, ensure `systemd-networkd-wait-online` or
  `NetworkManager-wait-online` is enabled:
  ```sh
  sudo systemctl enable --now systemd-networkd-wait-online
  ```

---

## Upgrades

```sh
sudo systemctl stop hadcd-agent
# Replace /opt/hadcd-agent/agent and /opt/hadcd-agent/hadcd_workloads
sudo /opt/hadcd-agent/.venv/bin/pip install -r \
     /opt/hadcd-agent/agent/requirements.txt
sudo systemctl start hadcd-agent
```

The agent's enrolled identity (`/var/lib/hadcd-agent/state.json`)
survives upgrades — no re-enrollment needed.
