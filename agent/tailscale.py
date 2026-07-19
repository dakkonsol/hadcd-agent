"""Tailscale detection and advisory for the HADCD agent.

Checks whether Tailscale is installed on this machine and, if so,
whether it is connected to a tailnet.  The results are used at agent
startup to:

  * log the Tailscale IP / hostname so operators know what to put in
    HADCD_API on remote agents pointing at this backend;
  * log a recommendation to install Tailscale when it is absent, so
    multi-site deployments (apartment GPU ↔ home backend) are easy.

Tailscale is NOT required.  Single-node local deployments work fine
without it.  The agent never refuses to start because Tailscale is
absent or disconnected.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger("hadcd.agent")

# Seconds to wait for `tailscale status --json` before giving up.
_STATUS_TIMEOUT_SEC = 5


@dataclass(frozen=True)
class TailscaleStatus:
    """Result of probing the local Tailscale daemon."""

    installed: bool
    """True if the tailscale binary was found on this machine."""

    connected: bool
    """True if Tailscale is running and authenticated (BackendState=Running)."""

    tailscale_ip: str | None = field(default=None)
    """The node's Tailscale IPv4 address (100.x.x.x), or None."""

    hostname: str | None = field(default=None)
    """The node's MagicDNS hostname (e.g. home-pc.tail-abc123.ts.net)."""


def _find_tailscale_binary() -> str | None:
    """Return the path to the tailscale binary, or None if not found."""
    # Try PATH first (works on Linux / macOS and Windows when added to PATH).
    binary = shutil.which("tailscale")
    if binary:
        return binary

    # Windows: Tailscale installs under Program Files.
    if platform.system() == "Windows":
        candidates = [
            os.path.join(os.environ.get("ProgramFiles", ""), "Tailscale", "tailscale.exe"),
            os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Tailscale", "tailscale.exe"),
            # Winget / MSIX installs land here on some machines.
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Tailscale", "tailscale.exe"),
        ]
        for c in candidates:
            if c and os.path.isfile(c):
                return c

    # Linux / macOS: common non-PATH locations.
    for c in ("/usr/bin/tailscale", "/usr/local/bin/tailscale",
              "/opt/homebrew/bin/tailscale"):
        if os.path.isfile(c):
            return c

    return None


def check_tailscale_status() -> TailscaleStatus:
    """Probe the local Tailscale installation.

    Never raises.  If the binary is missing, returns
    ``TailscaleStatus(installed=False, connected=False)``.
    If the binary exists but the daemon is not running or returns
    unexpected output, returns
    ``TailscaleStatus(installed=True, connected=False)``.
    """
    binary = _find_tailscale_binary()
    if binary is None:
        return TailscaleStatus(installed=False, connected=False)

    try:
        result = subprocess.run(
            [binary, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=_STATUS_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired):
        return TailscaleStatus(installed=True, connected=False)

    if result.returncode != 0:
        # Daemon not running, or not logged in yet.
        return TailscaleStatus(installed=True, connected=False)

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return TailscaleStatus(installed=True, connected=False)

    backend_state = data.get("BackendState", "")
    if backend_state != "Running":
        return TailscaleStatus(installed=True, connected=False)

    self_node = data.get("Self") or {}
    ips: list[str] = self_node.get("TailscaleIPs") or []
    # Prefer the IPv4 address (100.x.x.x); fall back to whatever is first.
    tailscale_ip: str | None = None
    for ip in ips:
        if ip.startswith("100."):
            tailscale_ip = ip
            break
    if tailscale_ip is None and ips:
        tailscale_ip = ips[0]

    # DNSName from `tailscale status --json` includes a trailing dot;
    # strip it so it's a usable hostname.
    raw_dns = self_node.get("DNSName") or ""
    hostname: str | None = raw_dns.rstrip(".") or None

    return TailscaleStatus(
        installed=True,
        connected=True,
        tailscale_ip=tailscale_ip,
        hostname=hostname,
    )


def log_tailscale_advisory(status: TailscaleStatus) -> None:
    """Log a contextual message about Tailscale at agent startup.

    Three cases:

    connected
        Info: log the Tailscale IP and hostname so the operator knows
        what URL to put in HADCD_API on remote agents.

    installed but not connected
        Warning: the binary exists but `tailscale up` has not been run,
        or the daemon is not running.

    not installed
        Info: brief recommendation with install URL.  Quieter than the
        Sunshine advisory because Tailscale is only needed for multi-
        site deployments (it is irrelevant for a single local node).
    """
    if status.connected:
        # Build the most useful address to display.
        addr_parts: list[str] = []
        if status.hostname:
            addr_parts.append(status.hostname)
        if status.tailscale_ip:
            addr_parts.append(status.tailscale_ip)
        addr = " / ".join(addr_parts) if addr_parts else "(unknown)"

        logger.info(
            "Tailscale connected — this node's address: %s\n"
            "  Remote agents can reach this backend at:\n"
            "    HADCD_API=http://%s:8000",
            addr,
            status.hostname or status.tailscale_ip or "100.x.x.x",
        )
        return

    if status.installed:
        logger.warning(
            "Tailscale is installed but not connected.\n"
            "  Run `tailscale up` (or open the Tailscale app) to join your\n"
            "  tailnet, then remote agents can reach this backend via:\n"
            "    HADCD_API=http://<this-machine-name>.ts.net:8000\n"
            "  Continuing without Tailscale — local dispatch is unaffected."
        )
        return

    # Not installed — quiet INFO only (not needed for single-node installs).
    logger.info(
        "Tailscale is not installed on this machine.\n"
        "  For multi-site deployments (e.g. agents at a remote apartment\n"
        "  connecting back to a home backend), Tailscale is the simplest\n"
        "  way to link them without port forwarding or a public IP:\n"
        "    https://tailscale.com/download\n"
        "  Single-node local deployments run fine without it."
    )
