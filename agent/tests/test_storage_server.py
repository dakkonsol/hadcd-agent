"""Tests for the P2P storage server's bind address and pool serving.

The content-hash-as-capability model only holds while the port is
reachable over the tailnet alone, so the server must never bind all
interfaces: the agent passes the node's Tailscale IP, and the default
is loopback (fail-closed) rather than "" (fail-open to the LAN).
"""

from __future__ import annotations

import hashlib
import urllib.error
import urllib.request

import pytest

from agent.storage_server import StorageServer


@pytest.fixture()
def pool(tmp_path):
    d = tmp_path / "pool"
    d.mkdir()
    return d


def _put(pool, data: bytes) -> str:
    sha = hashlib.sha256(data).hexdigest()
    (pool / sha).write_bytes(data)
    return sha


def test_default_bind_is_loopback_not_all_interfaces(pool):
    srv = StorageServer(pool, 0)
    srv.start()
    try:
        assert srv._server.server_address[0] == "127.0.0.1"
    finally:
        srv.stop()


def test_explicit_host_is_honoured(pool):
    srv = StorageServer(pool, 0, host="127.0.0.1")
    srv.start()
    try:
        assert srv._server.server_address[0] == "127.0.0.1"
    finally:
        srv.stop()


def test_serves_pool_file_by_hash(pool):
    data = b"hello pool"
    sha = _put(pool, data)
    srv = StorageServer(pool, 0)
    srv.start()
    try:
        port = srv._server.server_address[1]
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/pool/{sha}", timeout=3
        ) as resp:
            assert resp.read() == data
            assert resp.headers["X-SHA256"] == sha
    finally:
        srv.stop()


def test_non_hash_paths_are_404(pool):
    _put(pool, b"data")
    srv = StorageServer(pool, 0)
    srv.start()
    try:
        port = srv._server.server_address[1]
        for path in ("/pool/../secrets", "/pool/abc", "/other"):
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}{path}", timeout=3
                )
            assert exc_info.value.code == 404
    finally:
        srv.stop()
