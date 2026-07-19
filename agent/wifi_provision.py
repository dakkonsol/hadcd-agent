"""WiFi captive-portal provisioner for HADCD nodes.

Usage (via __main__.py):

    python -m agent wifi-provision

Lifecycle
---------
1.  Checks if provisioning is needed.  Skips (< 1 s) when:
      * An ethernet interface is already connected, OR
      * NetworkManager already has a WiFi connection profile, OR
      * No WiFi hardware is detected on this machine.
2.  Scans for available WiFi networks (saved so the hotspot can re-use
    the radio in AP mode).
3.  Creates a local WPA2 hotspot:
        SSID:     HADCD-Setup
        Password: hadcdsetup
    NetworkManager assigns 10.42.0.1 to the node and runs a DHCP server
    so phones get an address automatically.
4.  Serves a minimal HTML form on http://10.42.0.1:80/
5.  Operator joins "HADCD-Setup" from their phone, opens a browser to
    http://10.42.0.1, picks their SSID, enters the password, taps Connect.
6.  Handler connects the node to the target WiFi, tears down the hotspot.
7.  Writes /var/lib/hadcd-agent/wifi-provisioned so every subsequent boot
    exits in under a second without doing anything.

The phone is only needed once.
"""

from __future__ import annotations

import html
import logging
import subprocess
import threading
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hadcd.wifi_provision")

# Written after successful provisioning; presence makes this module a no-op
# on all subsequent boots.
PROVISIONED_FLAG = Path("/var/lib/hadcd-agent/wifi-provisioned")

# Hotspot identity (well-known so the operator can connect without a display).
HOTSPOT_SSID = "HADCD-Setup"
HOTSPOT_PASSWORD = "hadcdsetup"

# Port for the provisioning web UI.  80 requires root; the service runs as
# root since it also needs nmcli for AP creation.
HTTP_PORT = 80

# Hotspot IP assigned by NetworkManager when ipv4.method=shared.
HOTSPOT_IP = "10.42.0.1"

# How long to wait for the operator to complete provisioning before giving up.
PROVISION_TIMEOUT_SEC = 600  # 10 minutes

# nmcli connection name used for the temporary hotspot profile.
HOTSPOT_CONN_NAME = "HADCD-Provision"


# ---------------------------------------------------------------------------
# nmcli helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a shell command; log stderr on failure."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        logger.debug("cmd %s → rc=%d stderr=%s", cmd, result.returncode, result.stderr.strip())
    return result


def _nmcli_available() -> bool:
    return _run(["which", "nmcli"]).returncode == 0


def find_wifi_interface() -> Optional[str]:
    """Return the name of the first WiFi interface visible to nmcli, or None."""
    r = _run(["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"])
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1].strip().lower() in ("wifi", "802-11-wireless"):
            return parts[0].strip()
    return None


def ethernet_connected() -> bool:
    """True if any ethernet/wired interface is in the 'connected' state."""
    r = _run(["nmcli", "-t", "-f", "TYPE,STATE", "device", "status"])
    if r.returncode != 0:
        return False
    for line in r.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 2:
            dev_type = parts[0].strip().lower()
            state = parts[1].strip().lower()
            if dev_type in ("ethernet", "802-3-ethernet") and state == "connected":
                return True
    return False


def wifi_already_configured() -> bool:
    """True if NetworkManager already has at least one WiFi connection profile."""
    r = _run(["nmcli", "-t", "-f", "TYPE,NAME", "connection", "show"])
    if r.returncode != 0:
        return False
    for line in r.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 1 and parts[0].strip().lower() in ("wifi", "802-11-wireless"):
            return True
    return False


def scan_networks(iface: str) -> list[dict]:
    """Return available WiFi networks as a list of {ssid, signal, security} dicts.

    Filters out hidden networks (empty SSID) and the provisioning hotspot itself.
    """
    # Force a fresh scan.
    _run(["nmcli", "device", "wifi", "rescan", "ifname", iface], timeout=10)
    r = _run(
        ["nmcli", "--terse", "--fields", "SSID,SIGNAL,SECURITY",
         "device", "wifi", "list", "ifname", iface],
        timeout=15,
    )
    if r.returncode != 0:
        logger.warning("WiFi scan failed: %s", r.stderr.strip())
        return []

    seen: set[str] = set()
    networks: list[dict] = []
    for line in r.stdout.splitlines():
        # nmcli --terse separates fields with ":" but SSIDs can contain ":"
        # so we split on the last two ":" only (SIGNAL and SECURITY are simple).
        parts = line.rsplit(":", 2)
        if len(parts) < 3:
            continue
        ssid = parts[0].strip()
        if not ssid or ssid == HOTSPOT_SSID or ssid in seen:
            continue
        seen.add(ssid)
        networks.append({
            "ssid": ssid,
            "signal": parts[1].strip(),
            "security": parts[2].strip() or "open",
        })

    # Sort strongest signal first.
    networks.sort(key=lambda n: int(n["signal"]) if n["signal"].isdigit() else 0, reverse=True)
    return networks


def create_hotspot(iface: str) -> bool:
    """Create the HADCD-Setup WPA2 hotspot.  Returns True on success."""
    # Delete any leftover hotspot profile from a previous run.
    _run(["nmcli", "connection", "delete", HOTSPOT_CONN_NAME])

    r = _run([
        "nmcli", "device", "wifi", "hotspot",
        "ifname",   iface,
        "ssid",     HOTSPOT_SSID,
        "password", HOTSPOT_PASSWORD,
        "con-name", HOTSPOT_CONN_NAME,
    ], timeout=20)
    if r.returncode == 0:
        logger.info("Hotspot '%s' active on %s  (password: %s)",
                    HOTSPOT_SSID, iface, HOTSPOT_PASSWORD)
        return True
    logger.error("Failed to create hotspot: %s", r.stderr.strip())
    return False


def teardown_hotspot() -> None:
    """Remove the temporary hotspot connection profile."""
    _run(["nmcli", "connection", "delete", HOTSPOT_CONN_NAME])
    logger.info("Hotspot torn down.")


def connect_to_wifi(iface: str, ssid: str, password: str) -> bool:
    """Connect the node to the target WiFi network.  Returns True on success."""
    cmd = ["nmcli", "device", "wifi", "connect", ssid, "ifname", iface]
    if password:
        cmd += ["password", password]
    r = _run(cmd, timeout=30)
    if r.returncode == 0:
        logger.info("Connected to '%s'.", ssid)
        return True
    logger.error("Failed to connect to '%s': %s", ssid, r.stderr.strip())
    return False


# ---------------------------------------------------------------------------
# HTTP server (blocking; runs in a background thread)
# ---------------------------------------------------------------------------

@dataclass
class _ProvisionResult:
    ssid: str
    password: str


def _build_handler(networks: list[dict], result_holder: list, done: threading.Event):
    """Return a BaseHTTPRequestHandler class with the scan results baked in."""

    networks_html = "".join(
        f'<option value="{html.escape(n["ssid"])}">'
        f'{html.escape(n["ssid"])} ({n["signal"]}% • {html.escape(n["security"])})'
        f"</option>"
        for n in networks
    )
    if not networks_html:
        networks_html = '<option value="">No networks found — retry in a moment</option>'

    page_template = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>HADCD Node — WiFi Setup</title>
  <style>
    body{{font-family:system-ui,sans-serif;max-width:480px;margin:2rem auto;padding:0 1rem;color:#1a1a1a}}
    h1{{font-size:1.3rem;margin-bottom:.25rem}}
    p{{color:#555;font-size:.9rem;margin-top:0}}
    label{{display:block;margin-top:1rem;font-weight:600;font-size:.9rem}}
    select,input{{width:100%;padding:.55rem;margin-top:.25rem;border:1px solid #ccc;
                  border-radius:6px;font-size:1rem;box-sizing:border-box}}
    button{{margin-top:1.5rem;width:100%;padding:.75rem;background:#2563eb;color:#fff;
            border:none;border-radius:6px;font-size:1rem;cursor:pointer}}
    button:hover{{background:#1d4ed8}}
    .note{{font-size:.8rem;color:#888;margin-top:1rem}}
  </style>
</head>
<body>
  <h1>HADCD Node — WiFi Setup</h1>
  <p>Connect this node to your building's WiFi network.<br>
     You only need to do this once.</p>
  <form method="POST" action="/connect">
    <label for="ssid">Network</label>
    <select id="ssid" name="ssid" required>{networks_html}</select>
    <label for="pw">Password</label>
    <input id="pw" name="pw" type="password"
           placeholder="Leave blank for open networks" autocomplete="off">
    <button type="submit">Connect</button>
  </form>
  <p class="note">Node IP on this hotspot: {HOTSPOT_IP}
     &nbsp;|&nbsp; After connecting, this page will close and the
     node will join your WiFi automatically on every future boot.</p>
</body>
</html>"""

    success_page = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>HADCD Node — Connected</title>
  <style>
    body{font-family:system-ui,sans-serif;max-width:480px;margin:2rem auto;
         padding:0 1rem;color:#1a1a1a;text-align:center}
    .icon{font-size:3rem;margin:1rem 0}
    h1{font-size:1.3rem}
    p{color:#555;font-size:.9rem}
  </style>
</head>
<body>
  <div class="icon">✅</div>
  <h1>Connected!</h1>
  <p>The node is now joining your WiFi network.<br>
     You can close this tab and disconnect from <strong>HADCD-Setup</strong>.<br>
     Your phone will reconnect to its normal network automatically.</p>
  <p>To finish setup, SSH in and edit <code>/etc/hadcd-agent/agent.env</code>,
     then run <code>sudo systemctl start hadcd-agent</code>.</p>
</body>
</html>"""

    error_page_tmpl = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>HADCD Node — Connection Failed</title>
  <style>
    body{{font-family:system-ui,sans-serif;max-width:480px;margin:2rem auto;padding:0 1rem}}
    h1{{color:#dc2626;font-size:1.3rem}}
    p{{color:#555;font-size:.9rem}}
    a{{color:#2563eb}}
  </style>
</head>
<body>
  <h1>Connection Failed</h1>
  <p>{reason}</p>
  <p><a href="/">← Try again</a></p>
</body>
</html>"""

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # suppress default access log to stderr
            logger.debug("http: " + fmt, *args)

        def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8"):
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, page_template)
            elif self.path == "/health":
                self._send(200, '{"status":"ok"}', "application/json")
            else:
                # Redirect everything else to / (handles captive-portal probes).
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()

        def do_POST(self):
            if self.path != "/connect":
                self.send_response(404)
                self.end_headers()
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                self._send(400, error_page_tmpl.format(reason="Bad request."))
                return
            if length < 0 or length > 16 * 1024:
                self._send(400, error_page_tmpl.format(reason="Request too large."))
                return
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            params = urllib.parse.parse_qs(raw)
            ssid = params.get("ssid", [""])[0].strip()
            password = params.get("pw", [""])[0]

            if not ssid:
                self._send(400, error_page_tmpl.format(reason="No SSID provided."))
                return
            # The SSID/password become nmcli argv entries. Argument lists
            # rule out shell injection, but a value starting with "-" would
            # be parsed as an nmcli option, and control characters have no
            # business in either field.
            if ssid.startswith("-") or any(ord(c) < 32 for c in ssid + password):
                self._send(400, error_page_tmpl.format(
                    reason="SSID or password contains unsupported characters."
                ))
                return

            logger.info("Provisioning request: SSID=%r", ssid)
            result_holder.append(_ProvisionResult(ssid=ssid, password=password))
            # Respond to the browser before we tear down the hotspot.
            self._send(200, success_page)
            # Signal the main loop that we have a result.
            done.set()

    return _Handler


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def provision() -> int:
    """Run WiFi provisioning.  Returns 0 on success (or skip), 1 on error."""
    if not _nmcli_available():
        logger.warning(
            "nmcli not found — WiFi provisioning unavailable.  "
            "Install network-manager if you need headless WiFi setup."
        )
        return 0

    # --- Fast-exit checks ---------------------------------------------------
    if PROVISIONED_FLAG.exists():
        logger.info("WiFi already provisioned (%s exists) — skipping.", PROVISIONED_FLAG)
        return 0

    if ethernet_connected():
        logger.info("Ethernet interface is connected — skipping WiFi provisioning.")
        return 0

    if wifi_already_configured():
        logger.info("WiFi connection profile already exists in NetworkManager — skipping.")
        return 0

    iface = find_wifi_interface()
    if not iface:
        logger.info("No WiFi interface found on this machine — skipping.")
        return 0

    logger.info("WiFi provisioning needed.  Interface: %s", iface)

    # --- Scan (before hotspot takes the radio into AP mode) -----------------
    logger.info("Scanning for available networks ...")
    networks = scan_networks(iface)
    logger.info("Found %d network(s): %s", len(networks),
                ", ".join(n["ssid"] for n in networks[:5]))

    # --- Create hotspot -----------------------------------------------------
    if not create_hotspot(iface):
        logger.error("Cannot create provisioning hotspot — giving up.")
        return 1

    logger.info(
        "=================================================================\n"
        "  WiFi provisioning mode\n"
        "  1. On your phone, join WiFi network:  %s\n"
        "     Password:  %s\n"
        "  2. Open a browser to:  http://%s\n"
        "  3. Pick your network, enter your password, tap Connect.\n"
        "=================================================================",
        HOTSPOT_SSID, HOTSPOT_PASSWORD, HOTSPOT_IP,
    )

    # --- Web server ---------------------------------------------------------
    done = threading.Event()
    result_holder: list[_ProvisionResult] = []
    handler_cls = _build_handler(networks, result_holder, done)

    try:
        httpd = HTTPServer(("0.0.0.0", HTTP_PORT), handler_cls)
    except OSError as exc:
        logger.error("Cannot bind to port %d: %s", HTTP_PORT, exc)
        teardown_hotspot()
        return 1

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    # Block until the operator submits the form or the timeout fires.
    timed_out = not done.wait(timeout=PROVISION_TIMEOUT_SEC)
    httpd.shutdown()

    if timed_out:
        logger.warning("Provisioning timed out after %d s.", PROVISION_TIMEOUT_SEC)
        teardown_hotspot()
        return 1

    result = result_holder[0]

    # Give the browser a moment to receive the success page before we
    # kill the hotspot (the hotspot is the phone's current default gateway).
    import time
    time.sleep(3)

    teardown_hotspot()

    # --- Connect to target WiFi ---------------------------------------------
    logger.info("Connecting to '%s' ...", result.ssid)
    if not connect_to_wifi(iface, result.ssid, result.password):
        logger.error(
            "Connection to '%s' failed.  Check the password and retry: "
            "sudo systemctl start hadcd-wifi-provision", result.ssid
        )
        return 1

    # --- Write provisioned flag ---------------------------------------------
    try:
        PROVISIONED_FLAG.parent.mkdir(parents=True, exist_ok=True)
        PROVISIONED_FLAG.touch()
    except OSError as exc:
        logger.warning("Could not write provisioned flag: %s", exc)

    logger.info(
        "WiFi provisioned.  Node will connect to '%s' automatically on "
        "every future boot.", result.ssid
    )
    return 0
