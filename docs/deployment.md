# Deploying a HADCD node agent

This guide takes one heat-capable building from "nothing installed"
to "agent running as a service, receiving offloaded compute, heat
flowing into the building." It is written for an infrastructure
operator who has used Linux or Windows in anger but has not seen this
project before.

If you are the *central server* operator and need to provision a new
node, jump to [§ For the central operator](#for-the-central-operator).

---

## What you are deploying

One **node agent** — a small Python service that:

1. enrolls with the central HADCD server (one-time);
2. reports the building's current heat demand (read from your BMS);
3. pulls compute tasks the central server has decided to send here;
4. runs them on this host's CPU (or GPU, later);
5. returns each result, telling the server how much heat the
   computation produced.

The waste heat from running the compute warms the room. That is the
entire point: heat the building gets for free, in proportion to its
real-time demand.

The agent is **best-effort**: if it's down, offline, or saying "no
heat needed right now," the central server quietly runs the work
itself. Nothing breaks. Building operators can pause participation
from the web UI at any time without telling you.

---

## What you need before you start

From the **central HADCD operator**:

- **The server's URL.** Something like `https://hadcd.example.org`.
  This is what the agent will call to enroll, heartbeat, and pull
  work. It must be reachable from the building's network — typically
  outbound HTTPS to that hostname.
- **An enrollment token.** A short string starting `hadcd_enroll_`.
  This is a shared secret that proves the agent has been authorised
  to register. It is used only for the first start; after that the
  agent has its own per-node bearer.

From your **building operations**:

- **A BMS data source.** Either:
  - the BMS (or a small bridging script of yours) can write a JSON
    file to disk every ~15 seconds → use the **file** adapter; or
  - the BMS has a REST endpoint returning JSON → use the **http**
    adapter.
  - JSON shape (all fields optional except `measured_kw`):
    ```json
    {
      "measured_kw":         8.5,
      "setpoint_c":          21.0,
      "room_temp_c":         19.3,
      "expected_window_sec": 1800
    }
    ```
- **A host to run on.** Modest spec — anything that can run Python
  3.11 and is on the building's network. Outbound HTTPS to the
  central server is the only required network hole. If using the
  http BMS adapter, also reachability to the BMS's REST endpoint
  from this host.

You do **not** need:
- Docker on the building's host (the agent runs as a normal Python
  service under systemd or as a Windows service).
- Inbound network access (the agent is pull-based — only outbound
  calls).
- A static IP for the host.

---

## Choosing values (the judgment calls)

These are the decisions that don't have a single right answer. Make
them honestly — the dispatcher's behaviour rests on them.

### `MAX_POWER_KW` — the most important field

The dispatcher uses this to size the work it sends. **Pick the
sustained power draw, not the peak**, and **don't include power the
building isn't willing to redirect to compute**.

Rough guidance:
- A small office PC under load: 0.2 – 0.4 kW.
- A workstation with a discrete GPU under sustained load: 0.6 – 1.5 kW.
- A small rack server: 0.5 – 1.5 kW.
- A multi-GPU box at full tilt: 2 – 5 kW.

**Overstating it** → the dispatcher routes tasks that finish later
than estimated → forecast windows slip → recalls → wasted work.
**Understating it** → the building receives less heat than it could.
**Err small if uncertain** — you can always bump it later.

### `NODE_TYPE`

`community_centre | pool | arena | office | other`. Purely a UI
label — no dispatch effect. Pick the one closest to what the
building actually is.

### `BMS_SOURCE` — `file` vs `http`

- **`file`** if the BMS (or a script you control) can write JSON to
  disk on the agent's host. This is the simplest integration: the
  BMS owns the file's contents; the agent just reads it. Recommended
  when in doubt.
- **`http`** if the BMS exposes a REST endpoint that returns the
  JSON shape directly, OR if a separate sidecar service (yours)
  translates BACnet/Modbus to JSON over HTTP. The agent will GET it
  every demand tick (~15s).

You can switch later by editing the env file and restarting; the
node's identity is independent of the BMS source.

### `AGENT_CONCURRENCY`

Default `1` is right for almost every deployment. Raise it only if:
- the host has spare cores (the agent always runs tasks in
  subprocesses, so concurrency 4 means up to 4 parallel CPU-bound
  tasks), AND
- the building's heat demand is consistently high enough to justify
  several simultaneous tasks producing heat at once.

If you are not sure, leave it at 1.

### `AGENT_STATE_FILE` permissions

The state file contains the node's bearer token. Treat it like a
password file: readable only by the agent user (or `LocalSystem` on
Windows), not world-readable. The systemd / Windows install steps
set this up for you; if you put the file somewhere else, replicate
those permissions.

---

## Install

Pick your platform.

### Linux (systemd)

Full walkthrough: [agent/deploy/systemd/README.md](../agent/deploy/systemd/README.md).

Summary:

```sh
# 1. Service user.
sudo useradd --system --home /var/lib/hadcd-agent --create-home \
  --shell /usr/sbin/nologin hadcd-agent

# 2. Copy agent code + create venv.
sudo mkdir -p /opt/hadcd-agent
sudo cp -r <checkout>/agent /opt/hadcd-agent/
sudo cp -r <checkout>/hadcd_workloads /opt/hadcd-agent/
sudo chown -R root:hadcd-agent /opt/hadcd-agent
sudo python3.11 -m venv /opt/hadcd-agent/.venv
sudo /opt/hadcd-agent/.venv/bin/pip install -r /opt/hadcd-agent/agent/requirements.txt

# 3. Env file.
sudo mkdir -p /etc/hadcd-agent
sudo cp /opt/hadcd-agent/agent/config.env.example /etc/hadcd-agent/agent.env
sudo chown root:hadcd-agent /etc/hadcd-agent/agent.env
sudo chmod 0640 /etc/hadcd-agent/agent.env
sudo $EDITOR /etc/hadcd-agent/agent.env

# 4. (Recommended) sanity-run in the foreground before installing
#    as a service. Use the agent user so file permissions match what
#    the service will see.
sudo -u hadcd-agent \
  env $(grep -v '^#' /etc/hadcd-agent/agent.env | xargs) \
  /opt/hadcd-agent/.venv/bin/python -m agent run
#  Watch for "enrolled as node <UUID>" + "POST /api/nodes/.../heartbeat 200".
#  Ctrl-C to stop; the identity is persisted to /var/lib/hadcd-agent/state.json.

# 5. Install the service.
sudo cp /opt/hadcd-agent/agent/deploy/systemd/hadcd-agent.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hadcd-agent
sudo systemctl status hadcd-agent

# Tail logs.
journalctl -u hadcd-agent -f
```

### Windows (NSSM)

Full walkthrough: [agent/deploy/windows/README.md](../agent/deploy/windows/README.md).

Summary, run from an **elevated** PowerShell:

```powershell
# 1. Copy agent code + create venv.
$dst = "$env:ProgramFiles\hadcd-agent"
New-Item -ItemType Directory -Force $dst | Out-Null
Copy-Item -Recurse <checkout>\agent           $dst\
Copy-Item -Recurse <checkout>\hadcd_workloads $dst\
python -m venv "$dst\.venv"
& "$dst\.venv\Scripts\pip" install -r "$dst\agent\requirements.txt"

# 2. Env file.
New-Item -ItemType Directory -Force "$env:ProgramData\hadcd-agent" | Out-Null
Copy-Item "$dst\agent\config.env.example" "$env:ProgramData\hadcd-agent\agent.env"
notepad   "$env:ProgramData\hadcd-agent\agent.env"
# In the env file, set AGENT_STATE_FILE and BMS_FILE to Windows paths,
# e.g. C:\ProgramData\hadcd-agent\state.json
icacls "$env:ProgramData\hadcd-agent\agent.env" `
  /inheritance:r /grant:r "SYSTEM:R" "Administrators:F"

# 3. (Recommended) sanity-run in the foreground.
& "$dst\.venv\Scripts\python.exe" -m agent run
# Watch for "enrolled as node <UUID>"; Ctrl-C to stop.

# 4. Install as a Windows service via NSSM.
& "$dst\agent\deploy\windows\install-service.ps1"

# Tail logs.
Get-Content -Wait -Tail 50 "$env:ProgramData\hadcd-agent\logs\agent.out.log"
```

The script auto-downloads NSSM 2.24 if not on PATH.

---

## First-run sanity check (what you should see)

In the logs, a successful first start looks like:

```
INFO [hadcd.agent] enrolled as node bb2963f1-2d66-416f-b092-34c8f9db4068
                  (state persisted to /var/lib/hadcd-agent/state.json)
INFO [httpx] HTTP Request: POST .../heartbeat "HTTP/1.1 200 OK"
INFO [httpx] HTTP Request: POST .../heat_demand "HTTP/1.1 201 Created"
INFO [httpx] HTTP Request: GET  .../api/work?node_id=... "HTTP/1.1 200 OK"
```

If a task arrives, you'll see:

```
INFO [hadcd.agent] running task <UUID> (type=cpu_burn)
INFO [hadcd.agent] task <UUID> finished: ok (4.15s)
```

Common things that go wrong on a first install and what they mean:

| Log line | Means | Fix |
|----------|-------|-----|
| `BMS config error: BMS_SOURCE=http but BMS_HTTP_URL is empty` | The env file selects http mode but no URL was set. | Set `BMS_HTTP_URL` or change `BMS_SOURCE=file`. |
| `error: ... no enrollment token` | First start, but `ENROLLMENT_TOKENS` is blank. | Ask the central operator for one; set it in the env file. |
| `POST /api/nodes/register HTTP/1.1 401` | The enrollment token is wrong (or has been rotated). | Confirm the value with the central operator. |
| `POST /api/nodes/.../heartbeat HTTP/1.1 401` | The state file's bearer is no longer valid (node was decommissioned on the server). | Delete the state file and let it re-enroll. |
| `BMS file ... not present — posting zero demand` | The agent is fine; the BMS isn't writing yet. | Get the BMS writing the file, or switch to http. |
| `BMS HTTP source ... unavailable` | The HTTP BMS is unreachable or returning errors. | Confirm `BMS_HTTP_URL` from the agent host. |

After a `node was decommissioned on the server` style fix, the next
start prints `enrolled as node <new-UUID>` and the central operator
will see a new row.

---

## Upgrading the agent

```sh
# Linux
sudo systemctl stop hadcd-agent
# replace /opt/hadcd-agent/agent and /opt/hadcd-agent/hadcd_workloads
sudo /opt/hadcd-agent/.venv/bin/pip install -r /opt/hadcd-agent/agent/requirements.txt
sudo systemctl start hadcd-agent
```

```powershell
# Windows
Stop-Service hadcd-agent
# replace agent\ and hadcd_workloads\ under %ProgramFiles%\hadcd-agent
& "$env:ProgramFiles\hadcd-agent\.venv\Scripts\pip" install -r `
  "$env:ProgramFiles\hadcd-agent\agent\requirements.txt"
Start-Service hadcd-agent
```

The node's identity persists across upgrades.

---

## Decommissioning

If the building is leaving the system permanently:

```sh
# Linux
sudo systemctl disable --now hadcd-agent
sudo rm /etc/systemd/system/hadcd-agent.service
sudo rm -rf /etc/hadcd-agent /var/lib/hadcd-agent /opt/hadcd-agent
sudo userdel hadcd-agent
```

```powershell
# Windows
Stop-Service hadcd-agent
nssm remove hadcd-agent confirm
Remove-Item -Recurse "$env:ProgramFiles\hadcd-agent"
Remove-Item -Recurse "$env:ProgramData\hadcd-agent"
```

Then tell the central operator the node is gone so they can mark the
row decommissioned on their side. (Without this, the node will sit
in their UI as "offline" forever — harmless, just untidy.)

---

## <a name="for-the-central-operator"></a>For the central operator

Provisioning a new building means:

1. **Hand the building operator your enrollment token.** This lives
   in the backend's `.env` under `ENROLLMENT_TOKENS` (comma-separated
   list). You can generate fresh values with:

   ```sh
   python -c "import secrets; print('hadcd_enroll_' + secrets.token_hex(16))"
   ```

   Sharing one enrollment token across all buildings is fine — it
   only authorises *registration*, not ongoing impersonation. Each
   building enrolls once and then has its own per-node bearer.

2. **Hand them your API URL** (`HADCD_API`). Typically the TLS
   endpoint of the reverse proxy in front of the backend.

3. After they install and start the agent, the node appears on your
   **Nodes** page with `status: online`. Confirm its `MAX_POWER_KW`
   and `NODE_TYPE` are sensible (you can edit operator notes from
   there, but the declared capabilities are fixed at registration).

To rotate the enrollment token across the whole fleet:

1. Prepend a new value to `ENROLLMENT_TOKENS` in the backend `.env`
   (now both old and new are accepted).
2. Restart the backend.
3. Hand the new value to any future-enrolling buildings.
4. Once all expected new enrollments are done, drop the old value
   from `ENROLLMENT_TOKENS` and restart the backend again.

Existing enrolled nodes are *not* affected — they use their own
per-node bearer, not the enrollment token.
