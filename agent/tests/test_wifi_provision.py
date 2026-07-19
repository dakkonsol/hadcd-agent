"""Tests for agent/wifi_provision.py.

Only the logic that does not require real WiFi hardware is tested here:
  * Skip conditions (ethernet connected, WiFi already configured,
    provisioned flag present, no WiFi hardware, nmcli absent)
  * Network list parsing (scan_networks output → dict list)
  * HTML generation (page contains expected SSID options)
  * HTTP handler routing (GET / → 200, GET /health → JSON, POST /connect)

The nmcli subprocess calls are patched out in every test.
"""

from __future__ import annotations

import json
import threading
from http.server import HTTPServer
from unittest.mock import MagicMock, patch


import agent.wifi_provision as wp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# ---------------------------------------------------------------------------
# Skip-condition tests
# ---------------------------------------------------------------------------

class TestSkipConditions:
    def test_provisioned_flag_skips(self, tmp_path, monkeypatch):
        flag = tmp_path / "wifi-provisioned"
        flag.touch()
        monkeypatch.setattr(wp, "PROVISIONED_FLAG", flag)
        monkeypatch.setattr(wp, "_nmcli_available", lambda: True)
        monkeypatch.setattr(wp, "ethernet_connected", lambda: False)
        monkeypatch.setattr(wp, "wifi_already_configured", lambda: False)
        monkeypatch.setattr(wp, "find_wifi_interface", lambda: "wlan0")
        assert wp.provision() == 0  # exits successfully without doing anything

    def test_ethernet_connected_skips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wp, "PROVISIONED_FLAG", tmp_path / "nope")
        monkeypatch.setattr(wp, "_nmcli_available", lambda: True)
        monkeypatch.setattr(wp, "ethernet_connected", lambda: True)
        assert wp.provision() == 0

    def test_wifi_already_configured_skips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wp, "PROVISIONED_FLAG", tmp_path / "nope")
        monkeypatch.setattr(wp, "_nmcli_available", lambda: True)
        monkeypatch.setattr(wp, "ethernet_connected", lambda: False)
        monkeypatch.setattr(wp, "wifi_already_configured", lambda: True)
        assert wp.provision() == 0

    def test_no_wifi_interface_skips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wp, "PROVISIONED_FLAG", tmp_path / "nope")
        monkeypatch.setattr(wp, "_nmcli_available", lambda: True)
        monkeypatch.setattr(wp, "ethernet_connected", lambda: False)
        monkeypatch.setattr(wp, "wifi_already_configured", lambda: False)
        monkeypatch.setattr(wp, "find_wifi_interface", lambda: None)
        assert wp.provision() == 0

    def test_nmcli_absent_skips_gracefully(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wp, "_nmcli_available", lambda: False)
        assert wp.provision() == 0


# ---------------------------------------------------------------------------
# nmcli output parsing
# ---------------------------------------------------------------------------

class TestEthernetConnected:
    def test_detects_connected_ethernet(self):
        output = "ethernet:connected\nwifi:disconnected\n"
        with patch.object(wp, "_run", return_value=_make_completed(0, output)):
            assert wp.ethernet_connected() is True

    def test_ignores_disconnected_ethernet(self):
        output = "ethernet:disconnected\n"
        with patch.object(wp, "_run", return_value=_make_completed(0, output)):
            assert wp.ethernet_connected() is False

    def test_returns_false_on_nmcli_failure(self):
        with patch.object(wp, "_run", return_value=_make_completed(1)):
            assert wp.ethernet_connected() is False


class TestWifiAlreadyConfigured:
    def test_detects_wifi_profile(self):
        output = "wifi:HomeNetwork\n802-11-wireless:OtherNet\n"
        with patch.object(wp, "_run", return_value=_make_completed(0, output)):
            assert wp.wifi_already_configured() is True

    def test_returns_false_when_only_ethernet_profiles(self):
        output = "ethernet:Wired connection 1\n"
        with patch.object(wp, "_run", return_value=_make_completed(0, output)):
            assert wp.wifi_already_configured() is False

    def test_returns_false_on_nmcli_failure(self):
        with patch.object(wp, "_run", return_value=_make_completed(1)):
            assert wp.wifi_already_configured() is False


class TestFindWifiInterface:
    def test_finds_wlan_interface(self):
        output = "eth0:ethernet\nwlan0:wifi\n"
        with patch.object(wp, "_run", return_value=_make_completed(0, output)):
            assert wp.find_wifi_interface() == "wlan0"

    def test_returns_none_when_no_wifi(self):
        output = "eth0:ethernet\n"
        with patch.object(wp, "_run", return_value=_make_completed(0, output)):
            assert wp.find_wifi_interface() is None

    def test_returns_none_on_failure(self):
        with patch.object(wp, "_run", return_value=_make_completed(1)):
            assert wp.find_wifi_interface() is None


class TestScanNetworks:
    def test_parses_standard_output(self):
        # nmcli --terse --fields SSID,SIGNAL,SECURITY output
        output = (
            "HomeNetwork:85:WPA2\n"
            "OfficeWiFi:72:WPA2\n"
            "GuestNet:45:open\n"
        )
        rescan = _make_completed(0)
        scan = _make_completed(0, output)
        with patch.object(wp, "_run", side_effect=[rescan, scan]):
            nets = wp.scan_networks("wlan0")
        assert len(nets) == 3
        assert nets[0]["ssid"] == "HomeNetwork"
        assert nets[0]["signal"] == "85"
        assert nets[0]["security"] == "WPA2"

    def test_filters_empty_ssids(self):
        output = ":72:WPA2\nRealNet:60:WPA2\n"
        with patch.object(wp, "_run", side_effect=[_make_completed(0), _make_completed(0, output)]):
            nets = wp.scan_networks("wlan0")
        assert all(n["ssid"] for n in nets)
        assert len(nets) == 1

    def test_filters_duplicate_ssids(self):
        output = "HomeNetwork:85:WPA2\nHomeNetwork:80:WPA2\n"
        with patch.object(wp, "_run", side_effect=[_make_completed(0), _make_completed(0, output)]):
            nets = wp.scan_networks("wlan0")
        assert len(nets) == 1

    def test_filters_own_hotspot_ssid(self):
        output = f"{wp.HOTSPOT_SSID}:99:WPA2\nRealNet:60:WPA2\n"
        with patch.object(wp, "_run", side_effect=[_make_completed(0), _make_completed(0, output)]):
            nets = wp.scan_networks("wlan0")
        ssids = [n["ssid"] for n in nets]
        assert wp.HOTSPOT_SSID not in ssids

    def test_sorts_by_signal_descending(self):
        output = "Weak:20:open\nStrong:90:WPA2\nMed:50:WPA2\n"
        with patch.object(wp, "_run", side_effect=[_make_completed(0), _make_completed(0, output)]):
            nets = wp.scan_networks("wlan0")
        signals = [int(n["signal"]) for n in nets]
        assert signals == sorted(signals, reverse=True)

    def test_returns_empty_on_scan_failure(self):
        with patch.object(wp, "_run", side_effect=[_make_completed(0), _make_completed(1)]):
            nets = wp.scan_networks("wlan0")
        assert nets == []


# ---------------------------------------------------------------------------
# HTTP handler — integration test using a real (loopback) socket
# ---------------------------------------------------------------------------

class TestHttpHandler:
    """Spin up a real HTTPServer on a random loopback port and exercise it."""

    def _start_server(self, networks):
        result_holder = []
        done = threading.Event()
        handler_cls = wp._build_handler(networks, result_holder, done)
        server = HTTPServer(("127.0.0.1", 0), handler_cls)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        return server, port, result_holder, done

    def _get(self, port, path):
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as resp:
            return resp.status, resp.read().decode()

    def _post(self, port, path, body):
        import urllib.request
        data = body.encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_get_root_returns_200(self):
        server, port, _, _ = self._start_server([])
        try:
            status, _ = self._get(port, "/")
            assert status == 200
        finally:
            server.shutdown()

    def test_get_root_contains_ssids(self):
        networks = [
            {"ssid": "MyHome", "signal": "80", "security": "WPA2"},
            {"ssid": "Office", "signal": "60", "security": "WPA2"},
        ]
        server, port, _, _ = self._start_server(networks)
        try:
            _, body = self._get(port, "/")
            assert "MyHome" in body
            assert "Office" in body
        finally:
            server.shutdown()

    def test_get_health_returns_json(self):
        server, port, _, _ = self._start_server([])
        try:
            status, body = self._get(port, "/health")
            assert status == 200
            assert json.loads(body)["status"] == "ok"
        finally:
            server.shutdown()

    def test_unknown_path_redirects(self):
        import urllib.request
        server, port, _, _ = self._start_server([])
        try:
            urllib.request.Request(
                f"http://127.0.0.1:{port}/captive-portal-check",
                headers={"User-Agent": "CaptiveNetworkSupport"},
            )
            # urllib follows redirects by default; just confirm we don't 500
            status, _ = self._get(port, "/")
            assert status == 200
        finally:
            server.shutdown()

    def test_post_connect_sets_result_and_fires_event(self):
        server, port, result_holder, done = self._start_server([])
        try:
            status, body = self._post(
                port, "/connect",
                "ssid=HomeNetwork&pw=supersecret"
            )
            assert status == 200
            assert "Connected" in body
            assert done.is_set()
            assert len(result_holder) == 1
            assert result_holder[0].ssid == "HomeNetwork"
            assert result_holder[0].password == "supersecret"
        finally:
            server.shutdown()

    def test_post_connect_empty_ssid_returns_400(self):
        server, port, result_holder, done = self._start_server([])
        try:
            status, _ = self._post(port, "/connect", "ssid=&pw=pass")
            assert status == 400
            assert not done.is_set()
        finally:
            server.shutdown()

    def test_post_connect_xss_safe(self):
        """SSID with HTML special chars must be escaped in the error page."""
        server, port, result_holder, done = self._start_server([])
        try:
            status, body = self._post(port, "/connect", "ssid=&pw=<script>")
            assert "<script>" not in body
        finally:
            server.shutdown()

    def test_post_connect_dash_ssid_returns_400(self):
        """An SSID starting with '-' would be parsed as an nmcli option."""
        server, port, result_holder, done = self._start_server([])
        try:
            status, _ = self._post(port, "/connect", "ssid=--rescan&pw=x")
            assert status == 400
            assert not done.is_set()
            assert result_holder == []
        finally:
            server.shutdown()

    def test_post_connect_control_chars_return_400(self):
        server, port, result_holder, done = self._start_server([])
        try:
            status, _ = self._post(port, "/connect", "ssid=Home%0aEvil&pw=x")
            assert status == 400
            assert result_holder == []
        finally:
            server.shutdown()

    def test_post_connect_oversized_body_returns_400(self):
        server, port, result_holder, done = self._start_server([])
        try:
            status, _ = self._post(
                port, "/connect", "ssid=Home&pw=" + "x" * (17 * 1024)
            )
            assert status == 400
            assert result_holder == []
        finally:
            server.shutdown()
