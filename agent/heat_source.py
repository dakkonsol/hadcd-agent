"""Pluggable heat-demand sources for the node agent.

A `HeatSource` produces a `HeatReading` on demand — the agent's
demand loop calls `await source.read()` once per tick and posts what
comes back. Adapters shipping:

* **FileHeatSource** — reads a JSON file the building's BMS (or a
  bridging script) writes. This is the original shape; a building
  integrated against 7b keeps working.

* **HttpHeatSource** — polls a URL that returns the same JSON shape.
  Suitable for BMS systems with a REST face, or for a bridging
  service that translates a vendor protocol (BACnet, Modbus) into
  JSON.

* **EcobeeHeatSource (Phase 8c)** — talks to Ecobee's Developer API
  directly. No bridging script needed. Polls a thermostat's
  `equipmentStatus`, `runtime.actualTemperature`, and
  `runtime.desiredHeat`. When the thermostat is calling for heat
  (i.e. its HVAC is actively running heat), the adapter emits the
  configured `ECOBEE_DEMAND_WHEN_HEATING_KW`; otherwise zero. One-
  time OAuth setup is handled by `python -m agent.ecobee_setup`;
  the adapter then reads the persisted refresh token from the state
  file and rotates it on each token refresh.

* **HomeAssistantHeatSource (Phase 8d)** — polls a local Home
  Assistant instance's REST API. Reads a climate entity's
  `hvac_action` attribute; when it is ``"heating"`` the adapter
  emits ``HA_DEMAND_WHEN_HEATING_KW``, otherwise zero. Also forwards
  ``current_temperature`` and ``temperature`` (setpoint) for the
  operator UI. Authentication is a long-lived access token generated
  from the HA profile page — no developer registration required.
  Works with any thermostat brand HA supports (Ecobee, Nest,
  Honeywell, Z-Wave, Zigbee, etc.).

Real BMS bridging (BACnet/Modbus) is a future adapter. The point of
the abstract base class is that the rest of the agent does not care
which one is in use — only `read()` matters.

JSON shape (all fields optional except `measured_kw`):

    {
      "measured_kw":          8.5,     // current heat demand in kW
      "setpoint_c":           21.0,    // thermostat setpoint
      "room_temp_c":          19.3,    // current room temperature
      "expected_window_sec":  1800     // how long demand is expected
    }

Read failures are not fatal: every adapter returns a zero-kW reading
on any error (with a single log line). The dispatcher then simply
does not offload to this node until the BMS recovers — the
fail-quiet behaviour avoids cascading a transient BMS hiccup into
"node is broken."
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from agent.config import AgentSettings

logger = logging.getLogger("hadcd.agent.bms")


# --- data ------------------------------------------------------------


@dataclass
class HeatReading:
    """One snapshot of the building's heat state."""

    measured_kw: float
    setpoint_c: float | None
    room_temp_c: float | None
    expected_window_sec: int | None

    def to_api_payload(self) -> dict:
        return {
            "measured_kw": self.measured_kw,
            "expected_window_sec": self.expected_window_sec,
            "setpoint_c": self.setpoint_c,
            "room_temp_c": self.room_temp_c,
        }


_ZERO = HeatReading(
    measured_kw=0.0,
    setpoint_c=None,
    room_temp_c=None,
    expected_window_sec=None,
)


def parse_reading(data: object, default_window_sec: int) -> HeatReading | None:
    """Coerce a parsed-JSON payload into a HeatReading.

    Returns None if the payload is not a dict with a numeric
    `measured_kw`. Common to both adapters so the shape contract has
    one definition.
    """
    if not isinstance(data, dict):
        return None
    measured = data.get("measured_kw")
    if not isinstance(measured, (int, float)):
        return None
    window = data.get("expected_window_sec")
    if not isinstance(window, int) or window <= 0:
        window = default_window_sec
    return HeatReading(
        measured_kw=float(measured),
        setpoint_c=_optional_float(data.get("setpoint_c")),
        room_temp_c=_optional_float(data.get("room_temp_c")),
        expected_window_sec=window,
    )


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


# --- interface -------------------------------------------------------


class HeatSource(ABC):
    """A source of current heat-demand readings."""

    @abstractmethod
    async def read(self) -> HeatReading:
        """Return the building's current heat-demand reading.

        Implementations must not raise on a transient BMS failure —
        they should return a zero-kW HeatReading (and log) instead.
        The agent's demand loop relies on this; an exception would
        leave a tick un-posted.
        """

    async def set_setpoint(self, temp_c: float) -> bool:
        """Write a new thermostat setpoint, if this source supports it.

        Phase 22 — used by the building-hub vacancy override to drop a
        vacant room to its setback temperature (and restore it on
        check-in). Returns True on success. The default supports
        nothing: file/http sources are read-only telemetry feeds, and
        the agent falls back to demand-zeroing alone.
        """
        return False

    async def aclose(self) -> None:
        """Release any resources (HTTP client, etc.). Default no-op."""


# --- file adapter ----------------------------------------------------


class FileHeatSource(HeatSource):
    """Reads heat demand from a JSON file the BMS (or a bridge) writes."""

    def __init__(self, path: str, default_window_sec: int) -> None:
        self.path = Path(path)
        self.default_window_sec = default_window_sec
        # Track whether we have already complained about a missing/bad
        # file so the agent doesn't spam its own log every tick.
        self._warned_missing = False
        self._warned_bad = False

    async def read(self) -> HeatReading:
        if not self.path.exists():
            if not self._warned_missing:
                logger.warning(
                    "BMS file %s not present — posting zero demand "
                    "until it appears",
                    self.path,
                )
                self._warned_missing = True
            return _ZERO
        # File exists — reset the missing-file gate.
        self._warned_missing = False
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            if not self._warned_bad:
                logger.warning("could not read BMS file %s: %s", self.path, exc)
                self._warned_bad = True
            return _ZERO
        reading = parse_reading(data, self.default_window_sec)
        if reading is None:
            if not self._warned_bad:
                logger.warning(
                    "BMS file %s missing or non-numeric 'measured_kw' — "
                    "ignoring",
                    self.path,
                )
                self._warned_bad = True
            return _ZERO
        self._warned_bad = False
        return reading


# --- HTTP adapter ----------------------------------------------------


class HttpHeatSource(HeatSource):
    """Polls a URL that returns the BMS JSON shape.

    Suitable for BMS systems with a REST face, or for a bridging
    service that translates BACnet / Modbus into JSON. Owns its own
    `httpx.AsyncClient` so the agent's backend bearer cannot leak to
    the BMS host.
    """

    def __init__(
        self,
        url: str,
        default_window_sec: int,
        timeout_sec: float = 5.0,
        auth_header: str | None = None,
    ) -> None:
        self.url = url
        self.default_window_sec = default_window_sec
        headers = {}
        if auth_header:
            headers["Authorization"] = auth_header
        self._client = httpx.AsyncClient(timeout=timeout_sec, headers=headers)
        self._warned = False

    async def read(self) -> HeatReading:
        try:
            resp = await self._client.get(self.url)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            if not self._warned:
                logger.warning(
                    "BMS HTTP source %s unavailable: %s", self.url, exc
                )
                self._warned = True
            return _ZERO
        reading = parse_reading(data, self.default_window_sec)
        if reading is None:
            if not self._warned:
                logger.warning(
                    "BMS HTTP source %s returned an unexpected shape "
                    "(missing or non-numeric 'measured_kw')",
                    self.url,
                )
                self._warned = True
            return _ZERO
        self._warned = False
        return reading

    async def aclose(self) -> None:
        await self._client.aclose()


# --- Ecobee adapter --------------------------------------------------


# Equipment-status tokens (from /1/thermostat) that mean "the
# thermostat is actively running heat right now." compCool* are cooling,
# not heat, so they don't count even though they share the "comp"
# prefix. fan / humidifier / ventilator are auxiliary and not heat
# delivery.
ECOBEE_HEATING_STATES = frozenset(
    {
        "heatPump",
        "heatPump2",
        "heatPump3",
        "auxHeat1",
        "auxHeat2",
        "auxHeat3",
    }
)


def _ecobee_tenths_f_to_c(value: object) -> float | None:
    """Convert Ecobee's `tenths of degrees Fahrenheit` to Celsius."""
    if not isinstance(value, (int, float)):
        return None
    return ((float(value) / 10.0) - 32.0) * 5.0 / 9.0


class EcobeeHeatSource(HeatSource):
    """Polls Ecobee's Developer API for current heat state.

    Reports `measured_kw == demand_when_heating_kw` when the
    thermostat's `equipmentStatus` shows any of the heating states
    (see ECOBEE_HEATING_STATES); zero otherwise. Also surfaces the
    setpoint and current room temperature so the operator UI's node
    detail view has them.

    OAuth flow:
      - One-time setup (`python -m agent.ecobee_setup`) obtains and
        persists a refresh token + thermostat id to the state file.
      - On read, the adapter trades the refresh token for an access
        token (cached for ~1h). Ecobee rotates refresh tokens on
        every refresh; the new one is atomically written back to the
        state file so the agent survives restarts.

    Failsafe (the HeatSource contract): on ANY error (token refresh
    failure, network error, malformed response, missing thermostat),
    return a zero-kW reading and log once. The dispatcher then
    simply doesn't offload to this node until Ecobee recovers.
    """

    AUTH_URL = "https://api.ecobee.com/token"
    THERMOSTAT_URL = "https://api.ecobee.com/1/thermostat"

    def __init__(
        self,
        api_key: str,
        state_file: str,
        thermostat_id: str,
        demand_when_heating_kw: float,
        default_window_sec: int,
        timeout_sec: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.state_file = Path(state_file)
        self.thermostat_id = thermostat_id
        self.demand_when_heating_kw = demand_when_heating_kw
        self.default_window_sec = default_window_sec
        self._client = httpx.AsyncClient(timeout=timeout_sec)
        self._access_token: str | None = None
        self._access_expires_at: float = 0.0
        self._refresh_token: str = self._load_refresh_token()
        self._warned = False

    def _load_refresh_token(self) -> str:
        if not self.state_file.exists():
            raise BMSConfigError(
                f"Ecobee state file not found: {self.state_file}. "
                f"Run 'python -m agent.ecobee_setup' first to complete "
                f"the one-time OAuth PIN flow."
            )
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise BMSConfigError(
                f"Ecobee state file {self.state_file} is unreadable: {exc}"
            ) from exc
        token = data.get("refresh_token")
        if not isinstance(token, str) or not token:
            raise BMSConfigError(
                f"Ecobee state file {self.state_file} is missing "
                f"'refresh_token'. Re-run the setup flow."
            )
        return token

    def _persist_refresh_token(self, new_token: str) -> None:
        """Atomic write of a rotated refresh token + thermostat id."""
        tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "refresh_token": new_token,
                    "thermostat_id": self.thermostat_id,
                }
            )
        )
        tmp.replace(self.state_file)

    async def _ensure_access_token(self) -> None:
        """Refresh the access token if it's expired (or about to)."""
        now = time.time()
        # 60-second safety margin so we don't race the expiry exactly.
        if self._access_token and now < self._access_expires_at - 60:
            return

        resp = await self._client.post(
            self.AUTH_URL,
            params={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self.api_key,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        self._access_token = data["access_token"]
        # Ecobee returns expires_in in seconds; cache slightly less.
        self._access_expires_at = now + int(data.get("expires_in", 3600))

        # Ecobee rotates the refresh token on each use. Persist
        # immediately so a process restart doesn't invalidate us.
        new_refresh = data.get("refresh_token")
        if new_refresh and new_refresh != self._refresh_token:
            self._refresh_token = new_refresh
            self._persist_refresh_token(new_refresh)

    async def read(self) -> HeatReading:
        try:
            await self._ensure_access_token()
            selection = json.dumps(
                {
                    "selection": {
                        "selectionType": "thermostats",
                        "selectionMatch": self.thermostat_id,
                        "includeRuntime": True,
                        "includeEquipmentStatus": True,
                    }
                },
                separators=(",", ":"),
            )
            resp = await self._client.get(
                self.THERMOSTAT_URL,
                params={"json": selection},
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            if not self._warned:
                logger.warning("Ecobee read failed: %s", exc)
                self._warned = True
            return _ZERO

        thermostats = data.get("thermostatList") or []
        if not thermostats:
            if not self._warned:
                logger.warning(
                    "Ecobee returned no thermostats matching id %s",
                    self.thermostat_id,
                )
                self._warned = True
            return _ZERO

        self._warned = False
        thermostat = thermostats[0]

        equipment_raw = thermostat.get("equipmentStatus") or ""
        equipment_states = {
            piece.strip()
            for piece in equipment_raw.split(",")
            if piece.strip()
        }
        is_heating = bool(equipment_states & ECOBEE_HEATING_STATES)

        runtime = thermostat.get("runtime") or {}
        room_temp_c = _ecobee_tenths_f_to_c(runtime.get("actualTemperature"))
        setpoint_c = _ecobee_tenths_f_to_c(runtime.get("desiredHeat"))

        return HeatReading(
            measured_kw=self.demand_when_heating_kw if is_heating else 0.0,
            setpoint_c=setpoint_c,
            room_temp_c=room_temp_c,
            # No expected_window_sec from Ecobee directly — the
            # thermostat doesn't expose "I'll keep wanting heat for N
            # more seconds." Fall back to the operator-configured
            # default so the dispatcher's window matching has
            # something to work with.
            expected_window_sec=(
                self.default_window_sec if is_heating else None
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


# --- Home Assistant adapter ------------------------------------------


class HomeAssistantHeatSource(HeatSource):
    """Polls a local Home Assistant instance for thermostat state.

    Calls ``GET /api/states/<entity_id>`` on HA's local REST API and
    inspects the climate entity's ``hvac_action`` attribute:

    * ``"heating"``           → reports ``demand_when_heating_kw``
    * anything else / absent  → reports zero (idle, cooling, off)

    Falls back to checking ``state == "heat"`` when ``hvac_action`` is
    not present — covers older HA integrations that do not expose it.

    Temperature values (``current_temperature`` and ``temperature``)
    are forwarded as room_temp_c and setpoint_c. If HA is configured
    in Fahrenheit set ``HA_TEMPERATURE_UNIT=F``; the adapter converts
    to Celsius automatically. Default is ``"C"`` (metric), which is
    correct for Canadian deployments and most HA installs.

    Authentication: generate a **long-lived access token** from the HA
    user profile page (Settings → Your profile → Long-lived access
    tokens). No developer registration, no OAuth PIN flow, no cloud
    account — just your local HA password.
    """

    # HA REST endpoint path for a single entity.
    _STATES_PATH = "/api/states/{entity_id}"
    # HA service call that writes a climate entity's target temperature.
    _SET_TEMP_PATH = "/api/services/climate/set_temperature"

    def __init__(
        self,
        ha_url: str,
        token: str,
        entity_id: str,
        demand_when_heating_kw: float,
        default_window_sec: int,
        timeout_sec: float = 5.0,
        temperature_unit: str = "C",
    ) -> None:
        self.entity_id = entity_id
        self.demand_when_heating_kw = demand_when_heating_kw
        self.default_window_sec = default_window_sec
        self._temperature_unit = temperature_unit.upper().strip()
        self._client = httpx.AsyncClient(
            base_url=ha_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_sec,
        )
        self._warned = False

    def _to_celsius(self, value: object) -> float | None:
        """Convert a temperature value to Celsius.

        If ``HA_TEMPERATURE_UNIT=F`` the value is treated as Fahrenheit
        and converted; otherwise it is returned as-is (assumed Celsius).
        Returns ``None`` for non-numeric input.
        """
        if not isinstance(value, (int, float)):
            return None
        f = float(value)
        if self._temperature_unit == "F":
            return (f - 32.0) * 5.0 / 9.0
        return f

    async def read(self) -> HeatReading:
        url = self._STATES_PATH.format(entity_id=self.entity_id)
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            if not self._warned:
                logger.warning(
                    "Home Assistant BMS: could not read entity %s: %s",
                    self.entity_id,
                    exc,
                )
                self._warned = True
            return _ZERO

        if not isinstance(data, dict):
            if not self._warned:
                logger.warning(
                    "Home Assistant BMS: unexpected response shape for %s",
                    self.entity_id,
                )
                self._warned = True
            return _ZERO

        self._warned = False

        attrs = data.get("attributes") or {}

        # Prefer hvac_action (actual equipment state) over state (mode
        # setting). Some integrations don't provide hvac_action — for
        # those, treat state=="heat" as a weak "probably heating" signal.
        hvac_action = attrs.get("hvac_action")
        if hvac_action is not None:
            is_heating = str(hvac_action).lower() == "heating"
        else:
            # Fallback: entity state "heat" means the *mode* is set to
            # heat; we can't tell if the equipment is actually running,
            # so err on the side of reporting demand.
            is_heating = str(data.get("state", "")).lower() in {
                "heat", "heating"
            }

        room_temp_c = self._to_celsius(attrs.get("current_temperature"))
        setpoint_c = self._to_celsius(attrs.get("temperature"))

        return HeatReading(
            measured_kw=self.demand_when_heating_kw if is_heating else 0.0,
            setpoint_c=setpoint_c,
            room_temp_c=room_temp_c,
            expected_window_sec=self.default_window_sec if is_heating else None,
        )

    async def set_setpoint(self, temp_c: float) -> bool:
        """Write the thermostat's target temperature via HA's climate
        service. temp_c is always Celsius; converted when HA is
        configured in Fahrenheit (mirror of the read-side conversion).
        """
        temperature = temp_c
        if self._temperature_unit == "F":
            temperature = temp_c * 9.0 / 5.0 + 32.0
        try:
            resp = await self._client.post(
                self._SET_TEMP_PATH,
                json={
                    "entity_id": self.entity_id,
                    "temperature": round(temperature, 1),
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning(
                "Home Assistant BMS: could not set setpoint on %s: %s",
                self.entity_id,
                exc,
            )
            return False
        return True

    async def aclose(self) -> None:
        await self._client.aclose()


# --- factory ---------------------------------------------------------


class BMSConfigError(RuntimeError):
    """Raised when BMS settings are inconsistent (e.g. http with no URL)."""


def build_source(settings: AgentSettings) -> HeatSource:
    """Construct the configured heat source.

    Raises BMSConfigError for misconfiguration; the agent's CLI
    surfaces it as a clean exit-2 startup error.
    """
    kind = (settings.bms_source or "file").lower()
    if kind == "file":
        return FileHeatSource(
            path=settings.bms_file,
            default_window_sec=settings.bms_default_window_sec,
        )
    if kind == "http":
        if not settings.bms_http_url:
            raise BMSConfigError(
                "BMS_SOURCE=http but BMS_HTTP_URL is empty"
            )
        return HttpHeatSource(
            url=settings.bms_http_url,
            default_window_sec=settings.bms_default_window_sec,
            timeout_sec=settings.bms_http_timeout_sec,
            auth_header=(settings.bms_http_auth_header or None),
        )
    if kind == "ecobee":
        if not settings.ecobee_api_key:
            raise BMSConfigError(
                "BMS_SOURCE=ecobee but ECOBEE_API_KEY is empty"
            )
        if not settings.ecobee_thermostat_id:
            raise BMSConfigError(
                "BMS_SOURCE=ecobee but ECOBEE_THERMOSTAT_ID is empty. "
                "Run 'python -m agent.ecobee_setup' to discover yours."
            )
        return EcobeeHeatSource(
            api_key=settings.ecobee_api_key,
            state_file=settings.ecobee_state_file,
            thermostat_id=settings.ecobee_thermostat_id,
            demand_when_heating_kw=settings.ecobee_demand_when_heating_kw,
            default_window_sec=settings.bms_default_window_sec,
            timeout_sec=settings.ecobee_timeout_sec,
        )
    if kind == "homeassistant":
        if not settings.ha_token:
            raise BMSConfigError(
                "BMS_SOURCE=homeassistant but HA_TOKEN is empty. "
                "Generate a long-lived access token from the Home Assistant "
                "profile page (Settings → Your profile → Long-lived access "
                "tokens) and set it here."
            )
        if not settings.ha_entity_id:
            raise BMSConfigError(
                "BMS_SOURCE=homeassistant but HA_ENTITY_ID is empty. "
                "Set it to your thermostat's climate entity, e.g. "
                "'climate.living_room'. Find it in HA under "
                "Settings → Devices & Services → Entities."
            )
        return HomeAssistantHeatSource(
            ha_url=settings.ha_url,
            token=settings.ha_token,
            entity_id=settings.ha_entity_id,
            demand_when_heating_kw=settings.ha_demand_when_heating_kw,
            default_window_sec=settings.bms_default_window_sec,
            timeout_sec=settings.ha_timeout_sec,
            temperature_unit=settings.ha_temperature_unit,
        )
    raise BMSConfigError(
        f"unknown BMS_SOURCE '{settings.bms_source}' "
        f"(expected 'file', 'http', 'ecobee', or 'homeassistant')"
    )
