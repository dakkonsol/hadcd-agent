"""Tests for agent/tailscale.py.

Covers:
  * _find_tailscale_binary — shutil.which, Windows candidates, Linux/macOS fallbacks
  * check_tailscale_status — all early-exit paths + happy-path parsing
  * log_tailscale_advisory — the three advisory tiers (connected, installed-not-connected,
    not-installed); exercised via caplog so no real subprocesses are spawned.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch


from agent.tailscale import (
    TailscaleStatus,
    _find_tailscale_binary,
    check_tailscale_status,
    log_tailscale_advisory,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json_output(
    backend_state: str = "Running",
    ips: list[str] | None = None,
    dns_name: str = "my-pc.tail-abc123.ts.net.",
) -> str:
    """Build a minimal ``tailscale status --json`` response."""
    return json.dumps(
        {
            "BackendState": backend_state,
            "Self": {
                "TailscaleIPs": ips if ips is not None else ["100.64.0.1", "fd7a::1"],
                "DNSName": dns_name,
            },
        }
    )


def _make_completed_process(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


# ---------------------------------------------------------------------------
# _find_tailscale_binary
# ---------------------------------------------------------------------------


class TestFindTailscaleBinary:
    def test_found_on_path(self):
        with patch("shutil.which", return_value="/usr/bin/tailscale"):
            assert _find_tailscale_binary() == "/usr/bin/tailscale"

    def test_not_found_returns_none(self):
        with (
            patch("shutil.which", return_value=None),
            patch("platform.system", return_value="Darwin"),
            patch("os.path.isfile", return_value=False),
        ):
            assert _find_tailscale_binary() is None

    def test_windows_program_files_fallback(self):
        """If not on PATH, try Windows Program Files location."""
        with (
            patch("shutil.which", return_value=None),
            patch("platform.system", return_value="Windows"),
            patch(
                "os.environ.get",
                side_effect=lambda key, default="": {
                    "ProgramFiles": r"C:\Program Files",
                    "ProgramFiles(x86)": r"C:\Program Files (x86)",
                    "LOCALAPPDATA": r"C:\Users\user\AppData\Local",
                }.get(key, default),
            ),
            patch("os.path.isfile") as mock_isfile,
        ):
            # Only the first candidate exists
            mock_isfile.side_effect = lambda p: "Program Files\\" in p and "(x86)" not in p and "Local" not in p
            result = _find_tailscale_binary()
            assert result is not None
            assert "tailscale.exe" in result

    def test_windows_localappdata_fallback(self):
        """Falls back to LOCALAPPDATA if neither Program Files path exists."""
        with (
            patch("shutil.which", return_value=None),
            patch("platform.system", return_value="Windows"),
            patch(
                "os.environ.get",
                side_effect=lambda key, default="": {
                    "ProgramFiles": r"C:\Program Files",
                    "ProgramFiles(x86)": r"C:\Program Files (x86)",
                    "LOCALAPPDATA": r"C:\Users\user\AppData\Local",
                }.get(key, default),
            ),
            patch("os.path.isfile") as mock_isfile,
        ):
            # Only the LOCALAPPDATA candidate exists
            mock_isfile.side_effect = lambda p: "AppData" in p
            result = _find_tailscale_binary()
            assert result is not None
            assert "AppData" in result

    def test_linux_usr_bin_fallback(self):
        with (
            patch("shutil.which", return_value=None),
            patch("platform.system", return_value="Linux"),
            patch("os.path.isfile") as mock_isfile,
        ):
            mock_isfile.side_effect = lambda p: p == "/usr/bin/tailscale"
            result = _find_tailscale_binary()
            assert result == "/usr/bin/tailscale"

    def test_linux_usr_local_bin_fallback(self):
        with (
            patch("shutil.which", return_value=None),
            patch("platform.system", return_value="Linux"),
            patch("os.path.isfile") as mock_isfile,
        ):
            mock_isfile.side_effect = lambda p: p == "/usr/local/bin/tailscale"
            result = _find_tailscale_binary()
            assert result == "/usr/local/bin/tailscale"

    def test_macos_homebrew_fallback(self):
        with (
            patch("shutil.which", return_value=None),
            patch("platform.system", return_value="Darwin"),
            patch("os.path.isfile") as mock_isfile,
        ):
            mock_isfile.side_effect = lambda p: p == "/opt/homebrew/bin/tailscale"
            result = _find_tailscale_binary()
            assert result == "/opt/homebrew/bin/tailscale"


# ---------------------------------------------------------------------------
# check_tailscale_status
# ---------------------------------------------------------------------------


class TestCheckTailscaleStatus:
    # --- binary not found -----------------------------------------------

    def test_binary_not_found_returns_not_installed(self):
        with patch("agent.tailscale._find_tailscale_binary", return_value=None):
            status = check_tailscale_status()
        assert status == TailscaleStatus(installed=False, connected=False)

    # --- subprocess failures --------------------------------------------

    def test_oserror_returns_installed_not_connected(self):
        with (
            patch("agent.tailscale._find_tailscale_binary", return_value="/usr/bin/tailscale"),
            patch("subprocess.run", side_effect=OSError("no such file")),
        ):
            status = check_tailscale_status()
        assert status == TailscaleStatus(installed=True, connected=False)

    def test_timeout_returns_installed_not_connected(self):
        with (
            patch("agent.tailscale._find_tailscale_binary", return_value="/usr/bin/tailscale"),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="tailscale", timeout=5)),
        ):
            status = check_tailscale_status()
        assert status == TailscaleStatus(installed=True, connected=False)

    def test_nonzero_returncode_returns_installed_not_connected(self):
        with (
            patch("agent.tailscale._find_tailscale_binary", return_value="/usr/bin/tailscale"),
            patch("subprocess.run", return_value=_make_completed_process(returncode=1)),
        ):
            status = check_tailscale_status()
        assert status == TailscaleStatus(installed=True, connected=False)

    def test_invalid_json_returns_installed_not_connected(self):
        with (
            patch("agent.tailscale._find_tailscale_binary", return_value="/usr/bin/tailscale"),
            patch(
                "subprocess.run",
                return_value=_make_completed_process(stdout="not json"),
            ),
        ):
            status = check_tailscale_status()
        assert status == TailscaleStatus(installed=True, connected=False)

    # --- BackendState variations ----------------------------------------

    def test_backend_state_stopped_returns_not_connected(self):
        with (
            patch("agent.tailscale._find_tailscale_binary", return_value="/usr/bin/tailscale"),
            patch(
                "subprocess.run",
                return_value=_make_completed_process(
                    stdout=_json_output(backend_state="Stopped")
                ),
            ),
        ):
            status = check_tailscale_status()
        assert status == TailscaleStatus(installed=True, connected=False)

    def test_backend_state_needs_login_returns_not_connected(self):
        with (
            patch("agent.tailscale._find_tailscale_binary", return_value="/usr/bin/tailscale"),
            patch(
                "subprocess.run",
                return_value=_make_completed_process(
                    stdout=_json_output(backend_state="NeedsLogin")
                ),
            ),
        ):
            status = check_tailscale_status()
        assert status == TailscaleStatus(installed=True, connected=False)

    # --- happy path -----------------------------------------------------

    def test_connected_returns_full_status(self):
        with (
            patch("agent.tailscale._find_tailscale_binary", return_value="/usr/bin/tailscale"),
            patch(
                "subprocess.run",
                return_value=_make_completed_process(stdout=_json_output()),
            ),
        ):
            status = check_tailscale_status()
        assert status.installed is True
        assert status.connected is True
        assert status.tailscale_ip == "100.64.0.1"
        assert status.hostname == "my-pc.tail-abc123.ts.net"

    def test_trailing_dot_stripped_from_dns_name(self):
        """DNSName from tailscale status --json includes a trailing dot."""
        with (
            patch("agent.tailscale._find_tailscale_binary", return_value="/usr/bin/tailscale"),
            patch(
                "subprocess.run",
                return_value=_make_completed_process(
                    stdout=_json_output(dns_name="home-pc.tail-xyz.ts.net.")
                ),
            ),
        ):
            status = check_tailscale_status()
        assert status.hostname == "home-pc.tail-xyz.ts.net"

    def test_prefers_100_dot_ip_over_ipv6(self):
        """Should pick the 100.x.x.x address when both IPv4 and IPv6 are listed."""
        with (
            patch("agent.tailscale._find_tailscale_binary", return_value="/usr/bin/tailscale"),
            patch(
                "subprocess.run",
                return_value=_make_completed_process(
                    stdout=_json_output(ips=["fd7a::1", "100.64.0.99"])
                ),
            ),
        ):
            status = check_tailscale_status()
        assert status.tailscale_ip == "100.64.0.99"

    def test_falls_back_to_first_ip_when_no_100_prefix(self):
        """If none of the IPs start with 100., use the first one."""
        with (
            patch("agent.tailscale._find_tailscale_binary", return_value="/usr/bin/tailscale"),
            patch(
                "subprocess.run",
                return_value=_make_completed_process(
                    stdout=_json_output(ips=["fd7a::1", "192.168.1.5"])
                ),
            ),
        ):
            status = check_tailscale_status()
        assert status.tailscale_ip == "fd7a::1"

    def test_no_ips_gives_none_tailscale_ip(self):
        with (
            patch("agent.tailscale._find_tailscale_binary", return_value="/usr/bin/tailscale"),
            patch(
                "subprocess.run",
                return_value=_make_completed_process(
                    stdout=_json_output(ips=[])
                ),
            ),
        ):
            status = check_tailscale_status()
        assert status.tailscale_ip is None

    def test_no_dns_name_gives_none_hostname(self):
        with (
            patch("agent.tailscale._find_tailscale_binary", return_value="/usr/bin/tailscale"),
            patch(
                "subprocess.run",
                return_value=_make_completed_process(
                    stdout=_json_output(dns_name="")
                ),
            ),
        ):
            status = check_tailscale_status()
        assert status.hostname is None

    def test_self_node_missing_gives_none_fields(self):
        """If the JSON has no 'Self' key, IP and hostname should be None."""
        data = json.dumps({"BackendState": "Running"})
        with (
            patch("agent.tailscale._find_tailscale_binary", return_value="/usr/bin/tailscale"),
            patch(
                "subprocess.run",
                return_value=_make_completed_process(stdout=data),
            ),
        ):
            status = check_tailscale_status()
        assert status.connected is True
        assert status.tailscale_ip is None
        assert status.hostname is None


# ---------------------------------------------------------------------------
# log_tailscale_advisory
# ---------------------------------------------------------------------------


class TestLogTailscaleAdvisory:
    def test_connected_logs_info_with_address(self, caplog):
        status = TailscaleStatus(
            installed=True,
            connected=True,
            tailscale_ip="100.64.0.1",
            hostname="home-pc.tail-abc123.ts.net",
        )
        with caplog.at_level("INFO", logger="hadcd.agent"):
            log_tailscale_advisory(status)

        assert any("Tailscale connected" in r.message for r in caplog.records)
        assert any("100.64.0.1" in r.message or "home-pc" in r.message for r in caplog.records)

    def test_connected_log_includes_hadcd_api_hint(self, caplog):
        status = TailscaleStatus(
            installed=True,
            connected=True,
            tailscale_ip="100.64.0.1",
            hostname="home-pc.tail-abc123.ts.net",
        )
        with caplog.at_level("INFO", logger="hadcd.agent"):
            log_tailscale_advisory(status)

        full_text = " ".join(r.message for r in caplog.records)
        assert "HADCD_API" in full_text
        assert ":8000" in full_text

    def test_installed_not_connected_logs_warning(self, caplog):
        status = TailscaleStatus(installed=True, connected=False)
        with caplog.at_level("WARNING", logger="hadcd.agent"):
            log_tailscale_advisory(status)

        assert any(r.levelname == "WARNING" for r in caplog.records)
        full_text = " ".join(r.message for r in caplog.records)
        assert "tailscale up" in full_text

    def test_not_installed_logs_info(self, caplog):
        status = TailscaleStatus(installed=False, connected=False)
        with caplog.at_level("INFO", logger="hadcd.agent"):
            log_tailscale_advisory(status)

        assert any(r.levelname == "INFO" for r in caplog.records)
        full_text = " ".join(r.message for r in caplog.records)
        assert "tailscale.com" in full_text

    def test_not_installed_does_not_warn(self, caplog):
        """Not installed is a quiet INFO; should never be WARNING or ERROR."""
        status = TailscaleStatus(installed=False, connected=False)
        with caplog.at_level("DEBUG", logger="hadcd.agent"):
            log_tailscale_advisory(status)

        assert not any(r.levelname in ("WARNING", "ERROR") for r in caplog.records)

    def test_connected_only_hostname_no_ip(self, caplog):
        """Connected but no Tailscale IP — should still log something useful."""
        status = TailscaleStatus(
            installed=True,
            connected=True,
            tailscale_ip=None,
            hostname="my-node.ts.net",
        )
        with caplog.at_level("INFO", logger="hadcd.agent"):
            log_tailscale_advisory(status)

        full_text = " ".join(r.message for r in caplog.records)
        assert "my-node.ts.net" in full_text

    def test_connected_only_ip_no_hostname(self, caplog):
        """Connected but no DNS name — should still log the IP."""
        status = TailscaleStatus(
            installed=True,
            connected=True,
            tailscale_ip="100.64.0.5",
            hostname=None,
        )
        with caplog.at_level("INFO", logger="hadcd.agent"):
            log_tailscale_advisory(status)

        full_text = " ".join(r.message for r in caplog.records)
        assert "100.64.0.5" in full_text
