"""Phase 11c/11d — Vast.AI provider: CLI wrapper + rental state machine.

Phase 11d adds rental session logging: every time the machine completes a
rental cycle (LISTING → LISTED → UNLISTING → UNLISTED), the session is
appended to a CSV at ``VAST_PAYOUT_LOG``.  The CSV captures:
  listed_at, unlisted_at, duration_hrs, machine_id,
  listing_price_dph, estimated_gross_usd, estimated_net_usd

Actual rental income (USD) must be confirmed from the Vast.AI dashboard —
the CLI does not expose per-session payout figures.


When VASTAI_API_KEY and VASTAI_MACHINE_ID are configured, the agent
drives its Vast.AI provider listing based on the cold-window schedule
computed by the WeatherPoller on the backend (Phase 11b).

Architecture
------------
The VastProvider class embeds a four-state machine:

  UNLISTED  ──(should list)──► LISTED
     ▲                           │
     │                     (should unlist)
     │                           ▼
     └──(no active rentals)── UNLISTING

* Listing is proactive: the machine is listed ``vast_pre_list_minutes``
  before the window starts so the offer is visible before cold weather
  arrives.
* Unlisting is always graceful: we remove the machine from the
  marketplace (stop new rentals) but NEVER terminate a running rental.
  The machine moves to UNLISTING and stays there until the active
  renter finishes or the contract expires.

Vast.AI CLI integration
-----------------------
The VastAiCli wrapper calls the ``vastai`` CLI binary (installed via
``pip install vastai``) as a subprocess.  The API key is passed via the
``VAST_API_KEY`` environment variable so it never appears in ``ps aux``
process listings.

Expected CLI commands (verify with ``vastai --help`` after installing):

  vastai set machine <machine_id> listed=true     # list for rent
  vastai set machine <machine_id> listed=false    # graceful unlist
  vastai show instances --owner me --machine_id <machine_id>
                                                  # check active rentals

Opt-in design
-------------
If either ``VASTAI_API_KEY`` or ``VASTAI_MACHINE_ID`` is empty, the
VastProvider is disabled (all methods are no-ops).  The agent logs one
advisory on startup and proceeds with pure heat-fill mode.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("hadcd.agent.vast")


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class VastProviderState(str, Enum):
    UNLISTED = "unlisted"
    LISTING = "listing"       # transient: CLI call in flight
    LISTED = "listed"
    UNLISTING = "unlisting"   # graceful: waiting for active rentals to end


# ---------------------------------------------------------------------------
# CLI wrapper
# ---------------------------------------------------------------------------


class VastAiCli:
    """Thin subprocess wrapper around the vastai CLI.

    Parameters
    ----------
    api_key:      Vast.AI API key. Passed via environment, not command line.
    machine_id:   The numeric Vast.AI machine ID of this node.
    cmd:          Name or path of the vastai CLI binary (default: "vastai").
    _run:         Injected subprocess.run replacement for testing.
    """

    def __init__(
        self,
        api_key: str,
        machine_id: str,
        cmd: str = "vastai",
        _run: Callable | None = None,
    ) -> None:
        self.api_key = api_key
        self.machine_id = machine_id
        self.cmd = cmd
        self._run: Callable = _run or subprocess.run

    def _env(self) -> dict[str, str]:
        """Build env dict with the API key injected.

        Passing credentials via environment (not CLI args) prevents them
        from appearing in ``ps aux`` or shell history.
        """
        env = os.environ.copy()
        env["VAST_API_KEY"] = self.api_key
        return env

    def _exec(self, args: list[str]) -> tuple[int, str, str]:
        """Run a vastai CLI command. Returns (returncode, stdout, stderr)."""
        full_cmd = [self.cmd] + args
        try:
            result = self._run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=30,
                env=self._env(),
            )
            return result.returncode, result.stdout, result.stderr
        except FileNotFoundError:
            return -1, "", f"command not found: {self.cmd}"
        except subprocess.TimeoutExpired:
            return -1, "", "vastai CLI timed out (30 s)"

    # --- provider operations ------------------------------------------

    def list_machine(self) -> bool:
        """Mark the machine as accepting new rentals.

        Returns True on success, False on CLI error.
        """
        code, out, err = self._exec(
            ["set", "machine", self.machine_id, "listed=true"]
        )
        if code != 0:
            logger.warning(
                "vast: list_machine failed (exit %d): %s", code, err or out
            )
            return False
        logger.info("vast: machine %s listed for rent", self.machine_id)
        return True

    def unlist_machine(self) -> bool:
        """Stop accepting new rentals. Existing rentals are NOT terminated.

        This is the graceful unlist: the marketplace offer disappears so
        no new renters can claim the machine, but currently running
        instances continue until they expire or the renter cancels.

        Returns True on success, False on CLI error.
        """
        code, out, err = self._exec(
            ["set", "machine", self.machine_id, "listed=false"]
        )
        if code != 0:
            logger.warning(
                "vast: unlist_machine failed (exit %d): %s", code, err or out
            )
            return False
        logger.info(
            "vast: machine %s gracefully unlisted (existing rentals unaffected)",
            self.machine_id,
        )
        return True

    def get_listing_price(self) -> float | None:
        """Return the machine's current dph_base ($/hr) from Vast.AI, or None on error.

        Called once when the machine transitions to LISTED so the session
        start price is recorded.  Runs `vastai show machines --raw` and
        matches on machine ID.  Returns the raw dph_base the host is listed
        at (before Vast.AI adds their renter-side markup).
        """
        code, out, err = self._exec(["show", "machines", "--raw"])
        if code != 0:
            logger.debug("vast: get_listing_price failed (%d): %s", code, err or out)
            return None
        try:
            data = json.loads(out)
            machines = data if isinstance(data, list) else data.get("machines", [])
            for m in machines:
                if str(m.get("id", "")) == str(self.machine_id):
                    # Prefer dph_base (host price); fall back to min_bid.
                    price = m.get("dph_base") or m.get("min_bid")
                    if price is not None:
                        return float(price)
            logger.debug(
                "vast: machine %s not found in show machines output", self.machine_id
            )
            return None
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.debug("vast: get_listing_price parse error: %s", exc)
            return None

    def active_rental_count(self) -> int:
        """Return the number of active rentals currently running on this machine.

        Returns -1 on CLI error or when the output cannot be parsed —
        callers should treat -1 as "unknown, assume rentals are active"
        so we don't prematurely stop waiting for renters to finish.
        """
        code, out, err = self._exec(
            ["show", "instances", "--owner", "me",
             "--machine_id", self.machine_id]
        )
        if code != 0:
            logger.debug("vast: show instances failed (%d): %s", code, err or out)
            return -1
        try:
            data = json.loads(out)
            if isinstance(data, list):
                count = len(data)
            elif isinstance(data, dict) and "instances" in data:
                count = len(data["instances"])
            else:
                count = 0
            return count
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.debug("vast: show instances parse error: %s", exc)
            return -1


# ---------------------------------------------------------------------------
# Session log helpers (Phase 11d)
# ---------------------------------------------------------------------------


def _log_session(
    log_path: str,
    machine_id: str,
    listed_at: datetime,
    unlisted_at: datetime,
    listing_price_dph: float | None = None,
    platform_cut_pct: float = 0.20,
) -> None:
    """Append one completed rental session row to the CSV payout log.

    CSV columns: listed_at, unlisted_at, duration_hrs, machine_id,
                 listing_price_dph, estimated_gross_usd, estimated_net_usd

    The file is created (with a header row) if it doesn't already exist.
    """
    duration_hrs = (unlisted_at - listed_at).total_seconds() / 3600
    price: str = ""
    gross: str = ""
    net: str = ""
    if listing_price_dph is not None:
        # A malformed price must not take down the provider tick — the row
        # still gets logged, just without the estimate columns.
        try:
            p = float(listing_price_dph)
            g = p * duration_hrs
            price = f"{p:.4f}"
            gross = f"{g:.6f}"
            net = f"{g * (1.0 - platform_cut_pct):.6f}"
        except (TypeError, ValueError):
            logger.warning("vast: ignoring non-numeric listing price %r", listing_price_dph)
    path = Path(log_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists()
        with path.open("a", newline="") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow([
                    "listed_at", "unlisted_at", "duration_hrs", "machine_id",
                    "listing_price_dph", "estimated_gross_usd", "estimated_net_usd",
                ])
            writer.writerow([
                listed_at.isoformat(),
                unlisted_at.isoformat(),
                f"{duration_hrs:.4f}",
                machine_id,
                price,
                gross,
                net,
            ])
        logger.info(
            "vast: session logged — machine %s, %.2f h%s",
            machine_id,
            duration_hrs,
            f", est. net ${float(net):.2f}" if net else " (no price data — check vast.ai/earnings)",
        )
    except OSError as exc:
        logger.warning("vast: could not write session log %s: %s", log_path, exc)


# ---------------------------------------------------------------------------
# State machine driver
# ---------------------------------------------------------------------------


class VastProvider:
    """Drives the Vast.AI provider listing state machine.

    One instance per agent process.  ``update()`` is called on each
    vast-poller tick (default every 60 s) with the schedule fetched from
    the backend.  The schedule is a dict matching the ``VastScheduleRead``
    schema:

        {
            "should_list": bool,
            "next_window_start": str | None,   # ISO-8601
            "next_window_end": str | None,
            "active_window_end": str | None,
            ...
        }
    """

    def __init__(
        self,
        cli: VastAiCli | None,
        *,
        pre_list_minutes: float = 10.0,
        payout_log: str = "",
        # Phase 12c — async callback to POST a completed rental to the backend.
        # Signature: async (machine_id, listed_at, unlisted_at) -> None
        # Injected by the agent so the provider stays test-friendly.
        rental_callback: Optional[Callable] = None,
    ) -> None:
        self._cli = cli
        self._pre_list_minutes = pre_list_minutes
        self._payout_log = payout_log
        self._rental_callback = rental_callback
        self._state = VastProviderState.UNLISTED
        self._enabled = cli is not None
        # Phase 11d — track when the machine was listed so we can compute
        # rental session duration when it finally unlists.
        self._listed_at: datetime | None = None
        # Phase 21b — listing price captured at listing time for earnings estimate.
        self._listing_price_dph: float | None = None
        # Phase 12b — operator manual override received via heartbeat response.
        # "list" → force list; "unlist" → force unlist; None → follow schedule.
        self._override: str | None = None

    @property
    def state(self) -> VastProviderState:
        return self._state

    @property
    def enabled(self) -> bool:
        return self._enabled

    # Phase 12b -------------------------------------------------------

    def set_override(self, override: str | None) -> None:
        """Apply (or clear) an operator-requested listing override.

        Called from ``_heartbeat_loop`` after every heartbeat response.

        Parameters
        ----------
        override:
            ``"list"``   — force-list regardless of weather schedule.
            ``"unlist"`` — force-unlist; stop accepting new rentals.
            ``None``     — no override; follow the weather schedule.
        """
        if override != self._override:
            logger.info(
                "vast: operator override changed: %s → %s",
                self._override or "none",
                override or "none",
            )
        self._override = override

    # -----------------------------------------------------------------

    def update(self, schedule: dict) -> None:
        """Reconcile the machine's listing state against ``schedule``.

        Called from the agent's asyncio loop via ``run_in_executor`` so
        the blocking subprocess calls do not stall the event loop.
        """
        if not self._enabled:
            return

        should_list: bool = schedule.get("should_list", False)
        next_start_raw: str | None = schedule.get("next_window_start")

        # Pre-listing: list early when a window is imminent.
        near_window = self._is_near_window(next_start_raw)
        want_listed = should_list or near_window

        # Phase 12b: operator override takes absolute priority.
        if self._override == "list":
            want_listed = True
        elif self._override == "unlist":
            want_listed = False

        current = self._state
        logger.debug(
            "vast: state=%s want_listed=%s (should_list=%s near_window=%s override=%s)",
            current.value,
            want_listed,
            should_list,
            near_window,
            self._override or "none",
        )

        if current == VastProviderState.UNLISTED:
            if want_listed:
                self._do_list()

        elif current == VastProviderState.LISTED:
            if not want_listed:
                self._do_unlist()

        elif current == VastProviderState.UNLISTING:
            # Stay here until active rentals drain.
            if want_listed:
                # Window started again while we were draining — re-list.
                self._do_list()
            else:
                count = self._cli.active_rental_count()  # type: ignore[union-attr]
                if count == 0:
                    logger.info("vast: all rentals finished — machine is now UNLISTED")
                    unlisted_at = datetime.now(timezone.utc)
                    machine_id = self._cli.machine_id  # type: ignore[union-attr]
                    # Phase 11d: append to local CSV payout log.
                    if self._listed_at is not None and self._payout_log:
                        _log_session(
                            self._payout_log,
                            machine_id,
                            self._listed_at,
                            unlisted_at,
                            listing_price_dph=self._listing_price_dph,
                        )
                    # Phase 12c: POST the completed session to the backend.
                    if self._listed_at is not None and self._rental_callback is not None:
                        import asyncio
                        try:
                            loop = asyncio.get_event_loop()
                            loop.create_task(
                                self._rental_callback(
                                    machine_id,
                                    self._listed_at,
                                    unlisted_at,
                                    self._listing_price_dph,
                                )
                            )
                        except RuntimeError:
                            # No running event loop (e.g. in tests) — skip.
                            logger.debug("vast: no event loop; skipping rental POST")
                    self._listed_at = None
                    self._listing_price_dph = None
                    self._state = VastProviderState.UNLISTED
                elif count > 0:
                    logger.debug(
                        "vast: %d active rental(s) still running — staying UNLISTING",
                        count,
                    )
                else:
                    # count == -1: CLI error; stay UNLISTING (safe default)
                    logger.debug("vast: rental count unknown; staying UNLISTING")

        # LISTING is transient — it resolves to LISTED on the same tick.
        # If we're somehow stuck in LISTING (e.g. CLI failed), try again.
        elif current == VastProviderState.LISTING:
            self._do_list()

    # --- internal transitions -----------------------------------------

    def _do_list(self) -> None:
        """Call the CLI to list the machine and advance state."""
        assert self._cli is not None
        self._state = VastProviderState.LISTING
        if self._cli.list_machine():
            self._state = VastProviderState.LISTED
            # Phase 11d: record the session start for duration tracking.
            if self._listed_at is None:
                self._listed_at = datetime.now(timezone.utc)
                # Phase 21b: capture listing price for earnings estimate.
                self._listing_price_dph = self._cli.get_listing_price()
                if self._listing_price_dph is not None:
                    logger.info(
                        "vast: machine listed at $%.4f/hr (dph_base)",
                        self._listing_price_dph,
                    )
                else:
                    logger.debug("vast: listing price not available from CLI")
        else:
            # CLI failed — revert so we retry next tick.
            self._state = VastProviderState.UNLISTED

    def _do_unlist(self) -> None:
        """Call the CLI to gracefully unlist and advance state."""
        assert self._cli is not None
        if self._cli.unlist_machine():
            self._state = VastProviderState.UNLISTING
        # On failure, stay LISTED so we retry next tick.

    def _is_near_window(self, next_start_raw: str | None) -> bool:
        """True if the next window starts within pre_list_minutes from now."""
        if not next_start_raw:
            return False
        try:
            next_start = datetime.fromisoformat(next_start_raw)
            if next_start.tzinfo is None:
                next_start = next_start.replace(tzinfo=timezone.utc)
            delta = next_start - datetime.now(timezone.utc)
            return timedelta(0) <= delta <= timedelta(minutes=self._pre_list_minutes)
        except (ValueError, TypeError):
            return False


# ---------------------------------------------------------------------------
# Machine ID auto-discovery
# ---------------------------------------------------------------------------


def _discover_machine_id(api_key: str, cmd: str = "vastai") -> str | None:
    """Query Vast.AI for this machine's numeric provider ID.

    Strategy
    --------
    1. Call ``vastai show machines --raw`` (returns JSON list of the
       account's registered host machines).
    2. Match the entry whose ``hostname`` equals ``socket.gethostname()``.
    3. If no hostname match but exactly *one* machine exists in the account,
       assume it's this one (common for a fresh single-node setup).
    4. Return the string machine ID, or ``None`` on any failure/no-match.

    The ``VAST_API_KEY`` env var is used so the key never appears in
    ``ps aux`` or process listings.
    """
    import socket

    hostname = socket.gethostname().lower()
    env = os.environ.copy()
    env["VAST_API_KEY"] = api_key
    try:
        result = subprocess.run(
            [cmd, "show", "machines", "--raw"],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
        if result.returncode != 0:
            logger.debug(
                "vast: show machines returned %d: %s",
                result.returncode,
                result.stderr.strip(),
            )
            return None
        raw = json.loads(result.stdout)
        # The CLI can return a list or {"machines": [...]}
        machines: list = raw if isinstance(raw, list) else raw.get("machines", [])
        # Prefer exact hostname match
        for m in machines:
            if str(m.get("hostname", "")).lower() == hostname:
                logger.info(
                    "vast: auto-discovered machine ID %s (hostname match: %r)",
                    m["id"],
                    hostname,
                )
                return str(m["id"])
        # Fall back: single-machine account → must be this one
        if len(machines) == 1:
            mid = str(machines[0]["id"])
            logger.info(
                "vast: auto-discovered machine ID %s "
                "(only machine in account; hostname was %r)",
                mid,
                hostname,
            )
            return mid
        logger.debug(
            "vast: %d machine(s) in account, none matching hostname %r — "
            "cannot auto-discover ID",
            len(machines),
            hostname,
        )
        return None
    except FileNotFoundError:
        logger.debug("vast: CLI binary %r not found — cannot auto-discover", cmd)
        return None
    except subprocess.TimeoutExpired:
        logger.debug("vast: show machines timed out")
        return None
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.debug("vast: could not parse show machines output: %s", exc)
        return None


def _persist_machine_id(machine_id: str) -> None:
    """Write ``VASTAI_MACHINE_ID`` back to agent.env so it survives restarts.

    Tries ``/etc/hadcd-agent/agent.env`` first (production), falls back to
    a local ``.env`` file (dev/Docker setup).  Silently skips if neither
    exists — the caller already has the ID in memory for this session.
    """
    candidates = [
        Path("/etc/hadcd-agent/agent.env"),
        Path(".env"),
    ]
    target = next((p for p in candidates if p.exists()), None)
    if target is None:
        logger.debug("vast: no agent.env found — machine ID not persisted to disk")
        return
    try:
        lines = target.read_text().splitlines()
        updated = False
        new_lines: list[str] = []
        for line in lines:
            if line.startswith("VASTAI_MACHINE_ID="):
                new_lines.append(f"VASTAI_MACHINE_ID={machine_id}")
                updated = True
            else:
                new_lines.append(line)
        if not updated:
            new_lines.append(f"VASTAI_MACHINE_ID={machine_id}")
        target.write_text("\n".join(new_lines) + "\n")
        logger.info(
            "vast: VASTAI_MACHINE_ID=%s written to %s", machine_id, target
        )
    except OSError as exc:
        logger.warning(
            "vast: could not persist machine ID to %s: %s", target, exc
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_vast_provider(settings) -> VastProvider:
    """Construct the VastProvider for the given agent settings.

    Returns a disabled (no-op) provider when ``VASTAI_API_KEY`` is not
    configured.  If ``VASTAI_MACHINE_ID`` is absent, auto-discovery is
    attempted via the Vast.AI CLI before giving up.
    """
    api_key = (settings.vastai_api_key or "").strip()
    if not api_key:
        logger.info(
            "vast: VASTAI_API_KEY not configured — "
            "Vast.AI provider integration disabled"
        )
        return VastProvider(cli=None)

    machine_id = (settings.vastai_machine_id or "").strip()
    if not machine_id:
        logger.info(
            "vast: VASTAI_MACHINE_ID not set — attempting auto-discovery via CLI"
        )
        machine_id = _discover_machine_id(api_key, settings.vastai_cmd) or ""
        if machine_id:
            # Persist so future restarts don't need to rediscover.
            _persist_machine_id(machine_id)
            # Update in-memory settings so this session activates immediately.
            settings.vastai_machine_id = machine_id
        else:
            logger.info(
                "vast: machine not yet registered on Vast.AI "
                "(visit https://vast.ai/become-a-host to register, "
                "then restart hadcd-agent) — Vast.AI disabled for this session"
            )
            return VastProvider(cli=None)

    cli = VastAiCli(
        api_key=api_key,
        machine_id=machine_id,
        cmd=settings.vastai_cmd,
    )
    logger.info(
        "vast: provider enabled for machine %s (pre-list window: %.0f min)",
        machine_id,
        settings.vast_pre_list_minutes,
    )
    return VastProvider(
        cli=cli,
        pre_list_minutes=settings.vast_pre_list_minutes,
        payout_log=settings.vast_payout_log,
    )
