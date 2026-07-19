"""Tests for agent/watchdog.py — sd_notify integration.

The module must:
  * be a no-op when NOTIFY_SOCKET is not set (dev mode / Docker)
  * write the correct datagram when NOTIFY_SOCKET is set (systemd mode)
"""

from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.watchdog import _notify, notify_ready, notify_watchdog


# ---------------------------------------------------------------------------
# No-op path (NOTIFY_SOCKET absent)
# ---------------------------------------------------------------------------

def test_notify_noop_when_no_socket(monkeypatch):
    """_notify must silently do nothing when NOTIFY_SOCKET is not set."""
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    # Should not raise; calling it is the entire test.
    _notify("READY=1")


def test_notify_ready_noop(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    notify_ready()  # no exception, no side effects


def test_notify_watchdog_noop(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    notify_watchdog()  # no exception, no side effects


# ---------------------------------------------------------------------------
# Live path (NOTIFY_SOCKET is a real Unix socket) — sd_notify is a
# systemd/Linux mechanism, so these can only run where AF_UNIX exists.
# ---------------------------------------------------------------------------

requires_af_unix = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="Unix domain sockets are unavailable on this platform (Windows)",
)


def _make_notify_socket() -> tuple[socket.socket, str]:
    """Create a listening Unix datagram socket and return it + its path."""
    tmp = tempfile.mktemp(suffix=".notify.sock", prefix="/tmp/hadcd_test_")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(tmp)
    sock.settimeout(1.0)
    return sock, tmp


@requires_af_unix
def test_notify_sends_datagram(monkeypatch):
    """_notify must send the message string to the NOTIFY_SOCKET."""
    server, path = _make_notify_socket()
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    try:
        _notify("WATCHDOG=1")
        data, _ = server.recvfrom(256)
        assert data == b"WATCHDOG=1"
    finally:
        server.close()
        Path(path).unlink(missing_ok=True)


@requires_af_unix
def test_notify_ready_content(monkeypatch):
    """notify_ready() must include READY=1 in the datagram."""
    server, path = _make_notify_socket()
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    try:
        notify_ready()
        data, _ = server.recvfrom(256)
        assert b"READY=1" in data
    finally:
        server.close()
        Path(path).unlink(missing_ok=True)


@requires_af_unix
def test_notify_watchdog_content(monkeypatch):
    """notify_watchdog() must include WATCHDOG=1 in the datagram."""
    server, path = _make_notify_socket()
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    try:
        notify_watchdog()
        data, _ = server.recvfrom(256)
        assert b"WATCHDOG=1" in data
    finally:
        server.close()
        Path(path).unlink(missing_ok=True)


def test_notify_bad_socket_path_no_raise(monkeypatch):
    """_notify must not raise even when the socket path does not exist."""
    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/does_not_exist_hadcd_test.sock")
    # Should swallow the error silently.
    _notify("READY=1")
