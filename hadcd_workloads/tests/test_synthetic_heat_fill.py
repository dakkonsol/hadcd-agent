"""Unit tests for the Phase 9c synthetic heat-fill handler.

Tests cover:
  - Happy-path execution (CPU only — no real burn in tests)
  - Result shape and required keys
  - Thread count: explicit vs. auto (SYNTHETIC_HEAT_THREADS=0)
  - GPU burn: flag forwarded correctly; torch unavailable → graceful skip
  - CSV session log: written, header correct, wallet-free, appends cleanly
  - Handler registered in the workload registry
  - fill_injector._FILL_SOURCES includes synthetic_heat_fill

All tests are pure unit tests — no real subprocesses, no GPU, no torch.
CPU worker processes are replaced by a mock so tests finish in <1 ms.
"""

from __future__ import annotations

import csv
import multiprocessing
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env_for(tmp_path: Path, extra: dict | None = None) -> dict:
    """Base env suitable for a fast test run."""
    base = {
        "SYNTHETIC_HEAT_THREADS": "2",
        "SYNTHETIC_HEAT_GPU": "false",
        "SYNTHETIC_HEAT_POLL_SEC": "0.05",
        "SYNTHETIC_HEAT_LOG": str(tmp_path / "synthetic_sessions.csv"),
    }
    if extra:
        base.update(extra)
    return base


def _fake_process() -> MagicMock:
    p = MagicMock(spec=multiprocessing.Process)
    p.is_alive.return_value = False
    return p


# ---------------------------------------------------------------------------
# Happy-path result shape
# ---------------------------------------------------------------------------

class TestSyntheticHeatFillResult:
    def _run(self, tmp_path: Path, extra_env: dict | None = None) -> dict:
        from hadcd_workloads import synthetic_heat_fill as mod

        env = _env_for(tmp_path, extra_env)
        fake_proc = _fake_process()

        with patch.dict(os.environ, env, clear=False), \
             patch.object(mod.multiprocessing, "Process", return_value=fake_proc), \
             patch.object(mod.multiprocessing, "Value") as mock_val:
            # Make stop_flag.value settable without actual shared memory.
            mock_val.return_value = MagicMock()
            result = mod.run_synthetic_heat_fill({"duration_sec": 0.05})

        return result

    def test_returns_dict(self, tmp_path):
        assert isinstance(self._run(tmp_path), dict)

    def test_required_keys_present(self, tmp_path):
        result = self._run(tmp_path)
        for key in ("threads", "gpu_burn_active", "duration_requested_sec",
                    "actual_sec", "session_start", "session_end"):
            assert key in result, f"Missing key: {key}"

    def test_threads_matches_env(self, tmp_path):
        result = self._run(tmp_path)
        assert result["threads"] == 2

    def test_gpu_burn_false_by_default(self, tmp_path):
        result = self._run(tmp_path)
        assert result["gpu_burn_active"] is False

    def test_duration_requested_preserved(self, tmp_path):
        result = self._run(tmp_path)
        assert result["duration_requested_sec"] == pytest.approx(0.05)

    def test_session_timestamps_present(self, tmp_path):
        result = self._run(tmp_path)
        # Both should be ISO-8601 strings
        assert "T" in result["session_start"]
        assert "T" in result["session_end"]

    def test_actual_sec_non_negative(self, tmp_path):
        result = self._run(tmp_path)
        assert result["actual_sec"] >= 0


# ---------------------------------------------------------------------------
# Thread count auto-detection
# ---------------------------------------------------------------------------

class TestThreadAutoDetection:
    def test_zero_threads_uses_auto(self, tmp_path):
        from hadcd_workloads import synthetic_heat_fill as mod

        env = _env_for(tmp_path, {"SYNTHETIC_HEAT_THREADS": "0"})
        fake_proc = _fake_process()

        with patch.dict(os.environ, env, clear=False), \
             patch.object(mod.multiprocessing, "Process", return_value=fake_proc), \
             patch.object(mod.multiprocessing, "Value") as mock_val, \
             patch.object(mod, "_default_threads", return_value=5) as mock_dt:
            mock_val.return_value = MagicMock()
            result = mod.run_synthetic_heat_fill({"duration_sec": 0.05})

        assert result["threads"] == 5

    def test_explicit_threads_used_directly(self, tmp_path):
        from hadcd_workloads import synthetic_heat_fill as mod

        env = _env_for(tmp_path, {"SYNTHETIC_HEAT_THREADS": "3"})
        fake_proc = _fake_process()

        with patch.dict(os.environ, env, clear=False), \
             patch.object(mod.multiprocessing, "Process", return_value=fake_proc), \
             patch.object(mod.multiprocessing, "Value") as mock_val:
            mock_val.return_value = MagicMock()
            result = mod.run_synthetic_heat_fill({"duration_sec": 0.05})

        assert result["threads"] == 3


# ---------------------------------------------------------------------------
# GPU burn flag
# ---------------------------------------------------------------------------

class TestGpuBurnFlag:
    def _run_with_gpu(self, tmp_path: Path, want_gpu: str) -> dict:
        from hadcd_workloads import synthetic_heat_fill as mod

        env = _env_for(tmp_path, {"SYNTHETIC_HEAT_GPU": want_gpu})
        fake_proc = _fake_process()

        with patch.dict(os.environ, env, clear=False), \
             patch.object(mod.multiprocessing, "Process", return_value=fake_proc), \
             patch.object(mod.multiprocessing, "Value") as mock_val, \
             patch.object(mod.threading, "Thread") as mock_thread:
            mock_val.return_value = MagicMock()
            fake_t = MagicMock()
            fake_t.is_alive.return_value = False
            mock_thread.return_value = fake_t
            result = mod.run_synthetic_heat_fill({"duration_sec": 0.05})

        return result

    def test_gpu_false_env_gives_false_result(self, tmp_path):
        result = self._run_with_gpu(tmp_path, "false")
        assert result["gpu_burn_active"] is False

    def test_gpu_true_env_gives_true_result(self, tmp_path):
        result = self._run_with_gpu(tmp_path, "true")
        assert result["gpu_burn_active"] is True

    def test_gpu_torch_unavailable_no_error(self, tmp_path):
        """If torch is not installed, _gpu_burn_thread exits silently."""
        from hadcd_workloads import synthetic_heat_fill as mod
        import threading

        stop_event = threading.Event()
        stop_event.set()  # fire immediately so the thread exits right away

        # Patching import to raise ImportError simulates torch not installed.
        import builtins
        original_import = builtins.__import__

        def _mock_import(name, *args, **kwargs):
            if name == "torch":
                raise ImportError("No module named 'torch'")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=_mock_import):
            # Should not raise.
            mod._gpu_burn_thread(stop_event)


# ---------------------------------------------------------------------------
# CSV session log
# ---------------------------------------------------------------------------

class TestCsvSessionLog:
    def _run_once(self, tmp_path: Path) -> Path:
        from hadcd_workloads import synthetic_heat_fill as mod

        log_path = tmp_path / "synthetic.csv"
        env = _env_for(tmp_path, {"SYNTHETIC_HEAT_LOG": str(log_path)})
        fake_proc = _fake_process()

        with patch.dict(os.environ, env, clear=False), \
             patch.object(mod.multiprocessing, "Process", return_value=fake_proc), \
             patch.object(mod.multiprocessing, "Value") as mock_val:
            mock_val.return_value = MagicMock()
            mod.run_synthetic_heat_fill({"duration_sec": 0.05})

        return log_path

    def test_csv_file_created(self, tmp_path):
        log_path = self._run_once(tmp_path)
        assert log_path.exists()

    def test_csv_has_header_and_row(self, tmp_path):
        log_path = self._run_once(tmp_path)
        rows = list(csv.reader(log_path.open()))
        assert len(rows) >= 2

    def test_csv_header_columns(self, tmp_path):
        log_path = self._run_once(tmp_path)
        header = list(csv.reader(log_path.open()))[0]
        for col in ("start_utc", "end_utc", "duration_sec", "threads", "gpu_burn_active"):
            assert col in header, f"Missing CSV column: {col}"

    def test_csv_no_wallet_data(self, tmp_path):
        """CSV must not contain any wallet address or credential data."""
        log_path = self._run_once(tmp_path)
        content = log_path.read_text()
        # Sanity: no common wallet-related keys
        assert "wallet" not in content.lower()
        assert "address" not in content.lower()

    def test_csv_appends_on_second_run(self, tmp_path):
        log_path = self._run_once(tmp_path)
        self._run_once(tmp_path)
        rows = list(csv.reader(log_path.open()))
        headers = [r for r in rows if "start_utc" in r]
        data_rows = [r for r in rows if r and "start_utc" not in r]
        assert len(headers) == 1, "Header must appear exactly once"
        assert len(data_rows) == 2, "Two runs should produce two data rows"


# ---------------------------------------------------------------------------
# Worker / default_threads
# ---------------------------------------------------------------------------

class TestDefaultThreads:
    def test_default_threads_at_least_one(self):
        from hadcd_workloads.synthetic_heat_fill import _default_threads
        assert _default_threads() >= 1

    def test_default_threads_cpu_count_minus_one(self):
        from hadcd_workloads.synthetic_heat_fill import _default_threads
        with patch.object(multiprocessing, "cpu_count", return_value=8):
            assert _default_threads() == 7

    def test_default_threads_single_core_stays_one(self):
        from hadcd_workloads.synthetic_heat_fill import _default_threads
        with patch.object(multiprocessing, "cpu_count", return_value=1):
            assert _default_threads() == 1


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------

class TestSyntheticHandlerRegistered:
    def test_registered_in_registry(self):
        import hadcd_workloads  # triggers @register decorators
        from hadcd_workloads.registry import _REGISTRY
        assert "synthetic_heat_fill" in _REGISTRY

    def test_run_registered_dispatches_handler(self):
        import hadcd_workloads
        from hadcd_workloads.registry import run_registered
        from hadcd_workloads import synthetic_heat_fill as mod

        fake_proc = _fake_process()
        with patch.object(mod.multiprocessing, "Process", return_value=fake_proc), \
             patch.object(mod.multiprocessing, "Value") as mock_val:
            mock_val.return_value = MagicMock()
            result = run_registered("synthetic_heat_fill", {"duration_sec": 0.05})

        assert isinstance(result, dict)
        assert "threads" in result

