"""Unit tests for the Phase 9b mining fill handlers.

Tests cover both handlers:
  - gpu_mining_fill  (NiceHash excavator)
  - p2pool_fill      (XMRig → P2Pool)

All tests are pure unit tests — no real binaries, no GPU, no network.
External calls (subprocess, nvidia-smi, psutil) are mocked.

Coverage:
  - Skip gracefully when binary path not configured
  - Skip gracefully when wallet not configured
  - Skip gracefully when binary file not found on disk
  - Happy-path subprocess management (launch, wait, terminate)
  - Returns correctly-shaped result dict
  - CSV session log is written after a session
  - GPU pressure detection: suspend on pressure, resume when clear
  - CSV log truncates wallet to 8 chars + ellipsis
"""

from __future__ import annotations

import csv
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# GPU mining fill handler tests
# ---------------------------------------------------------------------------

class TestGpuMiningFillSkip:
    """Handler returns {"skipped": True} when not configured."""

    def test_skip_when_excavator_path_empty(self):
        from hadcd_workloads.gpu_mining_fill import run_gpu_mining_fill
        with patch.dict(os.environ, {
            "NICEHASH_TREX_PATH": "",
            "NICEHASH_WALLET": "abc123wallet",
        }, clear=False):
            result = run_gpu_mining_fill({"duration_sec": 10})
        assert result["skipped"] is True
        assert "NICEHASH_TREX_PATH" in result["reason"]  # matches handler message

    def test_skip_when_wallet_empty(self, tmp_path):
        fake_exe = tmp_path / "excavator"
        fake_exe.touch()
        from hadcd_workloads.gpu_mining_fill import run_gpu_mining_fill
        with patch.dict(os.environ, {
            "NICEHASH_TREX_PATH": str(fake_exe),
            "NICEHASH_WALLET": "",
        }, clear=False):
            result = run_gpu_mining_fill({"duration_sec": 10})
        assert result["skipped"] is True
        assert "NICEHASH_WALLET" in result["reason"]

    def test_skip_when_binary_not_found(self, tmp_path):
        missing = tmp_path / "no_excavator_here"
        from hadcd_workloads.gpu_mining_fill import run_gpu_mining_fill
        with patch.dict(os.environ, {
            "NICEHASH_TREX_PATH": str(missing),
            "NICEHASH_WALLET": "abc123wallet",
        }, clear=False):
            result = run_gpu_mining_fill({"duration_sec": 10})
        assert result["skipped"] is True
        assert "t-rex not found" in result["reason"]


class TestGpuMiningFillHappyPath:
    """Happy-path execution with a fake excavator binary."""

    def _run(self, tmp_path, extra_env: dict | None = None, duration_sec: float = 0.1) -> dict:
        fake_exe = tmp_path / "excavator"
        fake_exe.touch()
        log_path = tmp_path / "gpu_sessions.csv"

        env = {
            "NICEHASH_TREX_PATH": str(fake_exe),
            "NICEHASH_WALLET": "abcdefghijklmnop",
            "NICEHASH_WORKER_NAME": "test-worker",
            "NICEHASH_POOL_HOST": "pool.test",
            "NICEHASH_POOL_PORT": "9200",
            "MINING_GPU_INDEX": "0",
            "MINING_GPU_PRESSURE_PCT": "20",
            "MINING_GPU_RESUME_PCT": "10",
            "MINING_POLL_INTERVAL_SEC": "0.05",
            "MINING_PAYOUT_LOG": str(log_path),
        }
        if extra_env:
            env.update(extra_env)

        fake_proc = MagicMock()
        fake_proc.pid = 12345
        fake_proc.poll.return_value = None  # still running
        fake_proc.returncode = 0

        from hadcd_workloads import gpu_mining_fill as mod

        with patch.dict(os.environ, env, clear=False), \
             patch.object(mod, "subprocess") as mock_sub, \
             patch.object(mod, "_compute_pids_excluding", return_value=[]), \
             patch.object(mod, "_gpu_model", return_value="RTX 4060"):

            mock_sub.Popen.return_value = fake_proc
            mock_sub.DEVNULL = subprocess.DEVNULL
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired

            # Let it run for duration_sec (very short in tests)
            result = mod.run_gpu_mining_fill({"duration_sec": duration_sec})

        return result, log_path

    def test_returns_dict(self, tmp_path):
        result, _ = self._run(tmp_path)
        assert isinstance(result, dict)
        assert "skipped" not in result

    def test_result_has_required_keys(self, tmp_path):
        result, _ = self._run(tmp_path)
        for key in ("worker", "gpu_model", "pool_host",
                    "duration_requested_sec", "active_mining_sec",
                    "paused_sec", "session_start", "session_end"):
            assert key in result, f"Missing key: {key}"

    def test_result_worker_name(self, tmp_path):
        result, _ = self._run(tmp_path)
        assert result["worker"] == "test-worker"

    def test_result_gpu_model(self, tmp_path):
        result, _ = self._run(tmp_path)
        assert result["gpu_model"] == "RTX 4060"

    def test_result_pool_host(self, tmp_path):
        result, _ = self._run(tmp_path)
        assert result["pool_host"] == "pool.test"

    def test_result_duration_requested(self, tmp_path):
        result, _ = self._run(tmp_path, duration_sec=0.1)
        assert result["duration_requested_sec"] == pytest.approx(0.1)

    def test_csv_written(self, tmp_path):
        _, log_path = self._run(tmp_path)
        assert log_path.exists()

    def test_csv_has_header_and_row(self, tmp_path):
        _, log_path = self._run(tmp_path)
        rows = list(csv.reader(log_path.open()))
        assert len(rows) >= 2  # header + at least one data row

    def test_csv_header_columns(self, tmp_path):
        _, log_path = self._run(tmp_path)
        rows = list(csv.reader(log_path.open()))
        header = rows[0]
        assert "start_utc" in header
        assert "worker_name" in header
        assert "wallet_prefix" in header

    def test_csv_wallet_truncated(self, tmp_path):
        """Only 8 chars of wallet appear in the CSV, plus the ellipsis."""
        _, log_path = self._run(tmp_path)
        content = log_path.read_text()
        # "abcdefgh…" — first 8 chars of "abcdefghijklmnop"
        assert "abcdefgh" in content
        assert "ijklmnop" not in content


class TestGpuMiningFillGpuPressure:
    """Miner is suspended when GPU pressure is detected."""

    def test_suspend_on_pressure(self, tmp_path):
        fake_exe = tmp_path / "excavator"
        fake_exe.touch()

        env = {
            "NICEHASH_TREX_PATH": str(fake_exe),
            "NICEHASH_WALLET": "walletaddr",
            "NICEHASH_WORKER_NAME": "w",
            "NICEHASH_POOL_HOST": "pool.test",
            "NICEHASH_POOL_PORT": "9200",
            "MINING_GPU_INDEX": "0",
            "MINING_GPU_PRESSURE_PCT": "20",
            "MINING_GPU_RESUME_PCT": "10",
            "MINING_POLL_INTERVAL_SEC": "0.01",
            "MINING_PAYOUT_LOG": str(tmp_path / "gpu.csv"),
        }

        fake_proc = MagicMock()
        fake_proc.pid = 99999
        fake_proc.poll.return_value = None

        suspend_calls = []
        resume_calls = []

        from hadcd_workloads import gpu_mining_fill as mod

        # Simulate: first tick has pressure (non-miner pid 111),
        # subsequent ticks are clear.
        call_count = [0]

        def _pids(_miner_pid):
            call_count[0] += 1
            if call_count[0] == 1:
                return [111]  # pressure on first poll
            return []

        with patch.dict(os.environ, env, clear=False), \
             patch.object(mod, "subprocess") as mock_sub, \
             patch.object(mod, "_compute_pids_excluding", side_effect=_pids), \
             patch.object(mod, "_gpu_model", return_value="RTX 4060"), \
             patch.object(mod, "_suspend", side_effect=lambda p: suspend_calls.append(p)), \
             patch.object(mod, "_resume", side_effect=lambda p: resume_calls.append(p)):

            mock_sub.Popen.return_value = fake_proc
            mock_sub.DEVNULL = subprocess.DEVNULL
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired

            mod.run_gpu_mining_fill({"duration_sec": 0.1})

        assert len(suspend_calls) >= 1, "Expected at least one suspend call"
        assert len(resume_calls) >= 1, "Expected at least one resume call"


# ---------------------------------------------------------------------------
# P2Pool / XMRig handler tests
# ---------------------------------------------------------------------------

class TestP2poolFillSkip:
    """Handler returns {"skipped": True} when not configured."""

    def test_skip_when_xmrig_path_empty(self):
        from hadcd_workloads.p2pool_fill import run_p2pool_fill
        with patch.dict(os.environ, {
            "XMRIG_PATH": "",
            "XMR_WALLET_ADDRESS": "walletaddr",
        }, clear=False):
            result = run_p2pool_fill({"duration_sec": 10})
        assert result["skipped"] is True
        assert "XMRIG_PATH" in result["reason"]

    def test_skip_when_wallet_empty(self, tmp_path):
        fake_xmrig = tmp_path / "xmrig"
        fake_xmrig.touch()
        from hadcd_workloads.p2pool_fill import run_p2pool_fill
        with patch.dict(os.environ, {
            "XMRIG_PATH": str(fake_xmrig),
            "XMR_WALLET_ADDRESS": "",
        }, clear=False):
            result = run_p2pool_fill({"duration_sec": 10})
        assert result["skipped"] is True
        assert "XMR_WALLET_ADDRESS" in result["reason"]

    def test_skip_when_binary_not_found(self, tmp_path):
        missing = tmp_path / "no_xmrig_here"
        from hadcd_workloads.p2pool_fill import run_p2pool_fill
        with patch.dict(os.environ, {
            "XMRIG_PATH": str(missing),
            "XMR_WALLET_ADDRESS": "walletaddr",
        }, clear=False):
            result = run_p2pool_fill({"duration_sec": 10})
        assert result["skipped"] is True
        assert "xmrig not found" in result["reason"]


class TestP2poolFillHappyPath:
    """Happy-path execution with a fake xmrig binary."""

    def _run(self, tmp_path, duration_sec: float = 0.1) -> tuple[dict, Path]:
        fake_xmrig = tmp_path / "xmrig"
        fake_xmrig.touch()
        log_path = tmp_path / "cpu_sessions.csv"

        env = {
            "XMRIG_PATH": str(fake_xmrig),
            "XMR_WALLET_ADDRESS": "xmrwallet1234567890",
            "XMRIG_WORKER_NAME": "cpu-worker",
            "P2POOL_NODE_URL": "p2pool.test:3333",
            "XMRIG_THREADS": "4",
            "CPU_MINING_POLL_SEC": "0.05",
            "CPU_MINING_PAYOUT_LOG": str(log_path),
        }

        fake_proc = MagicMock()
        fake_proc.pid = 23456
        fake_proc.poll.return_value = None
        fake_proc.returncode = 0

        from hadcd_workloads import p2pool_fill as mod

        with patch.dict(os.environ, env, clear=False), \
             patch.object(mod, "subprocess") as mock_sub:

            mock_sub.Popen.return_value = fake_proc
            mock_sub.DEVNULL = subprocess.DEVNULL
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired

            result = mod.run_p2pool_fill({"duration_sec": duration_sec})

        return result, log_path

    def test_returns_dict(self, tmp_path):
        result, _ = self._run(tmp_path)
        assert isinstance(result, dict)
        assert "skipped" not in result

    def test_result_has_required_keys(self, tmp_path):
        result, _ = self._run(tmp_path)
        for key in ("worker", "pool_url", "threads",
                    "duration_requested_sec", "actual_sec",
                    "session_start", "session_end"):
            assert key in result, f"Missing key: {key}"

    def test_result_worker_name(self, tmp_path):
        result, _ = self._run(tmp_path)
        assert result["worker"] == "cpu-worker"

    def test_result_pool_url(self, tmp_path):
        result, _ = self._run(tmp_path)
        assert result["pool_url"] == "p2pool.test:3333"

    def test_result_threads(self, tmp_path):
        result, _ = self._run(tmp_path)
        assert result["threads"] == 4

    def test_result_duration_requested(self, tmp_path):
        result, _ = self._run(tmp_path, duration_sec=0.1)
        assert result["duration_requested_sec"] == pytest.approx(0.1)

    def test_csv_written(self, tmp_path):
        _, log_path = self._run(tmp_path)
        assert log_path.exists()

    def test_csv_header_columns(self, tmp_path):
        _, log_path = self._run(tmp_path)
        rows = list(csv.reader(log_path.open()))
        header = rows[0]
        assert "start_utc" in header
        assert "threads" in header
        assert "wallet_prefix" in header

    def test_csv_wallet_truncated(self, tmp_path):
        """Only 8 chars of wallet appear in the CSV, plus the ellipsis."""
        _, log_path = self._run(tmp_path)
        content = log_path.read_text()
        assert "xmrwalle" in content
        assert "1234567890" not in content

    def test_csv_appends_on_second_run(self, tmp_path):
        """A second run appends a row, not a second header."""
        self._run(tmp_path)
        self._run(tmp_path)
        log_path = tmp_path / "cpu_sessions.csv"
        rows = list(csv.reader(log_path.open()))
        # Header once + 2 data rows
        headers = [r for r in rows if "start_utc" in r]
        assert len(headers) == 1, "Header should appear only once"
        data_rows = [r for r in rows if r and "start_utc" not in r]
        assert len(data_rows) == 2


class TestP2poolFillAutoThreads:
    """XMRIG_THREADS=0 triggers auto-detection (cpu_count - 1)."""

    def test_zero_threads_triggers_auto(self, tmp_path):
        fake_xmrig = tmp_path / "xmrig"
        fake_xmrig.touch()
        log_path = tmp_path / "cpu.csv"

        env = {
            "XMRIG_PATH": str(fake_xmrig),
            "XMR_WALLET_ADDRESS": "walletaddr",
            "XMRIG_WORKER_NAME": "w",
            "XMRIG_THREADS": "0",
            "CPU_MINING_POLL_SEC": "0.05",
            "CPU_MINING_PAYOUT_LOG": str(log_path),
        }

        fake_proc = MagicMock()
        fake_proc.pid = 1
        fake_proc.poll.return_value = None

        from hadcd_workloads import p2pool_fill as mod

        with patch.dict(os.environ, env, clear=False), \
             patch.object(mod, "subprocess") as mock_sub, \
             patch.object(mod, "_default_threads", return_value=7) as mock_dt:

            mock_sub.Popen.return_value = fake_proc
            mock_sub.DEVNULL = subprocess.DEVNULL
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired

            result = mod.run_p2pool_fill({"duration_sec": 0.1})

        assert result["threads"] == 7


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------

class TestMiningHandlersRegistered:
    """Both handlers must be registered in the workload registry."""

    def test_gpu_mining_fill_registered(self):
        import hadcd_workloads  # triggers @register decorators
        from hadcd_workloads.registry import _REGISTRY
        assert "gpu_mining_fill" in _REGISTRY

    def test_p2pool_fill_registered(self):
        import hadcd_workloads
        from hadcd_workloads.registry import _REGISTRY
        assert "p2pool_fill" in _REGISTRY

    def test_run_registered_calls_gpu_handler(self):
        import hadcd_workloads
        from hadcd_workloads.registry import run_registered
        with patch.dict(os.environ, {
            "NICEHASH_TREX_PATH": "",
            "NICEHASH_WALLET": "",
        }, clear=False):
            # Skips gracefully — confirms dispatch plumbing works
            result = run_registered("gpu_mining_fill", {"duration_sec": 1})
        assert "skipped" in result

    def test_run_registered_calls_p2pool_handler(self):
        import hadcd_workloads
        from hadcd_workloads.registry import run_registered
        with patch.dict(os.environ, {
            "XMRIG_PATH": "",
            "XMR_WALLET_ADDRESS": "",
        }, clear=False):
            result = run_registered("p2pool_fill", {"duration_sec": 1})
        assert "skipped" in result
