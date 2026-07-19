"""Dependency health check for the HADCD node agent.

Runs at agent startup and on each heartbeat cycle.  Returns a list of
dependency records that the agent includes in its heartbeat payload so
the operator dashboard can show exactly what is installed, what is
missing, and where to get it.

Each record:
    key          str   — stable identifier used by the frontend
    name         str   — human-readable name
    installed    bool  — True if the dep was found and usable
    version      str?  — version string when detectable
    optional     bool  — False = required for core function
    download_url str?  — link to the project's download page
    note         str?  — one-line context ("Required for GPU container tasks")
"""

from __future__ import annotations

import shutil
import subprocess
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger("hadcd.agent.dep_check")


def _run(cmd: list[str], timeout: float = 5.0) -> str | None:
    """Run a command, return stdout stripped, or None on any failure."""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.stdout.strip() or None
    except Exception:
        return None


def _which(name: str) -> bool:
    return shutil.which(name) is not None


def _dep(
    key: str,
    name: str,
    installed: bool,
    version: str | None = None,
    optional: bool = False,
    download_url: str | None = None,
    note: str | None = None,
) -> dict:
    return {
        "key": key,
        "name": name,
        "installed": installed,
        "version": version,
        "optional": optional,
        "download_url": download_url,
        "note": note,
    }


# ── Individual checks ─────────────────────────────────────────────────────────


def _check_python() -> dict:
    import sys
    ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    ok = sys.version_info >= (3, 11)
    return _dep(
        "python",
        "Python 3.11+",
        ok,
        version=ver,
        optional=False,
        download_url="https://www.python.org/downloads/",
        note="Required — agent runtime",
    )


def _check_docker() -> dict:
    out = _run(["docker", "--version"])
    installed = out is not None
    version = out.replace("Docker version ", "").split(",")[0] if out else None
    return _dep(
        "docker",
        "Docker Engine",
        installed,
        version=version,
        optional=False,
        download_url="https://docs.docker.com/engine/install/",
        note="Required for container task workloads",
    )


def _check_nvidia_smi() -> dict:
    out = _run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"])
    installed = out is not None
    version = out.split("\n")[0].strip() if out else None
    return _dep(
        "nvidia_driver",
        "NVIDIA GPU Driver",
        installed,
        version=version,
        optional=True,
        download_url="https://www.nvidia.com/Download/index.aspx",
        note="Required for GPU container tasks and mining",
    )


def _check_nvidia_container_toolkit() -> dict:
    """Check for nvidia-container-toolkit by probing the Docker runtime list."""
    # Fastest reliable check: see if the nvidia runtime is registered with Docker.
    out = _run(["docker", "info", "--format", "{{json .Runtimes}}"])
    installed = out is not None and "nvidia" in out.lower()

    # Fallback: check if the package binary exists on the path.
    if not installed:
        installed = _which("nvidia-container-runtime") or _which("nvidia-ctk")

    return _dep(
        "nvidia_container_toolkit",
        "NVIDIA Container Toolkit",
        bool(installed),
        optional=True,
        download_url="https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html",
        note="Required to pass GPU into Docker containers (--gpus all)",
    )


def _check_tailscale() -> dict:
    out = _run(["tailscale", "version"])
    installed = out is not None
    version = out.split("\n")[0].strip() if out else None

    # Check if it's actually connected (not just installed)
    status_out = _run(["tailscale", "status", "--json"])
    connected = False
    if status_out:
        try:
            import json
            st = json.loads(status_out)
            connected = st.get("BackendState") == "Running"
        except Exception:
            pass

    note = "Required for reliable dispatcher connectivity"
    if installed and not connected:
        note = "Installed but not connected — run: sudo tailscale up"

    return _dep(
        "tailscale",
        "Tailscale",
        installed,
        version=version,
        optional=False,
        download_url="https://tailscale.com/download/linux",
        note=note,
    )


def _check_sunshine() -> dict:
    """Check if Sunshine (remote desktop server) is installed and running."""
    # Check systemd service first
    svc_out = _run(["systemctl", "is-active", "sunshine"])
    if svc_out == "active":
        return _dep(
            "sunshine",
            "Sunshine (remote desktop)",
            True,
            version="active",
            optional=True,
            download_url="https://github.com/LizardByte/Sunshine/releases",
            note="Enables Mode 2 remote desktop sessions",
        )

    # Check if binary exists anywhere
    installed = _which("sunshine") or Path("/usr/bin/sunshine").exists() or \
        Path("/usr/local/bin/sunshine").exists()

    note = "Enables Mode 2 remote desktop sessions"
    if installed:
        note = "Installed but service not running — run: sudo systemctl start sunshine"

    return _dep(
        "sunshine",
        "Sunshine (remote desktop)",
        bool(installed),
        optional=True,
        download_url="https://github.com/LizardByte/Sunshine/releases",
        note=note,
    )


def _check_trex(trex_path: str | None) -> dict:
    """Check for T-Rex Miner (NiceHash GPU mining)."""
    if not trex_path:
        return _dep(
            "trex_miner",
            "T-Rex Miner (NiceHash GPU)",
            False,
            optional=True,
            download_url="https://github.com/trexminer/T-Rex/releases",
            note="Not configured — set NICEHASH_TREX_PATH in agent.env to enable GPU mining",
        )
    exists = Path(trex_path).is_file()
    out = _run([trex_path, "--version"]) if exists else None
    version = out.split("\n")[0] if out else None
    return _dep(
        "trex_miner",
        "T-Rex Miner (NiceHash GPU)",
        exists,
        version=version,
        optional=True,
        download_url="https://github.com/trexminer/T-Rex/releases",
        note="GPU fill-tier mining via NiceHash" if exists else f"Binary not found at {trex_path}",
    )


def _check_xmrig(xmrig_path: str | None) -> dict:
    """Check for XMRig (P2Pool CPU mining)."""
    if not xmrig_path:
        return _dep(
            "xmrig",
            "XMRig (P2Pool CPU mining)",
            False,
            optional=True,
            download_url="https://xmrig.com/download",
            note="Not configured — set XMRIG_PATH in agent.env to enable CPU mining",
        )
    exists = Path(xmrig_path).is_file()
    out = _run([xmrig_path, "--version"]) if exists else None
    version = out.split("\n")[0] if out else None
    return _dep(
        "xmrig",
        "XMRig (P2Pool CPU mining)",
        exists,
        version=version,
        optional=True,
        download_url="https://xmrig.com/download",
        note="CPU fill-tier mining via P2Pool" if exists else f"Binary not found at {xmrig_path}",
    )


def _check_vastai_cli(vastai_cmd: str | None) -> dict:
    """Check for the Vast.AI CLI."""
    cmd = vastai_cmd or "vastai"
    out = _run([cmd, "--version"])
    installed = out is not None
    return _dep(
        "vastai_cli",
        "Vast.AI CLI",
        installed,
        version=out if installed else None,
        optional=True,
        download_url="https://vast.ai/docs/cli/commands",
        note="Required for weather-driven GPU rental listing on Vast.AI",
    )


# ── Public API ────────────────────────────────────────────────────────────────


def check_all(
    trex_path: str | None = None,
    xmrig_path: str | None = None,
    vastai_cmd: str | None = None,
    check_vastai: bool = False,
    check_mining: bool = False,
) -> list[dict]:
    """Run all dependency checks and return a list of dep records.

    Args:
        trex_path:    NICEHASH_TREX_PATH from agent config (or None if unset)
        xmrig_path:   XMRIG_PATH from agent config (or None if unset)
        vastai_cmd:   VASTAI_CMD from agent config (default "vastai")
        check_vastai: Include Vast.AI CLI check (True when VASTAI_API_KEY configured)
        check_mining: Include miner checks (True when wallet addresses configured)
    """
    deps: list[dict] = []

    # Core — always checked
    deps.append(_check_python())
    deps.append(_check_docker())
    deps.append(_check_tailscale())
    deps.append(_check_nvidia_smi())
    deps.append(_check_nvidia_container_toolkit())
    deps.append(_check_sunshine())

    # Mining — only if configured
    if check_mining or trex_path:
        deps.append(_check_trex(trex_path))
    if check_mining or xmrig_path:
        deps.append(_check_xmrig(xmrig_path))

    # Vast.AI — only if configured
    if check_vastai:
        deps.append(_check_vastai_cli(vastai_cmd))

    logger.info(
        "dep_check: %d/%d deps installed",
        sum(1 for d in deps if d["installed"]),
        len(deps),
    )
    return deps
