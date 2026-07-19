# Sunshine setup (interactive use + session detection)

Sunshine is HADCD's recommended companion on every heater node. This
walkthrough covers installing it, pairing the Moonlight client on your
laptop, and wiring it into the HADCD agent so background work pauses
while you're using the machine.

## Why HADCD wants Sunshine

The space-heater pilot has two faces:

- **You using the machine** — gaming, running a local AI app, editing
  video, browsing. You do this from your laptop by streaming the
  heater's desktop with **Moonlight** (the client) talking to
  **Sunshine** (the host).
- **The machine working in the background** — when you're *not* using
  it, fill-tier work (crypto mining, synthetic heat-fill) consumes the
  heat demand so the room still gets warm and the hardware isn't idle.

Sunshine is what lets HADCD tell these apart. The agent polls
Sunshine's `GET /api/connections` endpoint each heartbeat; when a
Moonlight client is connected, the node reports `session_active=true`
and the dispatcher pauses fill tiers. The moment you disconnect, fill
work resumes (if heat is still demanded).

Without Sunshine, the agent still runs — but it has no way to know
you're using the machine, so mining/heat-fill would compete with you
for the GPU. The agent logs a loud advisory at startup when no session
source is configured.

> Note: Sunshine session detection covers the *remote-desktop* case
> (you streaming in via Moonlight). A future slice adds `nvidia-smi`
> based detection for apps you launch *directly* on the heater without
> Moonlight. For the pilot, run interactive work through Moonlight and
> Sunshine covers it.

## 1. Install Sunshine

Sunshine ships native installers — HADCD does **not** bundle or
containerise it (it needs direct display + GPU access for hardware
encoding, which a container can't cleanly provide).

- **Windows:** download the latest installer from
  <https://github.com/LizardByte/Sunshine/releases> and run it. The
  HADCD Windows agent installer (`install-service.ps1`) checks for
  Sunshine and prompts you if it's missing.
- **Linux (Debian/Ubuntu):** grab the latest `.deb` from the releases
  page and `sudo apt install ./sunshine-*.deb`.
- **Other:** see the
  [Sunshine docs](https://docs.lizardbyte.dev/projects/sunshine/).

Sunshine needs an NVIDIA (NVENC), AMD (AMF), or Intel (QSV) GPU for
hardware encoding. The pilot assumes NVIDIA.

## 2. Set the Sunshine admin credentials

Open <https://localhost:47990> on the heater (you'll get a self-signed
certificate warning — that's expected for a localhost service; proceed).
On first run Sunshine asks you to create an **admin username and
password**.

**Remember these** — they double as the API credentials the HADCD
agent uses to poll session state. There is no separate API key today;
Sunshine accepts the admin credentials as HTTP Basic auth on its API.

## 3. Pair Moonlight (your laptop)

1. Install Moonlight on your laptop / tablet / phone from
   <https://moonlight-stream.org/>.
2. On the heater's Sunshine web UI, note the PIN-pairing option, or
   start pairing from Moonlight — it'll show a PIN.
3. Enter the PIN in Sunshine's web UI under **PIN** to authorise the
   client.
4. Moonlight should now list the heater. Connect to confirm you get
   the desktop.

## 4. Wire Sunshine into the HADCD agent

In the agent's env file (`/etc/hadcd-agent/agent.env` on Linux,
`%ProgramData%\hadcd-agent\agent.env` on Windows):

```sh
SESSION_SOURCE=sunshine
SUNSHINE_URL=https://localhost:47990
SUNSHINE_USERNAME=sunshine          # whatever you set in step 2
SUNSHINE_PASSWORD=your-admin-password
```

Restart the agent. On startup it no longer logs the "no session
detector" advisory. From now on:

- Connect with Moonlight → within one heartbeat (~10s) the node
  reports `session_active=true` → fill tiers pause.
- Disconnect → `session_active=false` → fill tiers resume if heat is
  still demanded.

## 5. Verify

With the agent running and Sunshine configured:

```sh
# On the central server, watch the node's session_active flag flip
# as you connect/disconnect Moonlight:
curl -H "Authorization: Bearer $ADMIN" \
  https://your-hadcd-server/api/nodes/<node-id> | jq .session_active
```

It should read `true` while a Moonlight session is live and `false`
otherwise.

## Troubleshooting

- **Agent logs "Sunshine connections poll failed".** Check
  `SUNSHINE_URL` is reachable from the agent process (same host,
  loopback is fine), and that `SUNSHINE_USERNAME` / `SUNSHINE_PASSWORD`
  match what you set in Sunshine's web UI. The agent holds the last
  known session state on a transient failure rather than flapping the
  gate.
- **Self-signed cert errors.** Expected — the agent's Sunshine adapter
  does not verify TLS for the localhost call, so this shouldn't block
  it. If you put Sunshine behind a real cert, the adapter still works
  (it just doesn't require one).
- **`session_active` never goes true.** Confirm a Moonlight client is
  actually *streaming* (not just paired). The `/api/connections`
  endpoint reflects live connections, not pairings.
