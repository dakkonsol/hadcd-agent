# Changelog

All notable changes to the HADCD node agent are recorded here. This
repository is a curated public mirror of the agent extracted from a
larger private monorepo; the "Phase N" markers throughout the source
refer to the internal development history that produced each subsystem.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and the project follows [Semantic Versioning](https://semver.org/) once
it reaches 1.0. It is pre-1.0 today: expect breaking changes on `main`.

## [Unreleased]

### Security
- **Container tasks now enforce a node-side security floor.** Bind-mount
  sources must be locally allowlisted (`CONTAINER_MOUNT_ALLOWLIST`; the
  agent's own blob-staging dirs are always allowed); `network_mode=host`
  is refused; and `hardened` tasks always run with `cap_drop=ALL`,
  `no-new-privileges`, unprivileged, on an isolated bridge — regardless
  of what the task payload contains. `CONTAINER_REQUIRE_HARDENED` applies
  the floor to every container task on a node.
- **Listening surfaces are no longer bound to all interfaces.** The P2P
  storage server and rental-session container ports bind the node's
  Tailscale IP (loopback when Tailscale is down). The first-boot setup
  wizard gates configuration writes behind a console setup code, rejects
  values containing control characters (env-injection), and caps request
  bodies; the WiFi provisioner rejects `nmcli` option-injection SSIDs.
- Added [docs/THREAT-MODEL.md](docs/THREAT-MODEL.md) and
  [SECURITY.md](SECURITY.md) making the dispatcher-trust boundary and the
  Docker-socket root-equivalence explicit, and corrected the README's
  networking claims.

### Fixed
- **Startup crash:** the agent read a settings field that does not exist
  (`node_token` lives in the persisted identity state, not settings),
  aborting before any loop started. The rental-session handler is now
  constructed after enrollment from the real identity, and a regression
  test asserts every settings attribute the agent reads actually exists.
- Task temporary directories are now removed in a real `finally` block,
  so a cancelled or failed task cannot leave downloaded inputs/outputs on
  disk.
- Removed dead reads of the GPU-pressure utilisation thresholds in the
  mining fill handler (detection is presence-based; the percentage
  thresholds were never applied) and documented that behaviour.

### Added
- Continuous integration ([.github/workflows/ci.yml](.github/workflows/ci.yml)):
  the agent and workloads test suites on Python 3.11 and 3.12, plus
  `ruff` and an advisory `pip-audit`.
- [pyproject.toml](pyproject.toml) declaring the supported Python range
  and tooling config; [.dockerignore](.dockerignore).
- Operator documentation and install scripts: `scripts/install-agent.sh`,
  `scripts/vast-register.sh`, `scripts/install-ha.sh`, and `docs/`
  (deployment, Sunshine, Vast.ai, mining setup), resolving the previously
  dangling references in the systemd deploy guide.

### Changed
- The `Dockerfile` builds from the repository root so the sibling
  `hadcd_workloads` package is included; build with
  `docker build -f agent/Dockerfile -t hadcd-agent .`.
- `config.env.example` now documents every implemented setting (storage,
  blob staging, Kasa power meter, synthetic heat, node role, ComfyUI,
  and the new container-security-floor knobs), and no longer documents
  the removed `GPU_MODEL` field. A test keeps the example and the
  settings class from drifting apart.

## [0.1.0] — 2026-07-19
- Initial public release of the HADCD node agent under AGPL-3.0.
