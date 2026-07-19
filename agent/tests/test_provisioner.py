"""Tests for the setup-wizard hardening in agent/provisioner.py.

The provisioner intentionally binds all interfaces (the operator's phone
must reach it over the LAN), so its /save endpoint is the security
boundary. These tests pin the three protections added after the first
public review:

  * /save requires the console setup code (constant-time compared);
  * submitted values may not contain control characters (an embedded
    newline would inject extra agent.env keys — including executable
    paths that dep_check later runs);
  * request bodies are capped and a malformed Content-Length is a 400,
    not a crash.

A real HTTPServer runs on a loopback port; _write_env and systemctl are
patched out, so nothing touches /etc or services.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import HTTPServer
from unittest.mock import MagicMock

import pytest

import agent.provisioner as prov


@pytest.fixture()
def server(monkeypatch):
    """Loopback provisioner server with recorded _write_env + no systemctl."""
    written: list[dict] = []
    monkeypatch.setattr(prov, "_write_env", lambda updates: written.append(updates))
    monkeypatch.setattr(prov.subprocess, "Popen", MagicMock())
    monkeypatch.setattr(prov, "_setup_code", "123456")
    prov._done.clear()
    srv = HTTPServer(("127.0.0.1", 0), prov._Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield port, written
    finally:
        srv.shutdown()
        prov._done.clear()


def _post_save(port: int, fields: dict[str, str], headers: dict | None = None):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/save",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_save_without_code_is_rejected(server):
    port, written = server
    status, body = _post_save(port, {"node_name": "Intruder"})
    assert "setup code" in body.lower()
    assert written == []
    assert not prov._done.is_set()


def test_save_with_wrong_code_is_rejected(server):
    port, written = server
    status, body = _post_save(port, {"setup_code": "000000", "node_name": "X"})
    assert "setup code" in body.lower()
    assert written == []


def test_save_with_correct_code_writes_env(server):
    port, written = server
    status, body = _post_save(
        port,
        {"setup_code": "123456", "node_name": "Living Room PC",
         "hadcd_api": "http://100.1.2.3:8000"},
    )
    assert status == 200
    assert written == [{
        "NODE_NAME": "Living Room PC",
        "HADCD_API": "http://100.1.2.3:8000",
    }]
    assert prov._done.is_set()


def test_value_with_embedded_newline_is_rejected(server):
    port, written = server
    # parse_qs preserves encoded newlines inside values; unchecked, this
    # would append "XMRIG_PATH=/tmp/evil" as its own agent.env line.
    status, body = _post_save(
        port,
        {"setup_code": "123456",
         "node_name": "innocent\nXMRIG_PATH=/tmp/evil"},
    )
    assert "control characters" in body
    assert written == []


def test_oversized_body_is_rejected(server):
    port, written = server
    big = "x" * (prov.MAX_BODY_BYTES + 1)
    # The server responds 413 without reading the body; depending on
    # socket buffering the client either sees the 413 or an aborted
    # connection mid-upload. Both mean the body was never processed.
    try:
        status, _ = _post_save(port, {"setup_code": "123456", "node_name": big})
        assert status == 413
    except (ConnectionError, urllib.error.URLError):
        pass
    assert written == []


def test_malformed_content_length_is_400_not_crash(server):
    port, written = server
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.putrequest("POST", "/save")
    conn.putheader("Content-Type", "application/x-www-form-urlencoded")
    conn.putheader("Content-Length", "not-a-number")
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 400
    conn.close()
    assert written == []


def test_gate_disabled_when_code_is_none(server, monkeypatch):
    port, written = server
    monkeypatch.setattr(prov, "_setup_code", None)
    status, _ = _post_save(port, {"node_name": "NoGate"})
    assert status == 200
    assert written == [{"NODE_NAME": "NoGate"}]


def test_make_setup_code_variants(monkeypatch):
    monkeypatch.delenv("HADCD_SETUP_CODE", raising=False)
    code = prov._make_setup_code()
    assert code is not None and len(code) == 6 and code.isdigit()

    monkeypatch.setenv("HADCD_SETUP_CODE", "424242")
    assert prov._make_setup_code() == "424242"

    monkeypatch.setenv("HADCD_SETUP_CODE", "off")
    assert prov._make_setup_code() is None


def test_clean_env_value():
    assert prov._clean_env_value("  plain value  ") == "plain value"
    for bad in ("a\nb", "a\rb", "a\tb", "a\x00b", "a\x7fb"):
        with pytest.raises(ValueError):
            prov._clean_env_value(bad)
