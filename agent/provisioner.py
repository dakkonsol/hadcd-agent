"""Phase 16a — HADCD node configuration provisioner.

Serves a phone-friendly setup wizard on port 8080 that guides the operator
through Home Assistant connection and HADCD dispatcher registration.

Run by ``hadcd-provision.service`` at boot time, AFTER the WiFi provisioner
(which handles network setup).  Exits immediately if the node is already
fully configured.

Phone flow
----------
1. Node boots.  Both ``hadcd-ha.service`` (HA Container on port 8123) and
   ``hadcd-provision.service`` (this, on port 8080) start automatically.
2. Operator opens HA on their phone:  http://hadcd-node.local:8123
   - Creates HA account (~60 s, first time only).
   - Adds Ecobee integration (follow steps; HA shows a PIN, enter it at
     ecobee.com/consumerportal → My Apps → Add Application).
   - Creates a long-lived access token (Profile → Long-Lived Access Tokens).
3. Operator opens the HADCD provisioner: http://hadcd-node.local:8080
   - Pastes the HA token → clicks "Load thermostats" → picks the room's
     thermostat from the dropdown.
   - Enters the HADCD dispatcher URL, enrollment token, and node name.
   - Taps Save.
4. The provisioner writes /etc/hadcd-agent/agent.env and restarts hadcd-agent.
   The node appears in the dashboard within ~30 s.
5. The provisioner exits (the port is no longer exposed after first setup).

Security
--------
The provisioner runs as root (needed to write agent.env and restart services).
It binds to 0.0.0.0:8080 so it is reachable from any interface.  The service
is disabled at OS level once agent.env has all required fields, so the port is
not open during normal operation.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

logger = logging.getLogger("hadcd.provisioner")

CONF_DIR = Path("/etc/hadcd-agent")
ENV_FILE = CONF_DIR / "agent.env"
ENV_EXAMPLE = Path("/opt/hadcd-agent/agent/config.env.example")
HA_URL = "http://localhost:8123"
PROVISION_PORT = 8080
PROVISION_TIMEOUT = 900  # 15 min
REQUIRED_FIELDS = ["HADCD_API", "ENROLLMENT_TOKENS", "NODE_NAME"]

_done = threading.Event()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def provision() -> int:
    """Run the provisioner.  Returns 0 on success."""
    if _is_configured():
        logger.info("Node already configured — provisioner exiting.")
        return 0

    local_ip = _get_local_ip() or "hadcd-node.local"
    logger.info(
        "Node not yet configured.  Open http://%s:%d on your phone.",
        local_ip, PROVISION_PORT,
    )
    print()
    print("=" * 62)
    print("  HADCD NODE SETUP WIZARD")
    print("=" * 62)
    print(f"  Open on your phone:  http://{local_ip}:{PROVISION_PORT}")
    print()
    print("  Or use the mDNS name:  http://hadcd-node.local:8080")
    print("=" * 62)
    print()

    _serve(port=PROVISION_PORT)
    return 0


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _is_configured() -> bool:
    """True when agent.env has all required fields filled in."""
    if not ENV_FILE.exists():
        return False
    env = _read_env(ENV_FILE)
    if not all(env.get(k, "").strip() for k in REQUIRED_FIELDS):
        return False
    bms = env.get("BMS_SOURCE", "file").strip()
    if bms == "homeassistant":
        return bool(env.get("HA_TOKEN", "").strip() and
                    env.get("HA_ENTITY_ID", "").strip())
    return True


def _read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def _write_env(updates: dict[str, str]) -> None:
    """Merge `updates` into agent.env (creating from template if absent)."""
    CONF_DIR.mkdir(mode=0o750, parents=True, exist_ok=True)

    # Start from the existing file or the template.
    source = ENV_FILE if ENV_FILE.exists() else ENV_EXAMPLE
    lines = source.read_text().splitlines() if source.exists() else []

    # Overwrite lines where the key is in `updates`.
    updated: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                updated.add(key)
                continue
        new_lines.append(line)

    # Append keys not already in the file.
    remainder = {k: v for k, v in updates.items() if k not in updated}
    if remainder:
        new_lines.append("")
        new_lines.append("# --- set by HADCD provisioner ---")
        for k, v in remainder.items():
            new_lines.append(f"{k}={v}")

    tmp = ENV_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(new_lines) + "\n")
    tmp.chmod(0o640)
    tmp.rename(ENV_FILE)

    # Chown to root:hadcd-agent so the service user can read it.
    try:
        import grp
        gid = grp.getgrnam("hadcd-agent").gr_gid
        os.chown(ENV_FILE, 0, gid)
    except (KeyError, PermissionError):
        pass


def _get_local_ip() -> str | None:
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# HA REST helpers
# ---------------------------------------------------------------------------


def _ha_is_running() -> bool:
    try:
        urllib.request.urlopen(f"{HA_URL}/", timeout=3)
        return True
    except Exception:
        return False


def _ha_climate_entities(token: str) -> list[dict]:
    try:
        req = urllib.request.Request(
            f"{HA_URL}/api/states",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            states = json.loads(resp.read())
        return [
            {
                "entity_id": s["entity_id"],
                "name": s.get("attributes", {}).get("friendly_name") or s["entity_id"],
            }
            for s in states
            if s.get("entity_id", "").startswith("climate.")
        ]
    except Exception as exc:
        logger.debug("HA entity query failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress default access log

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        if path == "/ha-status":
            self._json({"running": _ha_is_running()})
        elif path == "/ha-entities":
            token = qs.get("token", [""])[0]
            self._json(_ha_climate_entities(token) if token else [])
        else:
            env = _read_env(ENV_FILE) if ENV_FILE.exists() else {}
            self._html(_render_page(env))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

        if path == "/save":
            data = urllib.parse.parse_qs(raw)

            def g(k):
                return (data.get(k, [""])[0] or "").strip()

            updates: dict[str, str] = {}
            if g("hadcd_api"):
                updates["HADCD_API"] = g("hadcd_api")
            if g("enrollment_tokens"):
                updates["ENROLLMENT_TOKENS"] = g("enrollment_tokens")
            if g("node_name"):
                updates["NODE_NAME"] = g("node_name")
            if g("max_power_kw"):
                updates["MAX_POWER_KW"] = g("max_power_kw")
            if g("node_type"):
                updates["NODE_TYPE"] = g("node_type")
            if g("ha_token"):
                updates["BMS_SOURCE"] = "homeassistant"
                updates["HA_TOKEN"] = g("ha_token")
            if g("ha_entity_id"):
                updates["HA_ENTITY_ID"] = g("ha_entity_id")
            if g("ha_demand_kw"):
                updates["HA_DEMAND_WHEN_HEATING_KW"] = g("ha_demand_kw")
            if g("zone_name"):
                updates["ZONE_NAME"] = g("zone_name")
            if g("node_latitude") and g("node_longitude"):
                updates["NODE_LATITUDE"] = g("node_latitude")
                updates["NODE_LONGITUDE"] = g("node_longitude")
            # Step 5 — mining wallets (optional; left blank = mining disabled)
            if g("nicehash_wallet"):
                updates["NICEHASH_WALLET"] = g("nicehash_wallet")
                # Enable T-Rex path only if the binary was pre-installed by the ISO.
                updates.setdefault("NICEHASH_TREX_PATH", "/opt/trex/t-rex")
            if g("xmr_wallet"):
                updates["XMR_WALLET_ADDRESS"] = g("xmr_wallet")
                updates.setdefault("XMRIG_PATH", "/opt/xmrig/xmrig")
            # Step 6 — Vast.AI (optional; machine ID is auto-discovered on first start)
            if g("vastai_api_key"):
                updates["VASTAI_API_KEY"] = g("vastai_api_key")

            try:
                _write_env(updates)
            except Exception as exc:
                self._html(_render_error(str(exc)))
                return

            # Restart hadcd-agent with the new config.
            subprocess.Popen(
                ["systemctl", "restart", "hadcd-agent"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._html(_render_success())
            _done.set()
        else:
            self._send(404, "text/plain", b"not found")

    def _html(self, body: str) -> None:
        self._send(200, "text/html; charset=utf-8", body.encode())

    def _json(self, obj) -> None:
        self._send(200, "application/json", json.dumps(obj).encode())

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _serve(port: int) -> None:
    server = HTTPServer(("0.0.0.0", port), _Handler)
    server.timeout = 2.0
    deadline = time.monotonic() + PROVISION_TIMEOUT
    while not _done.is_set() and time.monotonic() < deadline:
        server.handle_request()
    server.server_close()


# ---------------------------------------------------------------------------
# HTML pages (inline CSS, no external dependencies — works offline)
# ---------------------------------------------------------------------------

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#0f172a;color:#e2e8f0;min-height:100vh;padding:1rem}
.card{background:#1e293b;border-radius:12px;padding:1.25rem;margin-bottom:1rem}
h1{font-size:1.2rem;font-weight:700;color:#38bdf8;margin-bottom:.2rem}
h2{font-size:.9rem;font-weight:600;color:#94a3b8;margin-bottom:.75rem}
label{display:block;font-size:.78rem;color:#94a3b8;margin:.65rem 0 .2rem}
input,select{width:100%;padding:.55rem .7rem;border-radius:7px;
  border:1px solid #334155;background:#0f172a;color:#e2e8f0;font-size:.95rem}
input:focus,select:focus{border-color:#38bdf8;outline:none}
.req{color:#f87171}
.btn{display:block;width:100%;padding:.7rem;border-radius:8px;
  background:#0ea5e9;color:#fff;font-size:.95rem;font-weight:600;
  border:none;margin-top:1rem;cursor:pointer}
.btn:active{background:#38bdf8}
.btn-sm{background:#334155;margin-top:.4rem;font-size:.85rem;padding:.5rem}
.ha-row{display:flex;align-items:center;gap:.4rem;font-size:.8rem;margin-bottom:.5rem}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.g{background:#22c55e}.r{background:#ef4444}
.step{font-size:.82rem;color:#94a3b8;margin:.3rem 0}
.step a{color:#38bdf8}
.warn{background:#1c1917;border:1px solid #78350f;border-radius:7px;
  padding:.6rem;font-size:.82rem;color:#fcd34d;margin-bottom:.75rem}
"""

_JS = """
async function loadEntities() {
  const tok = document.getElementById('ha_token').value.trim();
  if (!tok) return;
  const sel = document.getElementById('ha_entity_id');
  sel.innerHTML = '<option>Loading…</option>';
  try {
    const r = await fetch('/ha-entities?token=' + encodeURIComponent(tok));
    const data = await r.json();
    if (!data.length) {
      sel.innerHTML = '<option>No climate entities found — add your thermostat integration in HA first</option>';
    } else {
      sel.innerHTML = data.map(e =>
        `<option value="${e.entity_id}">${e.name}</option>`).join('');
    }
  } catch(e) {
    sel.innerHTML = '<option>Cannot reach HA — check token</option>';
  }
}
async function checkHA() {
  try {
    const r = await fetch('/ha-status');
    const d = await r.json();
    document.getElementById('ha-dot').className = 'dot ' + (d.running ? 'g' : 'r');
    document.getElementById('ha-txt').textContent =
      d.running ? 'Home Assistant is running' : 'HA not running yet — wait ~30 s after boot';
  } catch(e) {}
}
checkHA();
setInterval(checkHA, 6000);
window.addEventListener('load', () => {
  if (document.getElementById('ha_token').value.trim()) loadEntities();
});
"""


def _render_page(env: dict) -> str:
    nt = env.get("NODE_TYPE", "office")

    def sel(v):
        return "selected" if nt == v else ""

    return textwrap.dedent(f"""
    <!DOCTYPE html><html lang="en"><head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>HADCD Node Setup</title>
    <style>{_CSS}</style></head><body>

    <div class="card" style="border:1px solid #1e40af">
      <h1>HADCD Node Setup</h1>
      <h2>Follow the steps — no terminal or SSH needed.</h2>
    </div>

    <div class="card">
      <h2>Step 1 — Home Assistant</h2>
      <div class="ha-row">
        <span class="dot r" id="ha-dot"></span>
        <span id="ha-txt">Checking HA…</span>
      </div>
      <p class="step">Open
        <a href="http://homeassistant.local:8123" target="_blank">http://homeassistant.local:8123</a>
        in another tab.</p>
      <p class="step">1. Create your HA account (first time only, ~60 s).</p>
      <p class="step">2. Settings → Integrations → Add Integration → search for your
         thermostat brand (e.g. <b>Tuya</b>, <b>Moes</b>, <b>Ecobee</b>, <b>Nest</b>)
         and follow the pairing steps shown by HA.</p>
      <p class="step">3. Profile (your name, bottom-left) →
         Long-Lived Access Tokens → Create Token → copy it.</p>
    </div>

    <form method="post" action="/save">
      <div class="card">
        <h2>Step 2 — Connect to Home Assistant</h2>
        <label>HA long-lived token <span class="req">*</span></label>
        <input type="password" name="ha_token" id="ha_token"
               value="{env.get('HA_TOKEN','')}" placeholder="eyJ0eXAi…"
               oninput="loadEntities()">
        <button type="button" class="btn btn-sm" onclick="loadEntities()">↻ Load thermostats</button>
        <label>Thermostat <span class="req">*</span></label>
        <select name="ha_entity_id" id="ha_entity_id">
          <option value="{env.get('HA_ENTITY_ID','')}">
            {env.get('HA_ENTITY_ID') or '— paste token above, then load —'}
          </option>
        </select>
        <label>Heat demand when thermostat is calling (kW)</label>
        <input type="number" name="ha_demand_kw"
               value="{env.get('HA_DEMAND_WHEN_HEATING_KW', env.get('MAX_POWER_KW','1.0'))}"
               step="0.1" min="0.1">
      </div>

      <div class="card">
        <h2>Step 3 — HADCD dispatcher</h2>
        <p style="font-size:.82rem;color:#94a3b8;margin-bottom:.75rem">
          The dispatcher is the central server (running on your laptop or a home server).
          This node must be able to reach it over the network — use a
          <strong style="color:#e2e8f0">Tailscale IP</strong> (e.g. <code>http://100.x.x.x:8000</code>)
          for reliable connectivity across networks, or a LAN IP if both machines are on the same Wi-Fi.
          If you haven't run <code>tailscale up</code> on this node yet, do that first — the agent
          won't be able to connect until Tailscale is authenticated.
        </p>
        <label>Dispatcher URL <span class="req">*</span></label>
        <input name="hadcd_api" value="{env.get('HADCD_API','')}"
               placeholder="http://100.x.x.x:8000  (Tailscale IP recommended)">
        <label>Enrollment token <span class="req">*</span>
          <span style="color:#475569;font-weight:400"> — from the HADCD Dispatcher app or your .env file (ENROLLMENT_TOKENS)</span>
        </label>
        <input type="password" name="enrollment_tokens"
               value="{env.get('ENROLLMENT_TOKENS','')}"
               placeholder="hadcd_enroll_…">
      </div>

      <div class="card">
        <h2>Step 4 — This node</h2>
        <label>Node name <span class="req">*</span></label>
        <input name="node_name" value="{env.get('NODE_NAME','')}"
               placeholder="Living Room PC">
        <label>Type</label>
        <select name="node_type">
          <option value="office" {sel('office')}>Office / Home</option>
          <option value="community_centre" {sel('community_centre')}>Community Centre</option>
          <option value="arena" {sel('arena')}>Arena</option>
          <option value="pool" {sel('pool')}>Pool</option>
        </select>
        <label>Max sustained power (kW)</label>
        <input type="number" name="max_power_kw"
               value="{env.get('MAX_POWER_KW','1.0')}" step="0.1" min="0.1">
        <label>Zone name
          <span style="color:#475569;font-weight:400"> — optional, for multi-node buildings</span>
        </label>
        <input name="zone_name" value="{env.get('ZONE_NAME','')}"
               placeholder="Living Room">
        <label>Latitude
          <span style="color:#475569;font-weight:400"> — optional, for Vast.AI weather scheduling</span>
        </label>
        <input name="node_latitude" value="{env.get('NODE_LATITUDE','')}" placeholder="45.42">
        <label>Longitude</label>
        <input name="node_longitude" value="{env.get('NODE_LONGITUDE','')}" placeholder="-75.69">
      </div>

      <div class="card">
        <h2>Step 5 — Mining wallets <span style="color:#475569;font-weight:400">(optional)</span></h2>
        <div class="warn">Leave blank to disable mining on this node. Wallets are stored only in
        agent.env and never sent to the dispatcher.</div>

        <label>NiceHash BTC wallet address
          <span style="color:#475569;font-weight:400"> — GPU mining, pays in BTC</span>
        </label>
        <p class="step">1. Create a free account at
          <a href="https://www.nicehash.com" target="_blank">nicehash.com</a>.</p>
        <p class="step">2. Enable 2FA (Account → Security).</p>
        <p class="step">3. Copy your address: Dashboard → Wallet → BTC → Copy address.</p>
        <input name="nicehash_wallet" value="{env.get('NICEHASH_WALLET','')}"
               placeholder="bc1q… or 1A1z…" autocomplete="off"
               style="font-family:monospace;font-size:.85rem">

        <label>Monero (XMR) wallet address
          <span style="color:#475569;font-weight:400"> — CPU mining via P2Pool, pays in XMR</span>
        </label>
        <p class="step">Get a self-custody wallet:
          <a href="https://cakewallet.com" target="_blank">Cake Wallet</a> (iOS/Android) or
          <a href="https://featherwallet.org" target="_blank">Feather Wallet</a> (desktop).
          Your address starts with <b>4</b> and is ~95 characters long.</p>
        <p class="step" style="color:#f87171">Do <b>not</b> use an exchange address
          (Binance, Kraken, etc.) — exchanges reject small mining deposits.</p>
        <input name="xmr_wallet" value="{env.get('XMR_WALLET_ADDRESS','')}"
               placeholder="4…" autocomplete="off"
               style="font-family:monospace;font-size:.85rem">
      </div>

      <div class="card">
        <h2>Step 6 — Vast.AI <span style="color:#475569;font-weight:400">(optional)</span></h2>
        <div class="warn">Leave blank to skip GPU rental. Your API key is stored only in
        agent.env and never sent to the dispatcher. It grants full control of your Vast.AI
        account — treat it like a password.</div>

        <label>Vast.AI API key
          <span style="color:#475569;font-weight:400"> — enables GPU rental on warm days</span>
        </label>
        <p class="step">1. Sign in at
          <a href="https://vast.ai" target="_blank">vast.ai</a>.</p>
        <p class="step">2. Account (top-right) → API Keys → Create API Key → copy it.</p>
        <p class="step">3. Paste it below. Your machine ID is discovered automatically
          on first start — you don't need to enter it here.</p>
        <input type="password" name="vastai_api_key"
               value="{env.get('VASTAI_API_KEY','')}"
               placeholder="••••••••••••••••••••••••" autocomplete="off">
      </div>

      <button type="submit" class="btn">💾 Save &amp; Start Node</button>
    </form>

    <script>{_JS}</script>
    </body></html>
    """).strip()


def _render_error(message: str) -> str:
    # Translate common OS errors into plain English before showing them.
    friendly = message
    if "Permission denied" in message:
        friendly = (
            "Permission denied — the wizard could not write the config file. "
            "Make sure the hadcd-agent service has write access to /etc/hadcd-agent/. "
            "Try restarting the node and opening this page again."
        )
    elif "No such file or directory" in message:
        friendly = (
            "Config directory not found — /etc/hadcd-agent/ does not exist yet. "
            "The HADCD first-boot service may still be running. "
            "Wait 2–3 minutes and refresh this page."
        )
    elif "Connection refused" in message or "Network" in message:
        friendly = (
            "Could not reach Home Assistant or the HADCD dispatcher. "
            "Check that both are running and that the URLs you entered are correct."
        )
    return textwrap.dedent(f"""
    <!DOCTYPE html><html lang="en"><head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>HADCD Setup — Error</title>
    <style>{_CSS}</style></head><body>
    <div class="card">
      <h1>Something went wrong</h1>
      <div class="warn">{friendly}</div>
      <details style="margin-top:.75rem;font-size:.75rem;color:#64748b">
        <summary>Technical details</summary>
        <pre style="margin-top:.5rem;white-space:pre-wrap;word-break:break-all">{message}</pre>
      </details>
      <a href="/"><button class="btn">← Try again</button></a>
    </div></body></html>
    """).strip()


def _render_success() -> str:
    return textwrap.dedent("""
    <!DOCTYPE html><html lang="en"><head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>HADCD Setup — Done</title>
    <style>*{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,sans-serif;background:#0f172a;color:#e2e8f0;
         min-height:100vh;padding:1.5rem;text-align:center}
    .card{background:#1e293b;border-radius:12px;padding:1.5rem;margin-bottom:1rem}
    h1{font-size:1.4rem;font-weight:700;color:#22c55e;margin-bottom:.5rem}
    h2{font-size:.9rem;color:#94a3b8;margin-bottom:1rem}
    .step{font-size:.85rem;color:#94a3b8;margin:.4rem 0}
    </style></head><body>
    <div class="card">
      <div style="font-size:2.5rem;margin-bottom:.5rem">✅</div>
      <h1>Node configured!</h1>
      <h2>The agent is starting — check the dashboard in ~30 seconds.</h2>
    </div>
    <div class="card" style="text-align:left">
      <p class="step" style="color:#e2e8f0;font-weight:600;margin-bottom:.5rem">What happens next:</p>
      <p class="step">• Node enrolls with the dispatcher and starts heartbeating.</p>
      <p class="step">• When the thermostat calls for heat, GPU + CPU mining starts.</p>
      <p class="step">• On warm days the GPU lists on Vast.AI for rental income.</p>
      <p class="step">• Works autonomously even if the dispatcher is offline.</p>
    </div>
    </body></html>
    """).strip()
