"""Phase 20c — Minimal HTTP file server for direct P2P storage transfers.

Runs in a daemon background thread so it doesn't block the async event loop.
Only serves GET /pool/{sha256} — the content-addressed pool files.

Other nodes on the Tailscale mesh reach this directly, bypassing the backend
relay entirely.  The SHA-256 in the URL acts as a capability token: you need
to know the hash to request the file.  TLS is not added here because Tailscale
already provides encrypted transport between nodes.
"""

from __future__ import annotations

import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

logger = logging.getLogger("hadcd.storage_server")

_SHA256_RE = re.compile(r"^/pool/([0-9a-f]{64})$")
_READ_CHUNK = 64 * 1024  # 64 KB read buffer


class _StorageHandler(BaseHTTPRequestHandler):
    """Minimal request handler — GET /pool/{sha256} only."""

    # Set on the class before the server starts (thread-safe: written once,
    # read many after that).
    pool_dir: Path

    def do_GET(self) -> None:
        m = _SHA256_RE.match(self.path)
        if not m:
            self.send_error(404, "not found")
            return

        sha256 = m.group(1)
        fp = self.pool_dir / sha256

        if not fp.exists():
            self.send_error(404, "file not in pool")
            return

        try:
            size = fp.stat().st_size
        except OSError:
            self.send_error(500, "stat failed")
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("X-SHA256", sha256)
        self.end_headers()

        try:
            with open(fp, "rb") as fh:
                while True:
                    data = fh.read(_READ_CHUNK)
                    if not data:
                        break
                    self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client disconnected mid-transfer — normal
        except OSError as exc:
            logger.warning("storage-server: send error for %s: %s", sha256[:8], exc)

    def log_message(self, fmt: str, *args: object) -> None:  # type: ignore[override]
        logger.debug("storage-server: " + fmt, *args)


class StorageServer:
    """Thin wrapper: starts an HTTPServer in a daemon thread.

    Usage::

        srv = StorageServer(pool_dir=Path("/mnt/data/pool"), port=8015)
        srv.start()   # non-blocking; server runs until process exits
        # ...
        srv.stop()    # graceful shutdown (optional — daemon thread exits on quit)
    """

    def __init__(self, pool_dir: Path, port: int, host: str = "127.0.0.1") -> None:
        """`host` is the interface to bind.

        The security model relies on Tailscale as the transport (see module
        docstring), so the caller should pass the node's Tailscale IP; the
        default is loopback so a misconfigured node degrades to
        unreachable-from-peers rather than open-to-the-LAN. Binding all
        interfaces ("" / "0.0.0.0") is never appropriate here — a SHA-256
        URL is a capability token only while the port is tailnet-only.
        """
        self._pool_dir = pool_dir
        self._port = port
        self._host = host
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the server.  Idempotent — safe to call twice."""
        if self._thread and self._thread.is_alive():
            return

        # Dynamically subclass _StorageHandler to inject the pool_dir
        # without a global variable (safe for multiple servers in tests).
        handler_cls = type(
            "_Handler",
            (_StorageHandler,),
            {"pool_dir": self._pool_dir},
        )

        self._server = HTTPServer((self._host, self._port), handler_cls)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name=f"storage-p2p-{self._port}",
        )
        self._thread.start()
        logger.info(
            "storage: P2P server listening on %s:%d (pool=%s)",
            self._host,
            self._port,
            self._pool_dir,
        )

    def stop(self) -> None:
        """Shut down the server gracefully."""
        if self._server:
            self._server.shutdown()
            self._server = None
        self._thread = None
