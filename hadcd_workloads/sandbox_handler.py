"""Phase 10h — Disposable Sandbox Session handler.

Spins up a temporary Ubuntu/XFCE4 desktop in Docker, streamed via
Sunshine to the operator's Moonlight client.  The container is
force-removed when the session ends (timeout or client disconnect).

Task payload / args
-------------------
connection_timeout_min : float  (default 5)
    Minutes to wait for a Moonlight client to connect before giving
    up.  Prevents orphaned containers when the operator submits but
    never connects.
session_max_min : float  (default 60)
    Hard maximum for the total session lifetime in minutes.  The
    container is force-killed when this elapses regardless of whether
    a client is connected.
disconnect_grace_min : float  (default 2)
    After a client disconnects, keep the container alive for this
    many minutes to allow a brief reconnect before destroying it.
gpu_request : bool  (default False)
    Pass ``--gpus all`` to the container for NVENC hardware encoding.
    Faster streaming quality; requires NVIDIA Container Toolkit on the
    host.  False = x264 software encoding (always available, slower).

Task result
-----------
Returns a dict with:
  moonlight_host   str   IP address the operator should enter in Moonlight
  moonlight_port   int   RTSP port (47994 — the host-side mapped port)
  web_ui_url       str   Sunshine HTTPS web UI for first-time pairing
  connected_at     str | None  ISO-8601 UTC timestamp of first connection
  disconnected_at  str | None  ISO-8601 UTC timestamp of final disconnect
  session_sec      float  Total wall-clock seconds the task ran
  terminated_by    str   "connection_timeout" | "max_time" |
                         "client_disconnected" | "error"

Frontend note
-------------
While the task is RUNNING the result is not yet available.  The
frontend should use ``task.claimed_by`` (node UUID) → ``node.local_ip``
to show the connection address immediately after the task is dispatched.
The static port (47994) is always the same.
"""

from __future__ import annotations

import logging
import os
import socket
import ssl
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from hadcd_workloads.registry import register

logger = logging.getLogger("hadcd.workloads.sandbox")

# ── Constants ─────────────────────────────────────────────────────────────────

SANDBOX_IMAGE = "hadcd-sandbox:latest"

# Host-side ports (container internal ports are offset by −10 to avoid
# collision with a Mode-2 host Sunshine instance on the standard ports).
MOONLIGHT_HOST_PORT = 47994   # container 47984 — Moonlight RTSP
SUNSHINE_WEB_HOST_PORT = 48000  # container 47990 — Sunshine HTTPS web UI

# Full port mapping: container_port/proto → host_port (int)
PORT_BINDINGS: dict[str, int] = {
    "47984/tcp": 47994,
    "47989/tcp": 47999,
    "47990/tcp": 48000,
    "47998/udp": 48008,
    "47999/udp": 48009,
    "48000/udp": 48010,
    "48010/tcp": 48020,
}

DEFAULT_CONNECTION_TIMEOUT_MIN: float = 5.0
DEFAULT_SESSION_MAX_MIN: float = 60.0
DEFAULT_DISCONNECT_GRACE_MIN: float = 2.0

# Sunshine admin credentials written by entrypoint.sh.
# Used for log-based session detection fallback.
_SUNSHINE_USER = "hadcd"
_SUNSHINE_PASS = "hadcd"

# Sunshine config volume — survives sandbox restarts so paired clients
# do not need to re-pair every time.
_CONFIG_HOST_DIR = os.environ.get(
    "HADCD_SANDBOX_CONFIG_DIR", "/var/lib/hadcd-agent/sandbox-config"
)

# Location of the Dockerfile.  Resolved relative to this file so it
# works whether the repo is installed as a package or run in-place.
# Override via HADCD_SANDBOX_DIR env var for non-standard deployments.
_DEFAULT_SANDBOX_DIR = str(
    Path(__file__).parent.parent / "agent" / "sandbox"
)
_SANDBOX_DIR = os.environ.get("HADCD_SANDBOX_DIR", _DEFAULT_SANDBOX_DIR)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _local_ip() -> str:
    """Return the host's primary outbound IP via connect-trick."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"


def _ensure_image(client: object) -> None:  # client: docker.DockerClient
    """Build the sandbox image locally if it does not already exist."""
    import docker  # type: ignore[import]

    try:
        client.images.get(SANDBOX_IMAGE)  # type: ignore[union-attr]
        logger.info("sandbox: image %s already exists", SANDBOX_IMAGE)
        return
    except docker.errors.ImageNotFound:
        pass

    sandbox_dir = Path(_SANDBOX_DIR)
    if not sandbox_dir.is_dir() or not (sandbox_dir / "Dockerfile").exists():
        raise RuntimeError(
            f"Sandbox Dockerfile not found at {sandbox_dir}. "
            "Ensure agent/sandbox/ is present in the deployment "
            "or set HADCD_SANDBOX_DIR to its location."
        )

    logger.info(
        "sandbox: building %s from %s (first-use build — may take "
        "a few minutes while pulling the base image)…",
        SANDBOX_IMAGE,
        sandbox_dir,
    )
    tail_lines: list[str] = []
    try:
        _img, build_iter = client.images.build(  # type: ignore[union-attr]
            path=str(sandbox_dir),
            tag=SANDBOX_IMAGE,
            rm=True,
        )
        for chunk in build_iter:
            if "stream" in chunk:
                line = chunk["stream"].rstrip()
                if line:
                    logger.debug("sandbox build: %s", line)
                    tail_lines.append(line)
                    if len(tail_lines) > 30:
                        tail_lines.pop(0)
    except Exception as exc:
        raise RuntimeError(
            f"sandbox image build failed: {exc}\n"
            "Last build output:\n" + "\n".join(tail_lines)
        ) from exc

    logger.info("sandbox: image %s built successfully", SANDBOX_IMAGE)


def _tls_ctx() -> ssl.SSLContext:
    """Return an SSL context that skips Sunshine's self-signed cert."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _wait_sunshine_ready(timeout_sec: float = 90.0) -> bool:
    """Poll Sunshine's HTTPS endpoint until it responds or times out."""
    url = f"https://localhost:{SUNSHINE_WEB_HOST_PORT}"
    ctx = _tls_ctx()
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, context=ctx, timeout=3)
            return True
        except Exception:
            time.sleep(2)
    return False


def _scan_logs_for_events(
    container: object,
    since_ts: float | None = None,
) -> tuple[bool, bool]:
    """Parse container log output for Sunshine connection / disconnect events.

    Returns ``(connected, disconnected)`` as booleans.  Best-effort —
    the caller must not rely on this exclusively; timeouts provide the
    safety net.

    ``since_ts`` is a Unix timestamp; only log lines emitted after that
    time are examined (avoids re-triggering on stale events).
    """
    try:
        kwargs: dict = {"stderr": True, "stdout": True}
        if since_ts is not None:
            # docker-py accepts datetime or Unix int for 'since'
            kwargs["since"] = int(since_ts)
        raw = (
            container.logs(**kwargs)  # type: ignore[union-attr]
            .decode("utf-8", errors="replace")
            .lower()
        )
    except Exception:
        return False, False

    connected = any(
        kw in raw
        for kw in (
            "new connection",
            "client connected",
            "client_connected",
            "connected from",
        )
    )
    disconnected = any(
        kw in raw
        for kw in (
            "client disconnected",
            "client_disconnected",
            "disconnecting client",
            "session stopped",
        )
    )
    return connected, disconnected


# ── Main handler ──────────────────────────────────────────────────────────────


@register("sandbox_session")
def _sandbox_session(args: dict) -> dict:
    """Phase 10h: start a disposable XFCE4 desktop sandbox.

    See module docstring for the full arg / result contract.
    """
    # Lazy import — prevents import-time failure on hosts without Docker SDK.
    try:
        import docker  # type: ignore[import]
        from docker.errors import APIError, DockerException  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "docker Python SDK is not installed on this node. "
            "Install it: pip install docker"
        ) from exc

    # ── Arg parsing ────────────────────────────────────────────────────────────
    def _float(key: str, default: float) -> float:
        try:
            return float(args.get(key, default))
        except (TypeError, ValueError):
            return default

    connection_timeout_min = _float("connection_timeout_min", DEFAULT_CONNECTION_TIMEOUT_MIN)
    session_max_min = _float("session_max_min", DEFAULT_SESSION_MAX_MIN)
    disconnect_grace_min = _float("disconnect_grace_min", DEFAULT_DISCONNECT_GRACE_MIN)
    gpu_request = bool(args.get("gpu_request", False))

    # ── Docker client ──────────────────────────────────────────────────────────
    try:
        client = docker.from_env()
    except DockerException as exc:
        raise RuntimeError(f"cannot connect to Docker daemon: {exc}") from exc

    # Build the image locally on first use (subsequent uses are instant).
    _ensure_image(client)

    # ── Port bindings ─────────────────────────────────────────────────────────
    port_bindings: dict = {
        cp: [{"HostPort": str(hp), "HostIp": ""}]
        for cp, hp in PORT_BINDINGS.items()
    }

    # ── Config volume ─────────────────────────────────────────────────────────
    os.makedirs(_CONFIG_HOST_DIR, exist_ok=True)
    volumes = {_CONFIG_HOST_DIR: {"bind": "/config", "mode": "rw"}}

    # ── GPU ───────────────────────────────────────────────────────────────────
    device_requests = None
    if gpu_request:
        from docker.types import DeviceRequest  # type: ignore[import]
        device_requests = [DeviceRequest(count=-1, capabilities=[["gpu"]])]

    host_ip = _local_ip()
    web_ui_url = f"https://{host_ip}:{SUNSHINE_WEB_HOST_PORT}"

    logger.info(
        "sandbox: launching (gpu=%s, connection_timeout=%.0f min, "
        "session_max=%.0f min)",
        gpu_request,
        connection_timeout_min,
        session_max_min,
    )

    session_start = time.monotonic()
    connected_at: datetime | None = None
    disconnected_at: datetime | None = None
    terminated_by = "error"
    container = None

    try:
        # ── Start container ────────────────────────────────────────────────────
        try:
            container = client.containers.run(
                image=SANDBOX_IMAGE,
                detach=True,
                auto_remove=False,
                ports=port_bindings,
                volumes=volumes,
                device_requests=device_requests,
                shm_size="256m",  # shared memory for X11 / Sunshine
            )
        except APIError as exc:
            raise RuntimeError(f"docker API error starting sandbox: {exc}") from exc

        logger.info("sandbox: container %s started", container.id[:12])

        # ── Wait for Sunshine to be ready ──────────────────────────────────────
        if not _wait_sunshine_ready(timeout_sec=90.0):
            raise RuntimeError(
                "Sunshine did not respond within 90 s. "
                "Check the sandbox image build and entrypoint.sh."
            )

        logger.info(
            "sandbox: ready — Moonlight → %s:%d | Web UI → %s",
            host_ip,
            MOONLIGHT_HOST_PORT,
            web_ui_url,
        )

        # ── Session monitoring loop ────────────────────────────────────────────
        # Poll every 10 s for Sunshine log events.  Two independent timeouts
        # provide the safety net when log parsing is inconclusive.
        ever_connected = False
        was_connected = False
        disconnect_started: float | None = None
        last_check_ts: float = time.time() - 15  # scan all logs on first pass

        while True:
            elapsed = time.monotonic() - session_start

            # Hard session ceiling
            if elapsed >= session_max_min * 60:
                terminated_by = "max_time"
                logger.info(
                    "sandbox: max session time (%.0f min) reached", session_max_min
                )
                break

            # Parse new log lines since last check
            log_connected, log_disconnected = _scan_logs_for_events(
                container, since_ts=last_check_ts
            )
            last_check_ts = time.time()

            # ── Connection detected ────────────────────────────────────────────
            if log_connected:
                if not ever_connected:
                    ever_connected = True
                    connected_at = datetime.now(timezone.utc)
                    logger.info("sandbox: Moonlight client connected")
                if not was_connected:
                    was_connected = True
                    disconnect_started = None  # reset any pending grace timer
                    disconnected_at = None

            # ── Disconnect detected ────────────────────────────────────────────
            if log_disconnected and was_connected:
                if disconnect_started is None:
                    disconnect_started = time.monotonic()
                    disconnected_at = datetime.now(timezone.utc)
                    logger.info(
                        "sandbox: client disconnected — grace %.0f min",
                        disconnect_grace_min,
                    )

            # ── Grace period expired ───────────────────────────────────────────
            if disconnect_started is not None:
                grace_elapsed = time.monotonic() - disconnect_started
                if grace_elapsed >= disconnect_grace_min * 60:
                    terminated_by = "client_disconnected"
                    break

            # ── Connection timeout (nobody ever connected) ─────────────────────
            if not ever_connected and elapsed >= connection_timeout_min * 60:
                terminated_by = "connection_timeout"
                logger.info(
                    "sandbox: no client connected within %.0f min — terminating",
                    connection_timeout_min,
                )
                break

            time.sleep(10)

    except Exception as exc:
        terminated_by = "error"
        logger.exception("sandbox: session error: %s", exc)
        raise
    finally:
        if container is not None:
            try:
                container.stop(timeout=5)
            except Exception:
                pass
            try:
                container.remove(force=True)
                logger.info("sandbox: container %s removed", container.id[:12])
            except Exception:
                logger.warning(
                    "sandbox: container removal failed for %s", container.id[:12]
                )

    session_sec = time.monotonic() - session_start
    logger.info(
        "sandbox: session ended (terminated_by=%s, duration=%.0f s)",
        terminated_by,
        session_sec,
    )

    return {
        "moonlight_host": host_ip,
        "moonlight_port": MOONLIGHT_HOST_PORT,
        "web_ui_url": web_ui_url,
        "connected_at": connected_at.isoformat() if connected_at else None,
        "disconnected_at": disconnected_at.isoformat() if disconnected_at else None,
        "session_sec": round(session_sec, 1),
        "terminated_by": terminated_by,
    }
