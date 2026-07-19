"""Unit tests for the heat-source adapters.

Both adapters share a "never raise on transient failure" contract:
on any error they must return a zero-kW HeatReading and log. These
tests pin that contract for the common failure modes.

The HTTP adapter is tested with httpx.MockTransport so no real
network or fake server is needed.
"""

from __future__ import annotations

import json

import httpx
import pytest

from agent.config import AgentSettings
from agent.heat_source import (
    ECOBEE_HEATING_STATES,
    BMSConfigError,
    EcobeeHeatSource,
    FileHeatSource,
    HomeAssistantHeatSource,
    HttpHeatSource,
    _ecobee_tenths_f_to_c,
    build_source,
    parse_reading,
)


# --- parse_reading ----------------------------------------------------


def test_parse_reading_minimal():
    r = parse_reading({"measured_kw": 5.0}, default_window_sec=1800)
    assert r is not None
    assert r.measured_kw == 5.0
    assert r.setpoint_c is None
    assert r.room_temp_c is None
    assert r.expected_window_sec == 1800


def test_parse_reading_full():
    r = parse_reading(
        {
            "measured_kw": 8.5,
            "setpoint_c": 21.0,
            "room_temp_c": 19.3,
            "expected_window_sec": 600,
        },
        default_window_sec=1800,
    )
    assert r is not None
    assert r.measured_kw == 8.5
    assert r.setpoint_c == 21.0
    assert r.room_temp_c == 19.3
    assert r.expected_window_sec == 600


def test_parse_reading_rejects_non_dict():
    assert parse_reading([1, 2, 3], default_window_sec=1800) is None
    assert parse_reading("nope", default_window_sec=1800) is None
    assert parse_reading(None, default_window_sec=1800) is None


def test_parse_reading_rejects_missing_or_bad_kw():
    assert parse_reading({}, default_window_sec=1800) is None
    assert parse_reading({"measured_kw": "lots"}, 1800) is None


def test_parse_reading_uses_default_window_when_invalid():
    r = parse_reading(
        {"measured_kw": 3.0, "expected_window_sec": -10},
        default_window_sec=1234,
    )
    assert r.expected_window_sec == 1234


# --- FileHeatSource ---------------------------------------------------


@pytest.mark.asyncio
async def test_file_source_returns_zero_when_missing(tmp_path):
    src = FileHeatSource(str(tmp_path / "bms.json"), default_window_sec=1800)
    reading = await src.read()
    assert reading.measured_kw == 0.0
    assert reading.expected_window_sec is None


@pytest.mark.asyncio
async def test_file_source_reads_valid_json(tmp_path):
    p = tmp_path / "bms.json"
    p.write_text(
        json.dumps(
            {
                "measured_kw": 9.0,
                "setpoint_c": 22.0,
                "room_temp_c": 18.5,
                "expected_window_sec": 900,
            }
        )
    )
    src = FileHeatSource(str(p), default_window_sec=1800)
    reading = await src.read()
    assert reading.measured_kw == 9.0
    assert reading.setpoint_c == 22.0
    assert reading.expected_window_sec == 900


@pytest.mark.asyncio
async def test_file_source_handles_malformed_json(tmp_path):
    p = tmp_path / "bms.json"
    p.write_text("{not valid")
    src = FileHeatSource(str(p), default_window_sec=1800)
    reading = await src.read()
    assert reading.measured_kw == 0.0


@pytest.mark.asyncio
async def test_file_source_handles_missing_measured_kw(tmp_path):
    p = tmp_path / "bms.json"
    p.write_text(json.dumps({"setpoint_c": 21.0}))
    src = FileHeatSource(str(p), default_window_sec=1800)
    reading = await src.read()
    assert reading.measured_kw == 0.0


@pytest.mark.asyncio
async def test_file_source_applies_default_window(tmp_path):
    p = tmp_path / "bms.json"
    p.write_text(json.dumps({"measured_kw": 4.2}))
    src = FileHeatSource(str(p), default_window_sec=1500)
    reading = await src.read()
    assert reading.expected_window_sec == 1500


# --- HttpHeatSource ---------------------------------------------------


def _http_source_with_handler(handler, **kwargs):
    """Build an HttpHeatSource backed by httpx MockTransport."""
    src = HttpHeatSource("http://bms.test/demand", default_window_sec=1800, **kwargs)
    # Replace the real client with one routed through the mock handler.
    transport = httpx.MockTransport(handler)
    src._client = httpx.AsyncClient(transport=transport, headers=src._client.headers)
    return src


@pytest.mark.asyncio
async def test_http_source_reads_valid_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "measured_kw": 6.0,
                "setpoint_c": 20.0,
                "expected_window_sec": 1200,
            },
        )

    src = _http_source_with_handler(handler)
    try:
        reading = await src.read()
    finally:
        await src.aclose()
    assert reading.measured_kw == 6.0
    assert reading.setpoint_c == 20.0
    assert reading.expected_window_sec == 1200


@pytest.mark.asyncio
async def test_http_source_returns_zero_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream broken")

    src = _http_source_with_handler(handler)
    try:
        reading = await src.read()
    finally:
        await src.aclose()
    assert reading.measured_kw == 0.0


@pytest.mark.asyncio
async def test_http_source_returns_zero_on_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    src = _http_source_with_handler(handler)
    try:
        reading = await src.read()
    finally:
        await src.aclose()
    assert reading.measured_kw == 0.0


@pytest.mark.asyncio
async def test_http_source_returns_zero_on_unexpected_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not a dict"])

    src = _http_source_with_handler(handler)
    try:
        reading = await src.read()
    finally:
        await src.aclose()
    assert reading.measured_kw == 0.0


@pytest.mark.asyncio
async def test_http_source_sends_auth_header_when_configured():
    seen_headers: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"measured_kw": 1.0})

    src = _http_source_with_handler(handler, auth_header="Bearer secret")
    try:
        await src.read()
    finally:
        await src.aclose()
    assert seen_headers["auth"] == "Bearer secret"


# --- build_source factory ---------------------------------------------


def test_build_source_default_is_file(tmp_path):
    settings = AgentSettings(
        enrollment_tokens="x",
        bms_file=str(tmp_path / "bms.json"),
    )
    src = build_source(settings)
    assert isinstance(src, FileHeatSource)


def test_build_source_http_requires_url():
    settings = AgentSettings(enrollment_tokens="x", bms_source="http")
    with pytest.raises(BMSConfigError):
        build_source(settings)


def test_build_source_http_with_url():
    settings = AgentSettings(
        enrollment_tokens="x",
        bms_source="http",
        bms_http_url="http://bms.example/demand",
    )
    src = build_source(settings)
    try:
        assert isinstance(src, HttpHeatSource)
        assert src.url == "http://bms.example/demand"
    finally:
        # Close the lazily-created client to avoid an event-loop warning.
        import asyncio

        asyncio.run(src.aclose())


def test_build_source_rejects_unknown_kind():
    settings = AgentSettings(enrollment_tokens="x", bms_source="bacnet")
    with pytest.raises(BMSConfigError):
        build_source(settings)


# --- EcobeeHeatSource (Phase 8c) -------------------------------------


def _ecobee_state(tmp_path, refresh_token: str = "rt-initial") -> str:
    """Write a valid ecobee state file and return its path."""
    state = tmp_path / "ecobee_state.json"
    state.write_text(
        json.dumps(
            {"refresh_token": refresh_token, "thermostat_id": "TS-123"}
        )
    )
    return str(state)


def _ecobee_source_with_handler(handler, state_path: str, **kwargs):
    """Build an EcobeeHeatSource with MockTransport-backed httpx."""
    defaults = dict(
        api_key="api-key-abc",
        state_file=state_path,
        thermostat_id="TS-123",
        demand_when_heating_kw=4.5,
        default_window_sec=1800,
    )
    defaults.update(kwargs)
    src = EcobeeHeatSource(**defaults)
    transport = httpx.MockTransport(handler)
    src._client = httpx.AsyncClient(transport=transport)
    return src


def test_ecobee_tenths_f_to_c_converts_correctly():
    # 720 tenths-F = 72.0F = 22.222...C
    assert _ecobee_tenths_f_to_c(720) == pytest.approx(22.2222, abs=0.001)
    # 320 tenths-F = 32.0F = 0.0C (freezing)
    assert _ecobee_tenths_f_to_c(320) == pytest.approx(0.0)
    # None / non-numeric returns None
    assert _ecobee_tenths_f_to_c(None) is None
    assert _ecobee_tenths_f_to_c("not a number") is None


def test_ecobee_constructor_raises_when_state_missing(tmp_path):
    missing = str(tmp_path / "nope.json")
    with pytest.raises(BMSConfigError, match="not found"):
        EcobeeHeatSource(
            api_key="x",
            state_file=missing,
            thermostat_id="TS-1",
            demand_when_heating_kw=3.0,
            default_window_sec=1800,
        )


def test_ecobee_constructor_raises_when_state_unparseable(tmp_path):
    state = tmp_path / "broken.json"
    state.write_text("not json")
    with pytest.raises(BMSConfigError, match="unreadable"):
        EcobeeHeatSource(
            api_key="x",
            state_file=str(state),
            thermostat_id="TS-1",
            demand_when_heating_kw=3.0,
            default_window_sec=1800,
        )


def test_ecobee_constructor_raises_when_refresh_token_missing(tmp_path):
    state = tmp_path / "noref.json"
    state.write_text(json.dumps({"thermostat_id": "TS-1"}))
    with pytest.raises(BMSConfigError, match="refresh_token"):
        EcobeeHeatSource(
            api_key="x",
            state_file=str(state),
            thermostat_id="TS-1",
            demand_when_heating_kw=3.0,
            default_window_sec=1800,
        )


def _ecobee_thermostat_response(
    equipment_status: str = "",
    actual_temp_tenths_f: int = 720,  # 72.0F
    desired_heat_tenths_f: int = 700,  # 70.0F
) -> dict:
    return {
        "thermostatList": [
            {
                "identifier": "TS-123",
                "equipmentStatus": equipment_status,
                "runtime": {
                    "actualTemperature": actual_temp_tenths_f,
                    "desiredHeat": desired_heat_tenths_f,
                },
            }
        ]
    }


def _ecobee_token_response(
    access: str = "at-1", refresh: str | None = None
) -> dict:
    body = {"access_token": access, "expires_in": 3600}
    if refresh is not None:
        body["refresh_token"] = refresh
    return body


@pytest.mark.asyncio
async def test_ecobee_heating_status_reports_demand(tmp_path):
    state = _ecobee_state(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json=_ecobee_token_response())
        return httpx.Response(
            200,
            json=_ecobee_thermostat_response(equipment_status="auxHeat1,fan"),
        )

    src = _ecobee_source_with_handler(handler, state)
    try:
        reading = await src.read()
    finally:
        await src.aclose()

    assert reading.measured_kw == 4.5  # the configured demand_when_heating_kw
    assert reading.expected_window_sec == 1800
    # 720 tenths F = 22.22 C
    assert reading.room_temp_c == pytest.approx(22.222, abs=0.01)
    # 700 tenths F = 21.11 C
    assert reading.setpoint_c == pytest.approx(21.111, abs=0.01)


@pytest.mark.asyncio
async def test_ecobee_no_heating_status_reports_zero(tmp_path):
    state = _ecobee_state(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json=_ecobee_token_response())
        # fan-only, or cooling — must not register as heating
        return httpx.Response(
            200,
            json=_ecobee_thermostat_response(
                equipment_status="fan,compCool1"
            ),
        )

    src = _ecobee_source_with_handler(handler, state)
    try:
        reading = await src.read()
    finally:
        await src.aclose()

    assert reading.measured_kw == 0.0
    assert reading.expected_window_sec is None
    # Temperatures still surfaced for the operator UI.
    assert reading.room_temp_c is not None
    assert reading.setpoint_c is not None


@pytest.mark.asyncio
async def test_ecobee_empty_equipment_status_reports_zero(tmp_path):
    state = _ecobee_state(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json=_ecobee_token_response())
        return httpx.Response(
            200, json=_ecobee_thermostat_response(equipment_status="")
        )

    src = _ecobee_source_with_handler(handler, state)
    try:
        reading = await src.read()
    finally:
        await src.aclose()
    assert reading.measured_kw == 0.0


@pytest.mark.asyncio
async def test_ecobee_persists_rotated_refresh_token(tmp_path):
    state = _ecobee_state(tmp_path, refresh_token="rt-old")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json=_ecobee_token_response(refresh="rt-new"),
            )
        return httpx.Response(
            200,
            json=_ecobee_thermostat_response(equipment_status="auxHeat1"),
        )

    src = _ecobee_source_with_handler(handler, state)
    try:
        await src.read()
    finally:
        await src.aclose()

    persisted = json.loads(open(state).read())
    assert persisted["refresh_token"] == "rt-new"
    assert persisted["thermostat_id"] == "TS-123"


@pytest.mark.asyncio
async def test_ecobee_reuses_access_token_until_expiry(tmp_path):
    state = _ecobee_state(tmp_path)
    token_calls = 0
    thermostat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, thermostat_calls
        if request.url.path == "/token":
            token_calls += 1
            return httpx.Response(200, json=_ecobee_token_response())
        thermostat_calls += 1
        return httpx.Response(
            200,
            json=_ecobee_thermostat_response(equipment_status="auxHeat1"),
        )

    src = _ecobee_source_with_handler(handler, state)
    try:
        await src.read()
        await src.read()
        await src.read()
    finally:
        await src.aclose()

    # Three reads should produce three thermostat calls but only one
    # token call — the access token caches across reads.
    assert token_calls == 1
    assert thermostat_calls == 3


@pytest.mark.asyncio
async def test_ecobee_returns_zero_on_token_failure(tmp_path):
    state = _ecobee_state(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(500, text="ecobee down")
        return httpx.Response(
            200,
            json=_ecobee_thermostat_response(equipment_status="auxHeat1"),
        )

    src = _ecobee_source_with_handler(handler, state)
    try:
        reading = await src.read()
    finally:
        await src.aclose()
    assert reading.measured_kw == 0.0


@pytest.mark.asyncio
async def test_ecobee_returns_zero_on_thermostat_query_failure(tmp_path):
    state = _ecobee_state(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json=_ecobee_token_response())
        return httpx.Response(502, text="thermostat backend down")

    src = _ecobee_source_with_handler(handler, state)
    try:
        reading = await src.read()
    finally:
        await src.aclose()
    assert reading.measured_kw == 0.0


@pytest.mark.asyncio
async def test_ecobee_returns_zero_when_thermostat_not_found(tmp_path):
    state = _ecobee_state(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json=_ecobee_token_response())
        return httpx.Response(200, json={"thermostatList": []})

    src = _ecobee_source_with_handler(handler, state)
    try:
        reading = await src.read()
    finally:
        await src.aclose()
    assert reading.measured_kw == 0.0


def test_ecobee_heating_states_includes_heat_pump_and_aux():
    # Sanity check the constant — these are the documented Ecobee
    # equipmentStatus tokens that mean heat is actively running.
    assert "heatPump" in ECOBEE_HEATING_STATES
    assert "auxHeat1" in ECOBEE_HEATING_STATES
    # compCool* is cooling, not heat — must NOT be included even though
    # it shares the comp* prefix.
    assert "compCool1" not in ECOBEE_HEATING_STATES
    # fan / humidifier are auxiliary equipment, not heat delivery.
    assert "fan" not in ECOBEE_HEATING_STATES


# --- build_source factory routing for ecobee -------------------------


def test_build_source_ecobee_requires_api_key():
    settings = AgentSettings(
        enrollment_tokens="x",
        bms_source="ecobee",
        ecobee_thermostat_id="TS-1",
    )
    with pytest.raises(BMSConfigError, match="ECOBEE_API_KEY"):
        build_source(settings)


def test_build_source_ecobee_requires_thermostat_id():
    settings = AgentSettings(
        enrollment_tokens="x",
        bms_source="ecobee",
        ecobee_api_key="api-key",
    )
    with pytest.raises(BMSConfigError, match="ECOBEE_THERMOSTAT_ID"):
        build_source(settings)


def test_build_source_ecobee_routes_to_ecobee_source(tmp_path):
    state = _ecobee_state(tmp_path)
    settings = AgentSettings(
        enrollment_tokens="x",
        bms_source="ecobee",
        ecobee_api_key="api-key",
        ecobee_thermostat_id="TS-123",
        ecobee_state_file=state,
        ecobee_demand_when_heating_kw=3.0,
    )
    src = build_source(settings)
    try:
        assert isinstance(src, EcobeeHeatSource)
        assert src.thermostat_id == "TS-123"
        assert src.demand_when_heating_kw == 3.0
    finally:
        import asyncio

        asyncio.run(src.aclose())


# --- HomeAssistantHeatSource (Phase 8d) ------------------------------


def _ha_source_with_handler(handler, **kwargs):
    """Build a HomeAssistantHeatSource backed by httpx MockTransport."""
    defaults = dict(
        ha_url="http://homeassistant.test",
        token="ha-token-abc",
        entity_id="climate.living_room",
        demand_when_heating_kw=5.0,
        default_window_sec=1800,
    )
    defaults.update(kwargs)
    src = HomeAssistantHeatSource(**defaults)
    transport = httpx.MockTransport(handler)
    src._client = httpx.AsyncClient(
        transport=transport,
        base_url="http://homeassistant.test",
        headers={"Authorization": "Bearer ha-token-abc"},
    )
    return src


def _ha_state_response(
    hvac_action: str | None = "heating",
    state: str = "heat",
    current_temperature: float | None = 18.5,
    temperature: float | None = 21.0,
) -> dict:
    attrs: dict = {}
    if hvac_action is not None:
        attrs["hvac_action"] = hvac_action
    if current_temperature is not None:
        attrs["current_temperature"] = current_temperature
    if temperature is not None:
        attrs["temperature"] = temperature
    return {"entity_id": "climate.living_room", "state": state, "attributes": attrs}


@pytest.mark.asyncio
async def test_ha_heating_action_reports_demand():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ha_state_response(hvac_action="heating"))

    src = _ha_source_with_handler(handler)
    try:
        reading = await src.read()
    finally:
        await src.aclose()

    assert reading.measured_kw == 5.0
    assert reading.expected_window_sec == 1800
    assert reading.room_temp_c == pytest.approx(18.5)
    assert reading.setpoint_c == pytest.approx(21.0)


@pytest.mark.asyncio
async def test_ha_idle_action_reports_zero():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ha_state_response(hvac_action="idle"))

    src = _ha_source_with_handler(handler)
    try:
        reading = await src.read()
    finally:
        await src.aclose()

    assert reading.measured_kw == 0.0
    assert reading.expected_window_sec is None
    # Temperatures still forwarded even when not heating.
    assert reading.room_temp_c == pytest.approx(18.5)
    assert reading.setpoint_c == pytest.approx(21.0)


@pytest.mark.asyncio
async def test_ha_cooling_action_reports_zero():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ha_state_response(hvac_action="cooling"))

    src = _ha_source_with_handler(handler)
    try:
        reading = await src.read()
    finally:
        await src.aclose()

    assert reading.measured_kw == 0.0


@pytest.mark.asyncio
async def test_ha_off_action_reports_zero():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ha_state_response(hvac_action="off", state="off"))

    src = _ha_source_with_handler(handler)
    try:
        reading = await src.read()
    finally:
        await src.aclose()

    assert reading.measured_kw == 0.0


@pytest.mark.asyncio
async def test_ha_falls_back_to_state_when_hvac_action_absent():
    """When hvac_action is missing, state=='heat' is treated as heating."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ha_state_response(hvac_action=None, state="heat"),
        )

    src = _ha_source_with_handler(handler)
    try:
        reading = await src.read()
    finally:
        await src.aclose()

    assert reading.measured_kw == 5.0


@pytest.mark.asyncio
async def test_ha_falls_back_state_off_reports_zero():
    """Fallback path: state=='off' → no demand."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ha_state_response(hvac_action=None, state="off"),
        )

    src = _ha_source_with_handler(handler)
    try:
        reading = await src.read()
    finally:
        await src.aclose()

    assert reading.measured_kw == 0.0


@pytest.mark.asyncio
async def test_ha_sends_bearer_token():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json=_ha_state_response())

    src = _ha_source_with_handler(handler)
    try:
        await src.read()
    finally:
        await src.aclose()

    assert seen["auth"] == "Bearer ha-token-abc"


@pytest.mark.asyncio
async def test_ha_hits_correct_entity_endpoint():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(200, json=_ha_state_response())

    src = _ha_source_with_handler(handler, entity_id="climate.ecobee_home")
    try:
        await src.read()
    finally:
        await src.aclose()

    assert seen["path"] == "/api/states/climate.ecobee_home"


@pytest.mark.asyncio
async def test_ha_returns_zero_on_http_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="HA unavailable")

    src = _ha_source_with_handler(handler)
    try:
        reading = await src.read()
    finally:
        await src.aclose()

    assert reading.measured_kw == 0.0


@pytest.mark.asyncio
async def test_ha_returns_zero_on_connection_error():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("HA unreachable")

    src = _ha_source_with_handler(handler)
    try:
        reading = await src.read()
    finally:
        await src.aclose()

    assert reading.measured_kw == 0.0


@pytest.mark.asyncio
async def test_ha_returns_zero_on_unexpected_response_shape():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "a", "dict"])

    src = _ha_source_with_handler(handler)
    try:
        reading = await src.read()
    finally:
        await src.aclose()

    assert reading.measured_kw == 0.0


@pytest.mark.asyncio
async def test_ha_converts_fahrenheit_to_celsius():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ha_state_response(
                hvac_action="heating",
                current_temperature=65.0,   # 65°F = 18.33°C
                temperature=70.0,           # 70°F = 21.11°C
            ),
        )

    src = _ha_source_with_handler(handler, temperature_unit="F")
    try:
        reading = await src.read()
    finally:
        await src.aclose()

    assert reading.room_temp_c == pytest.approx(18.333, abs=0.01)
    assert reading.setpoint_c == pytest.approx(21.111, abs=0.01)


@pytest.mark.asyncio
async def test_ha_none_temperatures_are_forwarded_as_none():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ha_state_response(
                current_temperature=None,
                temperature=None,
            ),
        )

    src = _ha_source_with_handler(handler)
    try:
        reading = await src.read()
    finally:
        await src.aclose()

    assert reading.room_temp_c is None
    assert reading.setpoint_c is None


# --- build_source factory routing for homeassistant ------------------


def test_build_source_ha_requires_token():
    settings = AgentSettings(
        enrollment_tokens="x",
        bms_source="homeassistant",
        ha_entity_id="climate.living_room",
    )
    with pytest.raises(BMSConfigError, match="HA_TOKEN"):
        build_source(settings)


def test_build_source_ha_requires_entity_id():
    settings = AgentSettings(
        enrollment_tokens="x",
        bms_source="homeassistant",
        ha_token="tok",
    )
    with pytest.raises(BMSConfigError, match="HA_ENTITY_ID"):
        build_source(settings)


def test_build_source_ha_routes_to_ha_source():
    settings = AgentSettings(
        enrollment_tokens="x",
        bms_source="homeassistant",
        ha_token="tok",
        ha_entity_id="climate.living_room",
        ha_demand_when_heating_kw=4.0,
    )
    src = build_source(settings)
    try:
        assert isinstance(src, HomeAssistantHeatSource)
        assert src.entity_id == "climate.living_room"
        assert src.demand_when_heating_kw == 4.0
    finally:
        import asyncio
        asyncio.run(src.aclose())


def test_build_source_unknown_now_mentions_homeassistant():
    """Error message for unknown BMS_SOURCE must list homeassistant."""
    settings = AgentSettings(enrollment_tokens="x", bms_source="bacnet")
    with pytest.raises(BMSConfigError, match="homeassistant"):
        build_source(settings)


# --- set_setpoint (Phase 22 — building hub vacancy override) ---------


@pytest.mark.asyncio
async def test_set_setpoint_default_is_unsupported():
    """Read-only sources (file/http) report no setpoint-write support."""
    import json as _json
    import tempfile as _tempfile
    from pathlib import Path as _Path

    with _tempfile.TemporaryDirectory() as tmp:
        path = _Path(tmp) / "bms.json"
        path.write_text(_json.dumps({"measured_kw": 0.0}))
        src = FileHeatSource(str(path), default_window_sec=1800)
        assert await src.set_setpoint(13.0) is False


@pytest.mark.asyncio
async def test_ha_set_setpoint_posts_climate_service():
    """The HA adapter writes via /api/services/climate/set_temperature."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json=[])

    src = _ha_source_with_handler(handler)
    try:
        ok = await src.set_setpoint(13.0)
    finally:
        await src.aclose()

    assert ok is True
    assert captured["path"] == "/api/services/climate/set_temperature"
    assert captured["body"] == {
        "entity_id": "climate.living_room",
        "temperature": 13.0,
    }


@pytest.mark.asyncio
async def test_ha_set_setpoint_converts_to_fahrenheit():
    """With HA_TEMPERATURE_UNIT=F the written value is Fahrenheit."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json=[])

    src = _ha_source_with_handler(handler, temperature_unit="F")
    try:
        ok = await src.set_setpoint(13.0)
    finally:
        await src.aclose()

    assert ok is True
    assert captured["body"]["temperature"] == pytest.approx(55.4)


@pytest.mark.asyncio
async def test_ha_set_setpoint_http_error_returns_false():
    """A failed service call reports False instead of raising."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    src = _ha_source_with_handler(handler)
    try:
        ok = await src.set_setpoint(13.0)
    finally:
        await src.aclose()

    assert ok is False
