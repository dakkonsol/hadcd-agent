"""Unit tests for Phase 11c/11d — VastProvider state machine, CLI wrapper,
and rental session CSV logging.

All tests are pure unit tests: no real subprocess calls, no network, no
external dependencies.  The VastAiCli subprocess runner and VastProvider
schedule polling are mocked at injection points.

Coverage:
  VastAiCli:
    - list_machine: correct CLI args, success path, failure path
    - unlist_machine: correct CLI args, success path, failure path
    - active_rental_count: JSON list parse, JSON dict parse, parse error,
      CLI failure → returns -1
    - command-not-found → returns -1 (safe)

  VastProvider (state machine):
    - disabled when API key or machine ID is empty
    - UNLISTED + should_list=True → calls list → LISTED
    - UNLISTED + should_list=False → stays UNLISTED
    - LISTED + should_list=True → stays LISTED
    - LISTED + should_list=False → calls unlist → UNLISTING
    - UNLISTING + no active rentals → UNLISTED
    - UNLISTING + active rentals → stays UNLISTING
    - UNLISTING + count=-1 (unknown) → stays UNLISTING (safe)
    - UNLISTING + want_listed again → re-lists → LISTED
    - pre-list: UNLISTED + next_window_start within pre_list_minutes → lists
    - pre-list: UNLISTED + next_window_start far away → stays UNLISTED
    - CLI list failure → reverts to UNLISTED (retry next tick)
    - CLI unlist failure → stays LISTED (retry next tick)

  build_vast_provider:
    - no api_key → provider disabled
    - no machine_id → provider disabled
    - both set → provider enabled with correct machine_id
"""

from __future__ import annotations

import csv
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.vast_provider import (
    VastAiCli,
    VastProvider,
    VastProviderState,
    _discover_machine_id,
    _log_session,
    _persist_machine_id,
    build_vast_provider,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fake_run(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Build a fake subprocess.run result."""
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def _mock_run_factory(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Return a callable that mimics subprocess.run, returning a fixed result."""
    def _run(*args, **kwargs):
        return _fake_run(returncode, stdout, stderr)
    return _run


def _cli(machine_id: str = "12345", returncode: int = 0, stdout: str = ""):
    """Build a VastAiCli with an injected no-op subprocess runner."""
    return VastAiCli(
        api_key="test-key",
        machine_id=machine_id,
        cmd="vastai",
        _run=_mock_run_factory(returncode, stdout),
    )


def _provider(
    cli: VastAiCli | None = None,
    pre_list_minutes: float = 10.0,
    payout_log: str = "",
) -> VastProvider:
    return VastProvider(
        cli=cli or _cli(),
        pre_list_minutes=pre_list_minutes,
        payout_log=payout_log,
    )


def _schedule(
    should_list: bool = False,
    next_window_start: str | None = None,
    next_window_end: str | None = None,
    active_window_end: str | None = None,
) -> dict:
    return {
        "should_list": should_list,
        "next_window_start": next_window_start,
        "next_window_end": next_window_end,
        "active_window_end": active_window_end,
        "windows": [],
        "last_computed_at": None,
    }


def _future_iso(minutes_from_now: float) -> str:
    dt = _utcnow() + timedelta(minutes=minutes_from_now)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# VastAiCli — list_machine
# ---------------------------------------------------------------------------


class TestVastAiCliListMachine:
    def test_returns_true_on_success(self):
        cli = _cli(returncode=0)
        assert cli.list_machine() is True

    def test_returns_false_on_nonzero_exit(self):
        cli = _cli(returncode=1, stdout="error: permission denied")
        assert cli.list_machine() is False

    def test_correct_cli_args(self):
        captured = []

        def fake_run(cmd, **kwargs):
            captured.append(cmd)
            return _fake_run(0)

        cli = VastAiCli(api_key="k", machine_id="99", cmd="vastai", _run=fake_run)
        cli.list_machine()

        assert captured[0] == ["vastai", "set", "machine", "99", "listed=true"]

    def test_api_key_in_env_not_args(self):
        """API key must be in env dict, not in the command args."""
        envs_seen = []

        def fake_run(cmd, **kwargs):
            envs_seen.append(kwargs.get("env", {}))
            return _fake_run(0)

        cli = VastAiCli(api_key="secret-key", machine_id="1", _run=fake_run)
        cli.list_machine()

        assert envs_seen[0].get("VAST_API_KEY") == "secret-key"
        assert "secret-key" not in " ".join(cli.cmd)

    def test_binary_not_found_returns_false(self):
        def fake_run(cmd, **kwargs):
            raise FileNotFoundError("vastai not found")

        cli = VastAiCli(api_key="k", machine_id="1", _run=fake_run)
        assert cli.list_machine() is False

    def test_timeout_returns_false(self):
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd="vastai", timeout=30)

        cli = VastAiCli(api_key="k", machine_id="1", _run=fake_run)
        assert cli.list_machine() is False


# ---------------------------------------------------------------------------
# VastAiCli — unlist_machine
# ---------------------------------------------------------------------------


class TestVastAiCliUnlistMachine:
    def test_returns_true_on_success(self):
        cli = _cli(returncode=0)
        assert cli.unlist_machine() is True

    def test_returns_false_on_failure(self):
        cli = _cli(returncode=1)
        assert cli.unlist_machine() is False

    def test_correct_cli_args(self):
        captured = []

        def fake_run(cmd, **kwargs):
            captured.append(cmd)
            return _fake_run(0)

        cli = VastAiCli(api_key="k", machine_id="42", _run=fake_run)
        cli.unlist_machine()

        assert captured[0] == ["vastai", "set", "machine", "42", "listed=false"]


# ---------------------------------------------------------------------------
# VastAiCli — active_rental_count
# ---------------------------------------------------------------------------


class TestVastAiCliRentalCount:
    def test_list_response_returns_length(self):
        instances = [{"id": 1}, {"id": 2}]
        cli = _cli(returncode=0, stdout=json.dumps(instances))
        assert cli.active_rental_count() == 2

    def test_dict_response_with_instances_key(self):
        body = {"instances": [{"id": 1}]}
        cli = _cli(returncode=0, stdout=json.dumps(body))
        assert cli.active_rental_count() == 1

    def test_empty_list_returns_zero(self):
        cli = _cli(returncode=0, stdout="[]")
        assert cli.active_rental_count() == 0

    def test_cli_failure_returns_minus_one(self):
        cli = _cli(returncode=1)
        assert cli.active_rental_count() == -1

    def test_invalid_json_returns_minus_one(self):
        cli = _cli(returncode=0, stdout="not json {{{")
        assert cli.active_rental_count() == -1

    def test_binary_not_found_returns_minus_one(self):
        def fake_run(cmd, **kwargs):
            raise FileNotFoundError()

        cli = VastAiCli(api_key="k", machine_id="1", _run=fake_run)
        assert cli.active_rental_count() == -1


# ---------------------------------------------------------------------------
# VastProvider — disabled state
# ---------------------------------------------------------------------------


class TestVastProviderDisabled:
    def test_disabled_when_cli_is_none(self):
        p = VastProvider(cli=None)
        assert not p.enabled

    def test_update_is_noop_when_disabled(self):
        p = VastProvider(cli=None)
        # Should not raise
        p.update({"should_list": True})
        assert p.state == VastProviderState.UNLISTED

    def test_build_vast_provider_disabled_no_api_key(self):
        settings = MagicMock()
        settings.vastai_api_key = ""
        settings.vastai_machine_id = "123"
        settings.vastai_cmd = "vastai"
        settings.vast_pre_list_minutes = 10.0
        p = build_vast_provider(settings)
        assert not p.enabled

    def test_build_vast_provider_disabled_no_machine_id_discovery_also_fails(self):
        """No machine ID + discovery returns nothing → provider disabled."""
        settings = MagicMock()
        settings.vastai_api_key = "secret"
        settings.vastai_machine_id = ""
        settings.vastai_cmd = "vastai"
        settings.vast_pre_list_minutes = 10.0
        with patch("agent.vast_provider._discover_machine_id", return_value=None):
            p = build_vast_provider(settings)
        assert not p.enabled

    def test_build_vast_provider_enabled_when_both_set(self):
        settings = MagicMock()
        settings.vastai_api_key = "secret"
        settings.vastai_machine_id = "42"
        settings.vastai_cmd = "vastai"
        settings.vast_pre_list_minutes = 10.0
        p = build_vast_provider(settings)
        assert p.enabled

    def test_build_vast_provider_auto_discovers_machine_id(self):
        """No machine ID but discovery succeeds → provider enabled with found ID."""
        settings = MagicMock()
        settings.vastai_api_key = "secret"
        settings.vastai_machine_id = ""
        settings.vastai_cmd = "vastai"
        settings.vast_pre_list_minutes = 10.0
        settings.vast_payout_log = ""
        with (
            patch("agent.vast_provider._discover_machine_id", return_value="99") as mock_disc,
            patch("agent.vast_provider._persist_machine_id") as mock_persist,
        ):
            p = build_vast_provider(settings)
        assert p.enabled
        mock_disc.assert_called_once_with("secret", "vastai")
        mock_persist.assert_called_once_with("99")
        # In-memory settings updated so the session activates immediately.
        assert settings.vastai_machine_id == "99"


# ---------------------------------------------------------------------------
# _discover_machine_id
# ---------------------------------------------------------------------------


class TestDiscoverMachineId:
    """Unit tests for the Vast.AI machine ID auto-discovery helper.

    All subprocess calls are mocked — no real CLI invocation.
    """

    def _run_ok(self, machines: list) -> MagicMock:
        """Build a fake subprocess result returning a JSON list of machines."""
        r = MagicMock()
        r.returncode = 0
        r.stdout = json.dumps(machines)
        r.stderr = ""
        return r

    def test_hostname_match_returns_id(self):
        machines = [
            {"id": 7, "hostname": "my-node"},
            {"id": 8, "hostname": "other-node"},
        ]
        with (
            patch("subprocess.run", return_value=self._run_ok(machines)),
            patch("socket.gethostname", return_value="my-node"),
        ):
            result = _discover_machine_id("key")
        assert result == "7"

    def test_single_machine_fallback_when_no_hostname_match(self):
        machines = [{"id": 42, "hostname": "something-else"}]
        with (
            patch("subprocess.run", return_value=self._run_ok(machines)),
            patch("socket.gethostname", return_value="my-node"),
        ):
            result = _discover_machine_id("key")
        assert result == "42"

    def test_multiple_machines_no_hostname_match_returns_none(self):
        machines = [
            {"id": 1, "hostname": "node-a"},
            {"id": 2, "hostname": "node-b"},
        ]
        with (
            patch("subprocess.run", return_value=self._run_ok(machines)),
            patch("socket.gethostname", return_value="my-node"),
        ):
            result = _discover_machine_id("key")
        assert result is None

    def test_empty_machine_list_returns_none(self):
        with (
            patch("subprocess.run", return_value=self._run_ok([])),
            patch("socket.gethostname", return_value="my-node"),
        ):
            result = _discover_machine_id("key")
        assert result is None

    def test_cli_nonzero_exit_returns_none(self):
        r = MagicMock()
        r.returncode = 1
        r.stdout = ""
        r.stderr = "error: unauthorized"
        with patch("subprocess.run", return_value=r):
            result = _discover_machine_id("key")
        assert result is None

    def test_cli_not_found_returns_none(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = _discover_machine_id("key")
        assert result is None

    def test_cli_timeout_returns_none(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("vastai", 20)):
            result = _discover_machine_id("key")
        assert result is None

    def test_invalid_json_returns_none(self):
        r = MagicMock()
        r.returncode = 0
        r.stdout = "not json"
        r.stderr = ""
        with patch("subprocess.run", return_value=r):
            result = _discover_machine_id("key")
        assert result is None

    def test_api_key_passed_via_env_not_cli_args(self):
        """Key must be in env, never in the CLI arg list (ps aux safety)."""
        machines = [{"id": 5, "hostname": "my-node"}]
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env", {})
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps(machines)
            r.stderr = ""
            return r

        with (
            patch("subprocess.run", side_effect=fake_run),
            patch("socket.gethostname", return_value="my-node"),
        ):
            _discover_machine_id("supersecret")

        assert "supersecret" not in " ".join(captured["cmd"])
        assert captured["env"].get("VAST_API_KEY") == "supersecret"

    def test_dict_response_with_machines_key(self):
        """Some CLI versions wrap the list in {"machines": [...]}."""
        machines = [{"id": 77, "hostname": "my-node"}]
        r = MagicMock()
        r.returncode = 0
        r.stdout = json.dumps({"machines": machines})
        r.stderr = ""
        with (
            patch("subprocess.run", return_value=r),
            patch("socket.gethostname", return_value="my-node"),
        ):
            result = _discover_machine_id("key")
        assert result == "77"


# ---------------------------------------------------------------------------
# _persist_machine_id
# ---------------------------------------------------------------------------


class TestPersistMachineId:
    """Tests for the agent.env write-back helper.

    We redirect the candidate path list to a real temp file so the
    read/write logic runs against actual filesystem I/O.
    """

    def _patch_candidates(self, first: "Path"):
        """Return a context manager that makes the first candidate == first."""
        from pathlib import Path as _RealPath

        def _path_factory(p: str):
            if "/etc/hadcd-agent/agent.env" in str(p):
                return first
            # Fall through to a non-existent real path so the sentinel
            # "next(p for p in candidates if p.exists())" skips it.
            return _RealPath(p)

        return patch("agent.vast_provider.Path", side_effect=_path_factory)

    def test_updates_existing_key(self, tmp_path):
        env = tmp_path / "agent.env"
        env.write_text("FOO=bar\nVASTAI_MACHINE_ID=old\nBAZ=qux\n")
        with self._patch_candidates(env):
            _persist_machine_id("99")
        content = env.read_text()
        assert "VASTAI_MACHINE_ID=99" in content
        assert "VASTAI_MACHINE_ID=old" not in content
        assert "FOO=bar" in content   # other keys preserved

    def test_appends_key_when_absent(self, tmp_path):
        env = tmp_path / "agent.env"
        env.write_text("FOO=bar\n")
        with self._patch_candidates(env):
            _persist_machine_id("55")
        assert "VASTAI_MACHINE_ID=55" in env.read_text()
        assert "FOO=bar" in env.read_text()

    def test_skips_gracefully_when_no_env_file(self, tmp_path):
        """No candidate file exists → no error, nothing written."""
        missing = tmp_path / "does_not_exist.env"
        # Both candidates are missing
        with self._patch_candidates(missing):
            _persist_machine_id("99")   # must not raise


# ---------------------------------------------------------------------------
# VastProvider — UNLISTED transitions
# ---------------------------------------------------------------------------


class TestVastProviderFromUnlisted:
    def test_stays_unlisted_when_should_list_false(self):
        p = _provider()
        p.update(_schedule(should_list=False))
        assert p.state == VastProviderState.UNLISTED

    def test_lists_when_should_list_true(self):
        mock_cli = MagicMock()
        mock_cli.list_machine.return_value = True
        p = VastProvider(cli=mock_cli)

        p.update(_schedule(should_list=True))

        mock_cli.list_machine.assert_called_once()
        assert p.state == VastProviderState.LISTED

    def test_reverts_to_unlisted_when_cli_list_fails(self):
        mock_cli = MagicMock()
        mock_cli.list_machine.return_value = False
        p = VastProvider(cli=mock_cli)

        p.update(_schedule(should_list=True))

        assert p.state == VastProviderState.UNLISTED

    def test_pre_lists_when_window_is_imminent(self):
        mock_cli = MagicMock()
        mock_cli.list_machine.return_value = True
        p = VastProvider(cli=mock_cli, pre_list_minutes=10.0)

        # Window starts in 5 minutes — within 10-minute pre-list window
        p.update(_schedule(
            should_list=False,
            next_window_start=_future_iso(5),
        ))

        mock_cli.list_machine.assert_called_once()
        assert p.state == VastProviderState.LISTED

    def test_stays_unlisted_when_window_is_far_away(self):
        mock_cli = MagicMock()
        p = VastProvider(cli=mock_cli, pre_list_minutes=10.0)

        # Window starts in 30 minutes — outside 10-minute pre-list window
        p.update(_schedule(
            should_list=False,
            next_window_start=_future_iso(30),
        ))

        mock_cli.list_machine.assert_not_called()
        assert p.state == VastProviderState.UNLISTED

    def test_no_next_window_stays_unlisted(self):
        mock_cli = MagicMock()
        p = VastProvider(cli=mock_cli)

        p.update(_schedule(should_list=False, next_window_start=None))

        mock_cli.list_machine.assert_not_called()
        assert p.state == VastProviderState.UNLISTED


# ---------------------------------------------------------------------------
# VastProvider — LISTED transitions
# ---------------------------------------------------------------------------


class TestVastProviderFromListed:
    def _listed_provider(self) -> VastProvider:
        mock_cli = MagicMock()
        mock_cli.list_machine.return_value = True
        p = VastProvider(cli=mock_cli)
        p.update(_schedule(should_list=True))
        assert p.state == VastProviderState.LISTED
        mock_cli.reset_mock()
        return p

    def test_stays_listed_when_should_list_true(self):
        p = self._listed_provider()
        p.update(_schedule(should_list=True))
        assert p.state == VastProviderState.LISTED

    def test_unlists_gracefully_when_should_list_false(self):
        p = self._listed_provider()
        p._cli.unlist_machine.return_value = True  # type: ignore[union-attr]

        p.update(_schedule(should_list=False))

        p._cli.unlist_machine.assert_called_once()  # type: ignore[union-attr]
        assert p.state == VastProviderState.UNLISTING

    def test_stays_listed_when_cli_unlist_fails(self):
        p = self._listed_provider()
        p._cli.unlist_machine.return_value = False  # type: ignore[union-attr]

        p.update(_schedule(should_list=False))

        assert p.state == VastProviderState.LISTED

    def test_does_not_call_list_again_when_already_listed(self):
        p = self._listed_provider()
        p.update(_schedule(should_list=True))
        p._cli.list_machine.assert_not_called()  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# VastProvider — UNLISTING transitions
# ---------------------------------------------------------------------------


class TestVastProviderFromUnlisting:
    def _unlisting_provider(self) -> tuple[VastProvider, MagicMock]:
        mock_cli = MagicMock()
        mock_cli.list_machine.return_value = True
        mock_cli.unlist_machine.return_value = True
        p = VastProvider(cli=mock_cli)
        # drive to LISTED
        p.update(_schedule(should_list=True))
        # drive to UNLISTING
        p.update(_schedule(should_list=False))
        assert p.state == VastProviderState.UNLISTING
        mock_cli.reset_mock()
        return p, mock_cli

    def test_transitions_to_unlisted_when_no_active_rentals(self):
        p, mock_cli = self._unlisting_provider()
        mock_cli.active_rental_count.return_value = 0

        p.update(_schedule(should_list=False))

        assert p.state == VastProviderState.UNLISTED

    def test_stays_unlisting_when_rentals_active(self):
        p, mock_cli = self._unlisting_provider()
        mock_cli.active_rental_count.return_value = 2

        p.update(_schedule(should_list=False))

        assert p.state == VastProviderState.UNLISTING

    def test_stays_unlisting_when_count_unknown(self):
        """count=-1 (CLI error) → stay UNLISTING, do not prematurely clear."""
        p, mock_cli = self._unlisting_provider()
        mock_cli.active_rental_count.return_value = -1

        p.update(_schedule(should_list=False))

        assert p.state == VastProviderState.UNLISTING

    def test_re_lists_when_window_resumes_while_unlisting(self):
        """If should_list becomes True during UNLISTING, re-list immediately."""
        p, mock_cli = self._unlisting_provider()
        mock_cli.list_machine.return_value = True

        p.update(_schedule(should_list=True))

        mock_cli.list_machine.assert_called_once()
        assert p.state == VastProviderState.LISTED


# ---------------------------------------------------------------------------
# VastProvider — _is_near_window
# ---------------------------------------------------------------------------


class TestIsNearWindow:
    def test_true_when_within_window(self):
        p = VastProvider(cli=MagicMock(), pre_list_minutes=10.0)
        soon = _future_iso(5)  # 5 min from now — within 10-min threshold
        assert p._is_near_window(soon) is True

    def test_false_when_outside_window(self):
        p = VastProvider(cli=MagicMock(), pre_list_minutes=10.0)
        far = _future_iso(30)  # 30 min from now — outside threshold
        assert p._is_near_window(far) is False

    def test_false_when_none(self):
        p = VastProvider(cli=MagicMock(), pre_list_minutes=10.0)
        assert p._is_near_window(None) is False

    def test_false_for_past_time(self):
        p = VastProvider(cli=MagicMock(), pre_list_minutes=10.0)
        past = (_utcnow() - timedelta(minutes=5)).isoformat()
        assert p._is_near_window(past) is False

    def test_false_for_invalid_string(self):
        p = VastProvider(cli=MagicMock(), pre_list_minutes=10.0)
        assert p._is_near_window("not-a-date") is False


# ---------------------------------------------------------------------------
# Phase 11d — _log_session helper
# ---------------------------------------------------------------------------


class TestLogSession:
    """Tests for the _log_session CSV writer."""

    def test_creates_file_with_header(self, tmp_path):
        log = str(tmp_path / "sessions.csv")
        t0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        _log_session(log, "99999", t0, t1)

        rows = list(csv.reader(Path(log).open()))
        assert rows[0] == [
            "listed_at", "unlisted_at", "duration_hrs", "machine_id",
            "listing_price_dph", "estimated_gross_usd", "estimated_net_usd",
        ]
        assert len(rows) == 2

    def test_row_values(self, tmp_path):
        log = str(tmp_path / "sessions.csv")
        t0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 1, 1, 11, 30, 0, tzinfo=timezone.utc)  # 1.5 h

        _log_session(log, "99999", t0, t1)

        rows = list(csv.reader(Path(log).open()))
        assert rows[1][0] == t0.isoformat()
        assert rows[1][1] == t1.isoformat()
        assert float(rows[1][2]) == pytest.approx(1.5)
        assert rows[1][3] == "99999"

    def test_appends_multiple_rows(self, tmp_path):
        log = str(tmp_path / "sessions.csv")
        t0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, 8, 0, 0, tzinfo=timezone.utc)
        t3 = datetime(2026, 1, 2, 10, 0, 0, tzinfo=timezone.utc)

        _log_session(log, "99999", t0, t1)
        _log_session(log, "99999", t2, t3)

        rows = list(csv.reader(Path(log).open()))
        # header + 2 data rows
        assert len(rows) == 3
        # header appears only once
        assert rows[0][:4] == ["listed_at", "unlisted_at", "duration_hrs", "machine_id"]

    def test_creates_parent_dirs(self, tmp_path):
        log = str(tmp_path / "deep" / "nested" / "sessions.csv")
        t0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        _log_session(log, "99999", t0, t1)

        assert Path(log).exists()

    def test_survives_bad_path(self):
        """An unwritable path must not raise — just log a warning."""
        # /dev/null/bad is unwritable on all platforms.
        # On Windows, an invalid path character suffices.
        bad = "/dev/null/subdir/impossible.csv"
        t0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        # Should not raise
        _log_session(bad, "99999", t0, t1)


# ---------------------------------------------------------------------------
# Phase 11d — session tracking inside VastProvider
# ---------------------------------------------------------------------------


class TestVastProviderSessionTracking:
    """VastProvider records listed_at on listing and logs sessions on
    the UNLISTING → UNLISTED transition."""

    def _make_provider(self, tmp_path) -> tuple[VastProvider, MagicMock]:
        mock_cli = MagicMock()
        mock_cli.machine_id = "12345"
        mock_cli.list_machine.return_value = True
        mock_cli.unlist_machine.return_value = True
        mock_cli.active_rental_count.return_value = 0
        # _log_session formats this as a float; a bare MagicMock would crash it.
        mock_cli.get_listing_price.return_value = 0.25
        log = str(tmp_path / "sessions.csv")
        p = VastProvider(cli=mock_cli, payout_log=log)
        return p, mock_cli

    def test_listed_at_set_on_listing_success(self, tmp_path):
        p, _ = self._make_provider(tmp_path)
        assert p._listed_at is None
        p.update(_schedule(should_list=True))
        assert p.state == VastProviderState.LISTED
        assert p._listed_at is not None

    def test_listed_at_not_set_on_listing_failure(self, tmp_path):
        mock_cli = MagicMock()
        mock_cli.machine_id = "12345"
        mock_cli.list_machine.return_value = False
        log = str(tmp_path / "sessions.csv")
        p = VastProvider(cli=mock_cli, payout_log=log)

        p.update(_schedule(should_list=True))

        assert p.state == VastProviderState.UNLISTED
        assert p._listed_at is None

    def test_session_logged_on_unlisting_complete(self, tmp_path):
        p, mock_cli = self._make_provider(tmp_path)
        log = p._payout_log

        # Drive UNLISTED → LISTED
        p.update(_schedule(should_list=True))
        assert p.state == VastProviderState.LISTED

        # Drive LISTED → UNLISTING
        mock_cli.unlist_machine.return_value = True
        p.update(_schedule(should_list=False))
        assert p.state == VastProviderState.UNLISTING

        # Drive UNLISTING → UNLISTED (0 active rentals)
        mock_cli.active_rental_count.return_value = 0
        p.update(_schedule(should_list=False))
        assert p.state == VastProviderState.UNLISTED

        # Check that the CSV was written
        rows = list(csv.reader(Path(log).open()))
        assert len(rows) == 2  # header + 1 data row
        assert rows[0][0] == "listed_at"
        assert rows[1][3] == "12345"  # machine_id column

    def test_session_not_logged_when_no_payout_log(self, tmp_path):
        """When payout_log is empty, no file is created."""
        mock_cli = MagicMock()
        mock_cli.machine_id = "12345"
        mock_cli.list_machine.return_value = True
        mock_cli.unlist_machine.return_value = True
        mock_cli.active_rental_count.return_value = 0
        # No payout_log
        p = VastProvider(cli=mock_cli, payout_log="")

        p.update(_schedule(should_list=True))
        p.update(_schedule(should_list=False))
        p.update(_schedule(should_list=False))

        # No file should exist — nothing to assert beyond "no exception raised"

    def test_listed_at_cleared_after_session_logged(self, tmp_path):
        p, mock_cli = self._make_provider(tmp_path)

        p.update(_schedule(should_list=True))
        p.update(_schedule(should_list=False))
        p.update(_schedule(should_list=False))

        assert p._listed_at is None
        assert p.state == VastProviderState.UNLISTED

    def test_session_not_logged_during_mere_unlisting(self, tmp_path):
        """Session CSV is only written when rentals fully drain — not on
        the LISTED → UNLISTING transition itself."""
        mock_cli = MagicMock()
        mock_cli.machine_id = "12345"
        mock_cli.list_machine.return_value = True
        mock_cli.unlist_machine.return_value = True
        mock_cli.active_rental_count.return_value = 1  # still a renter
        log = str(tmp_path / "sessions.csv")
        p = VastProvider(cli=mock_cli, payout_log=log)

        p.update(_schedule(should_list=True))   # → LISTED
        p.update(_schedule(should_list=False))  # → UNLISTING (renter still there)

        assert p.state == VastProviderState.UNLISTING
        assert not Path(log).exists(), "CSV should not be written yet"

    def test_listed_at_not_overwritten_on_relist(self, tmp_path):
        """If _do_list is called again while already LISTED (re-list during
        UNLISTING), the original _listed_at is preserved so duration is
        computed from the start of the first listing, not the re-list."""
        p, mock_cli = self._make_provider(tmp_path)

        p.update(_schedule(should_list=True))  # → LISTED
        original_listed_at = p._listed_at

        # Force back to UNLISTING, then re-list
        mock_cli.unlist_machine.return_value = True
        mock_cli.active_rental_count.return_value = 1  # renter still active
        p.update(_schedule(should_list=False))  # → UNLISTING

        # should_list resumes — re-list during UNLISTING
        mock_cli.list_machine.return_value = True
        p.update(_schedule(should_list=True))  # → LISTED again

        # listed_at should still be the original value (not reset to now)
        assert p._listed_at == original_listed_at

    def test_build_vast_provider_passes_payout_log(self):
        """build_vast_provider wires vast_payout_log from settings."""
        settings = MagicMock()
        settings.vastai_api_key = "key"
        settings.vastai_machine_id = "99"
        settings.vastai_cmd = "vastai"
        settings.vast_pre_list_minutes = 10.0
        settings.vast_payout_log = "/var/lib/hadcd-agent/vast_sessions.csv"

        p = build_vast_provider(settings)

        assert p._payout_log == "/var/lib/hadcd-agent/vast_sessions.csv"
