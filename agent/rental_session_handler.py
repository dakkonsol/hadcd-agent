"""Rental session handler — Phase 18c.

Manages Docker containers for paid GPU rental sessions.  Called from the
agent's heartbeat loop after each successful heartbeat.

The heartbeat response now carries a `sessions` list (NodeHeartbeatResponse).
Each entry has:
  session_id, type ('ssh' | 'api_endpoint'), status, model, duration_hr

Actions taken per status:
  starting → spin up container, POST /activate with connection_info
  active   → no action (already running; just track the container ID)
  stopping → stop + remove container, POST /stopped

Container images:
  ssh          — hadcd/ssh-gpu:latest    (Ubuntu + CUDA + SSH daemon)
  api_endpoint — ollama/ollama:latest    (Ollama server, OpenAI-compatible)

Network:
  Containers are exposed on the host's Tailscale IP (100.x.x.x) via
  randomly allocated host ports.  Clients connect directly over the Tailscale
  network; the dispatcher never proxies the data plane.

Security:
  --cap-drop ALL, --security-opt no-new-privileges, custom bridge network.
  SSH passwords are random hex; API tokens are per-session UUIDs.
  Neither is reused across sessions.
"""

from __future__ import annotations

import asyncio
import logging
import random
import secrets
import string
import uuid
from typing import Any, Callable

import httpx

logger = logging.getLogger("hadcd.session_handler")

# Port range for SSH and API endpoint containers.
_PORT_MIN = 52000
_PORT_MAX = 59999

# Default Docker images (operator can override via env).
_SSH_IMAGE = "hadcd/ssh-gpu:latest"
_OLLAMA_IMAGE = "ollama/ollama:latest"

# Docker bridge network for rental sessions (created once on agent startup).
_SESSION_NETWORK = "hadcd-rental-bridge"

# Phase 26 — named Docker volume shared by every api_endpoint session
# container. Ollama stores pulled models under /root/.ollama; mounting
# the same volume across sessions means a model is pulled once per node,
# not once per session, and the dispatcher's warm-model placement has
# something real to route to.
_OLLAMA_MODELS_VOLUME = "hadcd-ollama-models"

# Media (ComfyUI) container. Only operator-owned, opted-in nodes ever receive
# a `media` session (the dispatcher enforces owner_kind=operator + media_capable;
# see backend session_assigner._node_serves_session_type). The operator's
# COMFYUI_MODELS_PATH host dir is bind-mounted so models persist across
# sessions and are the target for remote model sync. ComfyUI serves on 8188.
_COMFYUI_IMAGE = "ghcr.io/ai-dock/comfyui:latest"
_COMFYUI_INTERNAL_PORT = 8188


def _random_port() -> int:
    return random.randint(_PORT_MIN, _PORT_MAX)


def _random_password(length: int = 24) -> str:
    return secrets.token_hex(length)


def _random_token() -> str:
    return str(uuid.uuid4())


class RentalSessionHandler:
    """Manages rental session containers for one node agent.

    Maintains an in-memory map of session_id → container_id so containers
    are not re-created on each heartbeat tick.
    """

    def __init__(
        self,
        node_id: str,
        dispatcher_url: str,
        node_token: str,
        on_model_cached: "Callable[[str], None] | None" = None,
        media_models_path: str = "",
        comfyui_image: str = _COMFYUI_IMAGE,
        publish_host: str = "127.0.0.1",
    ) -> None:
        self._node_id = node_id
        self._dispatcher_url = dispatcher_url.rstrip("/")
        self._node_token = node_token
        # Interface session container ports are published on. Sessions are
        # advertised to clients as tailnet-only, so the agent passes the
        # node's Tailscale IP; the default is loopback so a node without
        # Tailscale fails closed instead of exposing SSH/Ollama/ComfyUI
        # ports to its LAN (Docker's own default would be 0.0.0.0).
        self._publish_host = publish_host
        # Media (ComfyUI) opt-in: host models dir + image. Empty path = media
        # disabled on this node (the agent also reports media_capable=false).
        self._media_models_path = media_models_path
        self._comfyui_image = comfyui_image
        # Phase 26 — called with the model name once an api_endpoint
        # container is serving it (the model now sits in the shared
        # volume). The agent uses this to keep the heartbeat's
        # cached_models list accurate.
        self._on_model_cached = on_model_cached
        # session_id → {"container_id": str, "port": int, "type": str}
        self._active: dict[str, dict[str, Any]] = {}

    # ── Public interface — called from heartbeat loop ─────────────────────────

    async def handle_sessions(self, sessions: list[dict]) -> None:
        """Process the `sessions` list from the heartbeat response.

        Sessions are handled concurrently — spinning up a new container or
        stopping one does not block other sessions in the list.
        """
        if not sessions:
            return

        tasks = []
        for entry in sessions:
            sid = str(entry.get("session_id", ""))
            status = entry.get("status", "")
            stype = entry.get("type", "ssh")
            model = entry.get("model")

            if status == "starting" and sid not in self._active:
                tasks.append(self._start_session(sid, stype, model))
            elif status == "stopping" and sid in self._active:
                tasks.append(self._stop_session(sid))
            # 'active' with container already running → no-op

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── Container lifecycle ───────────────────────────────────────────────────

    async def _start_session(
        self, session_id: str, stype: str, model: str | None
    ) -> None:
        """Spin up a container and report activation to the dispatcher."""
        try:
            import docker  # type: ignore[import]
        except ImportError:
            logger.error("docker-py not installed — cannot start rental session %s", session_id)
            return

        port = _random_port()
        container_id: str | None = None

        try:
            client = docker.from_env()
            await _ensure_network(client)

            if stype == "ssh":
                password = _random_password()
                container = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: client.containers.run(
                        _SSH_IMAGE,
                        detach=True,
                        remove=False,
                        name=f"hadcd-session-{session_id[:8]}",
                        network=_SESSION_NETWORK,
                        ports={"22/tcp": (self._publish_host, port)},
                        cap_drop=["ALL"],
                        security_opt=["no-new-privileges:true"],
                        environment={"SSH_PASSWORD": password},
                        device_requests=[
                            docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])
                        ] if _gpu_available(client) else [],
                    )
                )
                container_id = container.id[:12]
                connection_info = {
                    "host": await _get_tailscale_ip(),
                    "port": port,
                    "username": "user",
                    "password": password,
                    "note": "SSH over Tailscale. Connect: ssh user@<host> -p <port>",
                }
                logger.info(
                    "rental session %s (ssh): container %s started on port %d",
                    session_id, container_id, port,
                )

            elif stype == "media":
                # ComfyUI for image/video/3D/audio. The dispatcher only ever
                # sends `media` here to an operator-owned, opted-in node, so
                # reaching this branch implies media is enabled — but guard
                # anyway: without a models path we cannot serve.
                if not self._media_models_path:
                    logger.error(
                        "rental session %s is media but COMFYUI_MODELS_PATH is "
                        "unset — cannot serve; leaving unstarted", session_id,
                    )
                    return
                token = _random_token()
                models_path = self._media_models_path
                image = self._comfyui_image
                container = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: client.containers.run(
                        image,
                        detach=True,
                        remove=False,
                        name=f"hadcd-session-{session_id[:8]}",
                        network=_SESSION_NETWORK,
                        ports={f"{_COMFYUI_INTERNAL_PORT}/tcp": (self._publish_host, port)},
                        cap_drop=["ALL"],
                        security_opt=["no-new-privileges:true"],
                        environment={
                            "DIRECT_ADDRESS": "0.0.0.0",
                            "DIRECT_ADDRESS_PORT": str(_COMFYUI_INTERNAL_PORT),
                            "HADCD_SESSION_TOKEN": token,
                        },
                        # Operator's persistent media-models dir (also the
                        # remote-model-sync target) → ComfyUI's models dir.
                        volumes={
                            models_path: {
                                "bind": "/opt/ComfyUI/models",
                                "mode": "rw",
                            }
                        },
                        device_requests=[
                            docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])
                        ] if _gpu_available(client) else [],
                    )
                )
                container_id = container.id[:12]
                tailscale_ip = await _get_tailscale_ip()
                connection_info = {
                    "url": f"http://{tailscale_ip}:{port}",
                    "token": token,
                    "note": (
                        "ComfyUI media endpoint over Tailscale. POST workflows "
                        "to <url>/prompt. Sovereign: operator-owned node only."
                    ),
                }
                logger.info(
                    "rental session %s (media/comfyui): container %s on port %d",
                    session_id, container_id, port,
                )

            else:  # api_endpoint
                token = _random_token()
                model_name = model or "llama3"
                container = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: client.containers.run(
                        _OLLAMA_IMAGE,
                        detach=True,
                        remove=False,
                        name=f"hadcd-session-{session_id[:8]}",
                        network=_SESSION_NETWORK,
                        ports={"11434/tcp": (self._publish_host, port)},
                        cap_drop=["ALL"],
                        security_opt=["no-new-privileges:true"],
                        environment={
                            "OLLAMA_HOST": "0.0.0.0",
                            "HADCD_SESSION_TOKEN": token,
                        },
                        # Phase 26: shared model store — a model pulled by
                        # any session stays warm for every later session.
                        volumes={
                            _OLLAMA_MODELS_VOLUME: {
                                "bind": "/root/.ollama",
                                "mode": "rw",
                            }
                        },
                        device_requests=[
                            docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])
                        ] if _gpu_available(client) else [],
                    )
                )
                container_id = container.id[:12]
                tailscale_ip = await _get_tailscale_ip()
                connection_info = {
                    "url": f"http://{tailscale_ip}:{port}/v1",
                    "token": token,
                    "model": model_name,
                    "note": (
                        "OpenAI-compatible endpoint. "
                        "Set OPENAI_BASE_URL=<url> and OPENAI_API_KEY=<token> in your app."
                    ),
                }
                logger.info(
                    "rental session %s (api_endpoint/%s): container %s on port %d",
                    session_id, model_name, container_id, port,
                )
                # Phase 26: the model now lives in the shared volume —
                # report it so the dispatcher can route warm.
                if self._on_model_cached is not None:
                    try:
                        self._on_model_cached(model_name)
                    except Exception:
                        logger.exception(
                            "on_model_cached callback failed for %s", model_name
                        )

            self._active[session_id] = {
                "container_id": container_id,
                "port": port,
                "type": stype,
            }

            # Report activation to dispatcher.
            await self._post_activate(session_id, connection_info)

        except Exception:
            logger.exception("failed to start rental session container %s", session_id)

    async def _stop_session(self, session_id: str) -> None:
        """Stop and remove the session container, then report to dispatcher."""
        info = self._active.pop(session_id, None)
        if info is None:
            return

        try:
            import docker  # type: ignore[import]
            client = docker.from_env()
            containers = client.containers.list(
                filters={"name": f"hadcd-session-{session_id[:8]}"}
            )
            for c in containers:
                try:
                    c.stop(timeout=10)
                    c.remove()
                    logger.info(
                        "rental session %s: container %s stopped and removed",
                        session_id, info["container_id"],
                    )
                except Exception:
                    logger.exception("error stopping container for session %s", session_id)
        except Exception:
            logger.exception("docker error stopping session %s", session_id)

        # Always report stopped — even if Docker raised, the session is done.
        await self._post_stopped(session_id)

    # ── Dispatcher callbacks ──────────────────────────────────────────────────

    async def _post_activate(
        self, session_id: str, connection_info: dict
    ) -> None:
        url = (
            f"{self._dispatcher_url}/api/nodes/{self._node_id}"
            f"/sessions/{session_id}/activate"
        )
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.post(
                    url,
                    json={"connection_info": connection_info},
                    headers={"Authorization": f"Bearer {self._node_token}"},
                )
            if resp.status_code == 200:
                logger.info("rental session %s activated on dispatcher", session_id)
            else:
                logger.warning(
                    "activate POST for session %s returned %d: %s",
                    session_id, resp.status_code, resp.text[:200],
                )
        except Exception:
            logger.exception("activate POST failed for session %s", session_id)

    async def _post_stopped(self, session_id: str) -> None:
        url = (
            f"{self._dispatcher_url}/api/nodes/{self._node_id}"
            f"/sessions/{session_id}/stopped"
        )
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.post(
                    url,
                    headers={"Authorization": f"Bearer {self._node_token}"},
                )
            if resp.status_code == 200:
                logger.info("rental session %s marked stopped on dispatcher", session_id)
            else:
                logger.warning(
                    "stopped POST for session %s returned %d: %s",
                    session_id, resp.status_code, resp.text[:200],
                )
        except Exception:
            logger.exception("stopped POST failed for session %s", session_id)


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _ensure_network(client: Any) -> None:
    """Create the rental session Docker bridge network if it doesn't exist."""
    try:
        existing = [n.name for n in client.networks.list()]
        if _SESSION_NETWORK not in existing:
            client.networks.create(_SESSION_NETWORK, driver="bridge")
            logger.info("created Docker network %s", _SESSION_NETWORK)
    except Exception:
        logger.debug("could not ensure Docker network %s", _SESSION_NETWORK)


def _gpu_available(client: Any) -> bool:
    """Return True if nvidia-container-toolkit is present on the host."""
    try:
        info = client.info()
        runtimes = info.get("Runtimes", {})
        return "nvidia" in runtimes
    except Exception:
        return False


async def _get_tailscale_ip() -> str:
    """Return the node's Tailscale IP (100.x.x.x), or '127.0.0.1' as fallback."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tailscale", "ip", "--4",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        ip = stdout.decode().strip()
        if ip:
            return ip
    except Exception:
        pass
    return "127.0.0.1"
