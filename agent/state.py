"""Agent identity persistence.

The agent's `node_id` and bearer `node_token` are what authorise it
to act as a specific node. Losing them means the agent must re-enroll
under a fresh UUID (and the operator must decommission the old node
row). Persisting them across restarts is therefore important.

JSON file because the state is small (two strings) and easy to inspect.
Writes are atomic (write to a tmp file, then rename) so a crash mid-
write cannot leave a corrupted half-state file.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger("hadcd.agent.state")


class AgentState:
    """Holds the agent's persistent identity; reads/writes one JSON file."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.node_id: str | None = None
        self.node_token: str | None = None
        # Phase 22 — the thermostat setpoint that was active before the
        # building-hub vacancy override dropped the room to setback.
        # Persisted so a restarted agent can still restore the original
        # temperature when the manager re-opens the room.
        self.saved_setpoint_c: float | None = None
        # Phase 26 — model names present in this node's shared Ollama
        # model volume (each api_endpoint session that serves a model
        # leaves it cached there). Persisted so a restarted agent still
        # reports an accurate warm-model list on its first heartbeat.
        self.cached_models: list[str] = []

    @property
    def enrolled(self) -> bool:
        return self.node_id is not None and self.node_token is not None

    def load(self) -> bool:
        """Populate `node_id` and `node_token` from the file.

        Returns True if a valid identity was loaded, False if the file
        does not exist or is malformed (in which case the agent should
        enroll afresh). Treats a malformed file as "no identity" rather
        than crashing — a bad state file should not stop the agent.
        """
        if not self.path.exists():
            return False
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("could not read state file %s: %s", self.path, exc)
            return False
        node_id = data.get("node_id")
        node_token = data.get("node_token")
        if isinstance(node_id, str) and isinstance(node_token, str):
            self.node_id = node_id
            self.node_token = node_token
            saved = data.get("saved_setpoint_c")
            self.saved_setpoint_c = (
                float(saved) if isinstance(saved, (int, float)) else None
            )
            cached = data.get("cached_models")
            self.cached_models = (
                [m for m in cached if isinstance(m, str)]
                if isinstance(cached, list)
                else []
            )
            return True
        return False

    def record_cached_model(self, model: str) -> None:
        """Record that `model` now sits in the local model store.

        Idempotent; persists immediately so the warm-model list survives
        an agent restart between heartbeats.
        """
        if model and model not in self.cached_models:
            self.cached_models.append(model)
            self.save()

    def save(self) -> None:
        """Persist the current identity to disk atomically."""
        if not self.enrolled:
            raise RuntimeError("cannot save state without an enrolled identity")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # tempfile.NamedTemporaryFile + os.replace gives us the write-
        # then-rename atomicity guarantee on POSIX and Windows.
        fd, tmp = tempfile.mkstemp(
            prefix=".state-", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            payload: dict = {
                "node_id": self.node_id,
                "node_token": self.node_token,
            }
            if self.saved_setpoint_c is not None:
                payload["saved_setpoint_c"] = self.saved_setpoint_c
            if self.cached_models:
                payload["cached_models"] = self.cached_models
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, self.path)
        except Exception:
            # Don't leave a stray .tmp file if something goes wrong.
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass
            raise
