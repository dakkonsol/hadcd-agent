"""Pluggable interactive-session detection for the node agent (10g).

A `SessionSource` answers one question on demand: *is the user
currently using this machine interactively right now?* The agent's
heartbeat loop calls `await source.is_active()` each tick and reports
the result to the backend, which uses it to pause fill-tier work
(mining, synthetic heat-fill) so it doesn't compete with the user.

Adapters:

* **NullSessionSource** — always returns False. The default, for
  heaters where no session detector is configured. Fill tiers run
  whenever heat is demanded.

* **SunshineSessionSource** — polls a local Sunshine instance's
  `GET /api/connections` endpoint. A non-empty connection list means
  a Moonlight client is actively streaming (Mode 2 / Mode 3 of the
  space-heater design). Sunshine's admin username/password double as
  the API credentials (HTTP Basic); Sunshine serves over HTTPS with
  a self-signed certificate, so the client does not verify TLS — it's
  a localhost call to software the operator installed themselves.

Same fail-quiet philosophy as the heat sources, but with a subtle
difference in *direction*: on a detection error we preserve the last
known value rather than flipping to a default, because flapping the
fill-tier gate on a transient Sunshine hiccup would be worse than a
brief staleness. The first-ever reading (before any success) defaults
to False so a permanently-broken Sunshine doesn't block fill forever.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from agent.config import AgentSettings

logger = logging.getLogger("hadcd.agent.session")


class SessionSource(ABC):
    """A detector of current interactive-session state."""

    @abstractmethod
    async def is_active(self) -> bool:
        """Return True if the user is interactively using the machine.

        Implementations must not raise on a transient failure — they
        should return a sensible fallback (and log) instead. The
        agent's heartbeat loop relies on this; an exception would
        leave a tick un-reported.
        """

    async def aclose(self) -> None:
        """Release any resources (HTTP client, etc.). Default no-op."""


class NullSessionSource(SessionSource):
    """Always reports 'no session'. The default when no detector is
    configured — fill tiers run whenever heat is demanded."""

    async def is_active(self) -> bool:
        return False


class SunshineSessionSource(SessionSource):
    """Detects an active stream by polling Sunshine's connections API."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout_sec: float = 5.0,
    ) -> None:
        self.connections_url = base_url.rstrip("/") + "/api/connections"
        # Sunshine serves HTTPS with a self-signed cert on localhost;
        # verifying it would just fail. This is a loopback call to
        # operator-installed software, so skipping verification is
        # acceptable here (and only here).
        self._client = httpx.AsyncClient(
            timeout=timeout_sec,
            verify=False,
            auth=(username, password),
        )
        self._last_known = False
        self._warned = False

    async def is_active(self) -> bool:
        try:
            resp = await self._client.get(self.connections_url)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            if not self._warned:
                logger.warning(
                    "Sunshine connections poll failed (%s) — holding "
                    "last known session state (%s)",
                    exc,
                    self._last_known,
                )
                self._warned = True
            return self._last_known

        self._warned = False
        # `GET /api/connections` returns a list of current connections;
        # a non-empty list means a client is actively streaming. Be
        # tolerant of either a bare list or a {"connections": [...]}
        # envelope across Sunshine versions.
        connections = data
        if isinstance(data, dict):
            connections = (
                data.get("connections")
                or data.get("clients")
                or []
            )
        active = bool(connections)
        self._last_known = active
        return active

    async def aclose(self) -> None:
        await self._client.aclose()


class SessionConfigError(RuntimeError):
    """Raised when session-source settings are inconsistent."""


def build_session_source(settings: AgentSettings) -> SessionSource:
    """Construct the configured session source.

    Raises SessionConfigError for misconfiguration; the agent's CLI
    surfaces it as a clean startup error.
    """
    kind = (settings.session_source or "none").lower()
    if kind in ("none", "", "null"):
        return NullSessionSource()
    if kind == "sunshine":
        if not settings.sunshine_password:
            raise SessionConfigError(
                "SESSION_SOURCE=sunshine but SUNSHINE_PASSWORD is empty. "
                "Set it to your Sunshine admin password (Sunshine uses "
                "that as the API credential)."
            )
        return SunshineSessionSource(
            base_url=settings.sunshine_url,
            username=settings.sunshine_username,
            password=settings.sunshine_password,
            timeout_sec=settings.sunshine_timeout_sec,
        )
    raise SessionConfigError(
        f"unknown SESSION_SOURCE '{settings.session_source}' "
        f"(expected 'none' or 'sunshine')"
    )
