"""Security-boundary tests for the container handler.

These pin the node-side security floor: the dispatcher writes the task
payload, so nothing in the payload may widen what runs on this host.
Concretely:

  * bind-mount sources must be locally allowlisted (the agent's own
    blob-staging dirs always are; HADCD_MOUNT_ALLOWLIST adds more);
  * ``network_mode="host"`` is refused outright;
  * a ``hardened`` job gets the full floor (cap_drop=ALL,
    no-new-privileges, unprivileged, isolated bridge) even when the
    profile fields were stripped from the payload;
  * HADCD_REQUIRE_HARDENED forces that floor for every task.

All Docker SDK calls are mocked — no daemon needed.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hadcd_workloads import run_registered
from hadcd_workloads.container import _HARDENED_DEFAULT_NETWORK


def _mock_client(exit_code: int = 0):
    container = MagicMock()
    container.wait.return_value = {"StatusCode": exit_code}
    container.logs.side_effect = [b"", b""]
    client = MagicMock()
    client.containers.run.return_value = container
    return client


def _staging_dir() -> Path:
    """A real dir matching the agent's blob-staging naming."""
    return Path(tempfile.mkdtemp(prefix="hadcd-blobs-"))


# ── Mount allowlist ────────────────────────────────────────────────────────


def test_payload_host_mount_outside_allowlist_is_rejected(monkeypatch):
    monkeypatch.delenv("HADCD_MOUNT_ALLOWLIST", raising=False)
    client = _mock_client()
    with patch("docker.from_env", return_value=client):
        with pytest.raises(ValueError, match="not allowlisted"):
            run_registered(
                "container",
                {
                    "image": "busybox",
                    "volumes": [
                        {"host_path": "/etc", "container_path": "/host-etc",
                         "mode": "rw"},
                    ],
                },
            )
    client.containers.run.assert_not_called()


def test_payload_output_dir_outside_allowlist_is_rejected(monkeypatch):
    monkeypatch.delenv("HADCD_MOUNT_ALLOWLIST", raising=False)
    client = _mock_client()
    with patch("docker.from_env", return_value=client):
        with pytest.raises(ValueError, match="not allowlisted"):
            run_registered(
                "container",
                {"image": "busybox", "output_dir": "/var/lib/hadcd-agent"},
            )
    client.containers.run.assert_not_called()


def test_agent_blob_staging_dirs_are_always_mountable(monkeypatch):
    monkeypatch.delenv("HADCD_MOUNT_ALLOWLIST", raising=False)
    staging = _staging_dir()
    (staging / "input").mkdir()
    client = _mock_client()
    with patch("docker.from_env", return_value=client):
        result = run_registered(
            "container",
            {
                "image": "busybox",
                "volumes": [
                    {"host_path": str(staging / "input"),
                     "container_path": "/input", "mode": "ro"},
                ],
                "output_dir": str(staging / "output"),
            },
        )
    assert result["exit_code"] == 0
    client.containers.run.assert_called_once()


def test_operator_allowlist_env_permits_extra_prefixes(monkeypatch, tmp_path):
    data_dir = tmp_path / "models"
    data_dir.mkdir()
    monkeypatch.setenv("HADCD_MOUNT_ALLOWLIST", str(tmp_path))
    client = _mock_client()
    with patch("docker.from_env", return_value=client):
        run_registered(
            "container",
            {
                "image": "busybox",
                "volumes": [
                    {"host_path": str(data_dir),
                     "container_path": "/models", "mode": "ro"},
                ],
            },
        )
    client.containers.run.assert_called_once()


# ── Network floor ──────────────────────────────────────────────────────────


def test_host_network_mode_is_refused(monkeypatch):
    monkeypatch.delenv("HADCD_REQUIRE_HARDENED", raising=False)
    client = _mock_client()
    with patch("docker.from_env", return_value=client):
        with pytest.raises(ValueError, match="host"):
            run_registered(
                "container",
                {"image": "busybox", "network_mode": "host"},
            )
    client.containers.run.assert_not_called()


# ── Hardened floor ─────────────────────────────────────────────────────────


def test_hardened_floor_applies_even_when_profile_is_stripped(monkeypatch):
    """`hardened: true` alone must yield the full locked-down floor."""
    monkeypatch.delenv("HADCD_REQUIRE_HARDENED", raising=False)
    client = _mock_client()
    with patch("docker.from_env", return_value=client):
        run_registered(
            "container",
            {"image": "busybox", "hardened": True},
        )
    kwargs = client.containers.run.call_args.kwargs
    assert kwargs["privileged"] is False
    assert kwargs["cap_drop"] == ["ALL"]
    assert any(
        str(o).startswith("no-new-privileges") for o in kwargs["security_opt"]
    )
    assert kwargs["network"] == _HARDENED_DEFAULT_NETWORK


def test_require_hardened_env_forces_floor_for_unmarked_tasks(monkeypatch):
    monkeypatch.setenv("HADCD_REQUIRE_HARDENED", "1")
    client = _mock_client()
    with patch("docker.from_env", return_value=client):
        run_registered("container", {"image": "busybox"})
    kwargs = client.containers.run.call_args.kwargs
    assert kwargs["privileged"] is False
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["network"] == _HARDENED_DEFAULT_NETWORK


def test_operator_task_without_profile_keeps_docker_defaults(monkeypatch):
    """No profile, no env override → same daemon call as before the floor."""
    monkeypatch.delenv("HADCD_REQUIRE_HARDENED", raising=False)
    client = _mock_client()
    with patch("docker.from_env", return_value=client):
        run_registered("container", {"image": "busybox", "command": "true"})
    kwargs = client.containers.run.call_args.kwargs
    assert "cap_drop" not in kwargs
    assert "security_opt" not in kwargs
    assert "privileged" not in kwargs
    assert "network" not in kwargs
