"""Drift guard — config.env.example must track AgentSettings exactly.

The first public review found the example documented settings that the
code did not implement (GPU_MODEL) and omitted whole sections it did
(storage, Kasa, synthetic heat, node role). Both directions of drift
cost reviewer trust, so both fail here:

  * every KEY= line in the example must be a real AgentSettings field;
  * every AgentSettings field must appear as a KEY= line in the example.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent.config import AgentSettings

_EXAMPLE = Path(__file__).resolve().parent.parent / "config.env.example"

# env names pydantic-settings maps onto each field (bare upper-cased).
_FIELD_ENV_NAMES = {name.upper() for name in AgentSettings.model_fields}

_KEY_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)

# The workload package runs in spawned worker processes and reads some
# settings straight from the environment rather than through
# AgentSettings (e.g. NICEHASH_ALGO, HADCD_MOUNT_ALLOWLIST). Those count
# as implemented too.
_WORKLOADS_DIR = _EXAMPLE.parent.parent / "hadcd_workloads"
_ENV_READ = re.compile(
    r'(?:_env\(\s*|os\.environ\.get\(\s*|os\.getenv\(\s*)"([A-Z][A-Z0-9_]+)"'
)


def _workload_env_names() -> set[str]:
    names: set[str] = set()
    for path in _WORKLOADS_DIR.glob("*.py"):
        names |= set(_ENV_READ.findall(path.read_text(encoding="utf-8")))
    return names


def _example_keys() -> set[str]:
    return set(_KEY_LINE.findall(_EXAMPLE.read_text(encoding="utf-8")))


def test_every_example_key_is_a_real_setting():
    implemented = _FIELD_ENV_NAMES | _workload_env_names()
    unknown = _example_keys() - implemented
    assert not unknown, (
        f"config.env.example documents settings that neither AgentSettings "
        f"nor the workload package implements: {sorted(unknown)}"
    )


def test_every_setting_appears_in_the_example():
    missing = _FIELD_ENV_NAMES - _example_keys()
    assert not missing, (
        f"AgentSettings fields missing from config.env.example: "
        f"{sorted(missing)}"
    )
