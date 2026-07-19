"""Phase 15b — Local weather helper for autonomous Vast.AI scheduling.

When the HADCD dispatcher is offline, the agent cannot fetch the backend's
pre-computed cold-window schedule.  This module lets the agent call
Open-Meteo directly (same free API the backend uses) and compute a
``VastProvider``-compatible schedule dict from the result.

Functions
---------
fetch_forecast(lat, lon) → HourlyForecast
    48-hour hourly temperature forecast for a location.

cold_windows(forecast, threshold_c, min_hours) → list[ColdWindow]
    Contiguous below-threshold periods worth listing for.

build_vast_schedule(forecast, threshold_c, min_hours, pre_list_minutes)
    → dict
    Returns a dict in the shape ``VastProvider.update()`` expects:
      {
        "should_list":       bool,         # False = we're in a cold period
        "next_window_start": str | None,   # ISO-8601; lets VastProvider
        "next_window_end":   str | None,   # pre-list ahead of cold weather
        "active_window_end": str | None,
      }

Logic
-----
Cold windows mean **heat is needed** — the node should run workloads, not
sit idle as a GPU rental.  Outside cold windows the GPU is free, so listing
on Vast.AI is the best use of it.

  currently cold  →  should_list = False  (run heat workloads)
  warm right now  →  should_list = True   (list for rental income)

VastProvider handles the pre-listing look-ahead itself: it reads
``next_window_start`` and lists early when a cold window is approaching.
We just need to supply the next upcoming window start.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

logger = logging.getLogger("hadcd.agent.weather")

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_DEFAULT_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Data types (mirrored from backend/app/weather.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HourlyForecast:
    """48 hours of hourly outdoor temperature data for one location."""
    latitude: float
    longitude: float
    timezone: str
    times: tuple[datetime, ...]
    temps_c: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.times) != len(self.temps_c):
            raise ValueError(
                f"times ({len(self.times)}) and temps_c "
                f"({len(self.temps_c)}) must have the same length"
            )


@dataclass(frozen=True)
class ColdWindow:
    """A contiguous period where the forecast temperature is below the cold threshold.

    During cold windows the node runs its own mining workloads for heat and
    does NOT list on Vast.AI.
    """
    start: datetime
    end: datetime
    min_temp_c: float
    mean_temp_c: float

    @property
    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0


@dataclass(frozen=True)
class HotWindow:
    """A contiguous period where the forecast temperature is above the hot threshold.

    During hot windows the building does not want additional GPU heat, so the
    node also does NOT list on Vast.AI.
    """
    start: datetime
    end: datetime
    max_temp_c: float
    mean_temp_c: float

    @property
    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# Forecast fetcher
# ---------------------------------------------------------------------------


async def fetch_forecast(
    latitude: float,
    longitude: float,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    _client: httpx.AsyncClient | None = None,
) -> HourlyForecast:
    """Fetch a 48-hour hourly temperature forecast from Open-Meteo.

    No API key required.  Open-Meteo is free and has no rate-limit concerns
    at the once-per-hour cadence used in autonomous mode.

    Parameters
    ----------
    latitude:   WGS-84 decimal latitude  (-90 to 90).
    longitude:  WGS-84 decimal longitude (-180 to 180).
    timeout:    Per-request timeout in seconds.
    _client:    Injected httpx client — used in tests to avoid real HTTP.

    Raises
    ------
    httpx.HTTPStatusError   if the API returns a non-2xx status.
    httpx.TimeoutException  if the request times out.
    ValueError              if the response body is missing expected keys.
    """
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": "temperature_2m",
        "forecast_days": 2,
        "timezone": "auto",
    }

    close_after = _client is None
    client = _client or httpx.AsyncClient(timeout=timeout)
    try:
        resp = await client.get(_OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    finally:
        if close_after:
            await client.aclose()

    try:
        hourly = data["hourly"]
        raw_times: list[str] = hourly["time"]
        raw_temps: list[float] = hourly["temperature_2m"]
    except KeyError as exc:
        raise ValueError(
            f"Open-Meteo response missing expected key: {exc}. "
            f"Keys present: {list(data.keys())}"
        ) from exc

    # Open-Meteo returns localised naive strings like "2026-05-23T14:00"
    # when timezone=auto is requested.  We keep them naive so they compare
    # correctly against datetime.now() (also naive local time).
    parsed_times = tuple(datetime.fromisoformat(t) for t in raw_times)
    parsed_temps = tuple(float(t) for t in raw_temps)

    logger.debug(
        "weather: fetched %d hourly points for (%.4f, %.4f) tz=%s",
        len(parsed_times),
        latitude,
        longitude,
        data.get("timezone", "?"),
    )

    return HourlyForecast(
        latitude=float(data.get("latitude", latitude)),
        longitude=float(data.get("longitude", longitude)),
        timezone=data.get("timezone", "UTC"),
        times=parsed_times,
        temps_c=parsed_temps,
    )


# ---------------------------------------------------------------------------
# Cold-window extractor
# ---------------------------------------------------------------------------


def cold_windows(
    forecast: HourlyForecast,
    *,
    threshold_c: float = 5.0,
    min_hours: float = 2.0,
) -> list[ColdWindow]:
    """Identify contiguous periods where the forecast temperature < threshold.

    Parameters
    ----------
    forecast:     The hourly forecast to analyse.
    threshold_c:  Temperature (°C) below which an hour is considered "cold".
    min_hours:    Minimum duration for a window to be included.

    Returns
    -------
    List of ColdWindow objects sorted by start time.
    """
    if not forecast.times:
        return []

    windows: list[ColdWindow] = []
    run_start_idx: int | None = None
    pairs = list(zip(forecast.times, forecast.temps_c))

    for i, (_, temp) in enumerate(pairs):
        is_cold = temp < threshold_c
        if is_cold and run_start_idx is None:
            run_start_idx = i
        elif not is_cold and run_start_idx is not None:
            _maybe_add_window(windows, pairs, run_start_idx, i, min_hours)
            run_start_idx = None

    if run_start_idx is not None:
        _maybe_add_window(windows, pairs, run_start_idx, len(pairs), min_hours)

    return windows


def _maybe_add_window(
    windows: list[ColdWindow],
    pairs: list[tuple[datetime, float]],
    start_idx: int,
    end_idx: int,
    min_hours: float,
) -> None:
    run_times = [pairs[i][0] for i in range(start_idx, end_idx)]
    run_temps = [pairs[i][1] for i in range(start_idx, end_idx)]

    window_start = run_times[0]
    window_end = run_times[-1] + timedelta(hours=1)
    duration_h = (window_end - window_start).total_seconds() / 3600.0

    if duration_h < min_hours:
        return

    windows.append(ColdWindow(
        start=window_start,
        end=window_end,
        min_temp_c=min(run_temps),
        mean_temp_c=sum(run_temps) / len(run_temps),
    ))


# ---------------------------------------------------------------------------
# Hot-window extractor (Phase 15d)
# ---------------------------------------------------------------------------


def hot_windows(
    forecast: HourlyForecast,
    *,
    threshold_c: float = 25.0,
    min_hours: float = 2.0,
) -> list[HotWindow]:
    """Identify contiguous periods where the forecast temperature exceeds threshold.

    Parameters
    ----------
    forecast:     The hourly forecast to analyse.
    threshold_c:  Temperature (°C) above which an hour is "too hot".
    min_hours:    Minimum duration for a window to be included.
    """
    if not forecast.times:
        return []

    windows: list[HotWindow] = []
    run_start_idx: int | None = None
    pairs = list(zip(forecast.times, forecast.temps_c))

    for i, (_, temp) in enumerate(pairs):
        is_hot = temp > threshold_c
        if is_hot and run_start_idx is None:
            run_start_idx = i
        elif not is_hot and run_start_idx is not None:
            _maybe_add_hot_window(windows, pairs, run_start_idx, i, min_hours)
            run_start_idx = None

    if run_start_idx is not None:
        _maybe_add_hot_window(windows, pairs, run_start_idx, len(pairs), min_hours)

    return windows


def _maybe_add_hot_window(
    windows: list[HotWindow],
    pairs: list[tuple[datetime, float]],
    start_idx: int,
    end_idx: int,
    min_hours: float,
) -> None:
    run_times = [pairs[i][0] for i in range(start_idx, end_idx)]
    run_temps = [pairs[i][1] for i in range(start_idx, end_idx)]

    window_start = run_times[0]
    window_end = run_times[-1] + timedelta(hours=1)
    duration_h = (window_end - window_start).total_seconds() / 3600.0

    if duration_h < min_hours:
        return

    windows.append(HotWindow(
        start=window_start,
        end=window_end,
        max_temp_c=max(run_temps),
        mean_temp_c=sum(run_temps) / len(run_temps),
    ))


# ---------------------------------------------------------------------------
# Schedule builder
# ---------------------------------------------------------------------------


def build_vast_schedule(
    forecast: HourlyForecast,
    *,
    threshold_c: float = 5.0,
    hot_threshold_c: float | None = None,
    min_hours: float = 2.0,
    now: datetime | None = None,
) -> dict:
    """Build a VastProvider-compatible schedule dict from a local forecast.

    The node lists on Vast.AI only in the moderate temperature band:
      too cold (below threshold_c)     → should_list = False (mine locally)
      moderate                          → should_list = True  (list on Vast.AI)
      too hot  (above hot_threshold_c) → should_list = False (no extra heat)

    hot_threshold_c=None disables the upper bound: the node lists on
    Vast.AI whenever it is not in a cold window.

    Returns
    -------
    dict with keys:
      should_list       — True when in the moderate temperature band.
      next_window_start — ISO-8601 start of the next no-list window (cold
                          or hot), or None.  VastProvider uses this for
                          proactive pre-unlisting.
      next_window_end   — ISO-8601 end of that window, or None.
      active_window_end — None (not tracked locally).
    """
    if now is None:
        now = datetime.now()

    cold_wins = cold_windows(forecast, threshold_c=threshold_c, min_hours=min_hours)
    hot_wins: list[HotWindow] = []
    if hot_threshold_c is not None:
        hot_wins = hot_windows(forecast, threshold_c=hot_threshold_c, min_hours=min_hours)

    currently_cold = any(w.start <= now <= w.end for w in cold_wins)
    currently_hot = any(w.start <= now <= w.end for w in hot_wins)

    # List only in the moderate band — not too cold, not too hot.
    should_list = not currently_cold and not currently_hot

    # Next upcoming no-list window (either cold or hot), whichever is sooner.
    future_nolists = sorted(
        [w for w in cold_wins if w.start > now] +
        [w for w in hot_wins if w.start > now],
        key=lambda w: w.start,
    )
    next_w = future_nolists[0] if future_nolists else None

    logger.debug(
        "weather: local schedule — cold=%s hot=%s should_list=%s next_window=%s",
        currently_cold,
        currently_hot,
        should_list,
        next_w.start.isoformat() if next_w else "none",
    )

    return {
        "should_list": should_list,
        "next_window_start": next_w.start.isoformat() if next_w else None,
        "next_window_end": next_w.end.isoformat() if next_w else None,
        "active_window_end": None,
    }
