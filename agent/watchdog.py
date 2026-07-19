"""Minimal systemd sd_notify integration — no extra dependencies.

Lets the agent:

  * signal READY=1 after enrollment so systemd doesn't declare the
    service "active" before it can actually take work;
  * ping WATCHDOG=1 on every heartbeat iteration so systemd can detect
    a frozen event loop and restart the process automatically.

When NOTIFY_SOCKET is not set (dev mode, Docker without systemd, plain
terminal) all calls are silent no-ops.  The sd_notify protocol is a
single UDP datagram to a Unix domain socket, so this module has zero
extra dependencies.
"""

from __future__ import annotations

import logging
import os
import socket

logger = logging.getLogger("hadcd.watchdog")


def _notify(message: str) -> None:
    """Send one sd_notify datagram.  Silent no-op outside systemd."""
    sock_path = os.environ.get("NOTIFY_SOCKET", "")
    if not sock_path:
        return
    # systemd may use an abstract-namespace socket (path starts with "@")
    # or a regular filesystem socket path.
    addr: str | bytes
    if sock_path.startswith("@"):
        # Abstract namespace: replace "@" with the null byte prefix.
        addr = ("\0" + sock_path[1:]).encode("utf-8")
    else:
        addr = sock_path
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode("utf-8"), addr)
    except Exception:
        # Never let a watchdog failure disturb the agent process.
        logger.debug("sd_notify failed (non-fatal)", exc_info=True)


def notify_ready() -> None:
    """Signal READY=1 — the agent is enrolled and ready to take work.

    Call once, immediately after ``ensure_enrolled`` succeeds.
    With ``Type=notify`` in the service unit, systemd holds the service
    in the "activating" state until this fires — guaranteeing that a
    reload / dependency ordering waits for a fully live agent, not just
    a process that exists.
    """
    _notify("READY=1\nSTATUS=enrolled and running")
    logger.debug("sd_notify: READY=1")


def notify_watchdog() -> None:
    """Ping the watchdog — call on every heartbeat loop iteration.

    The service unit sets ``WatchdogSec=90``.  The heartbeat loop
    fires every 10 s, so up to 9 consecutive heartbeat failures are
    tolerated before the watchdog concludes the event loop is frozen
    and tells systemd to kill and restart the process.
    """
    _notify("WATCHDOG=1")
