# HADCD Node Agent

The open-source node agent for **HADCD** — a heat-aware compute dispatch
network. HADCD turns the waste heat from GPU compute into useful building
heat: when a room calls for warmth, the network routes AI inference, media
generation, or mining work to the machine in that room, and the "waste"
heat does the heating.

This repository contains the software that runs **on the host's machine**.
It is published under the AGPL so that anyone considering joining the
network as a host can audit exactly what will run on their hardware.

## What the agent does — and doesn't do

The agent:

- polls the HADCD dispatcher over an outbound HTTPS connection and reports
  the machine's heat demand, capacity, and health;
- runs the work it is assigned (containerised compute sessions, media
  generation via ComfyUI, mining fill during idle heat demand);
- reads heat demand from a thermostat/BMS integration (Ecobee, Home
  Assistant, or a plain HTTP source) and an optional smart-plug power meter;
- manages the machine's Vast.ai listing during no-heat windows, if the host
  opts in — **the host's own Vast.ai account and earnings, not the network's**;
- sends session logs and payout records to CSV files the host can read.

The agent does **not**:

- listen on the public internet. All dispatcher traffic is outbound. The
  agent does open three local listening surfaces, each deliberately
  scoped: the P2P storage server binds the host's Tailscale IP (loopback
  when Tailscale is down — never all interfaces), rental-session
  containers publish ports for tailnet clients, and the first-boot setup
  wizards listen on the LAN only until setup completes, with
  configuration writes gated by a setup code shown on the node's console;
- hold or move funds — payouts are settled off-device (Stripe), and any
  mining wallets configured are the host's own;
- update itself silently or run anything not visible in this source tree.

Every credential the agent uses is supplied by the host in `config.env`
(see [`agent/config.env.example`](agent/config.env.example) for the fully
annotated reference). Nothing is phoned home beyond the dispatcher API
calls implemented in this repository.

The [`hadcd_workloads/`](hadcd_workloads/) package contains the complete
implementations of everything the agent can be asked to run — the
containerised session runner, the CPU/GPU mining fill, and the synthetic
heat fill — so "what exactly runs on my machine" has a source-level answer.

## Before you trust this on your hardware

Read **[docs/THREAT-MODEL.md](docs/THREAT-MODEL.md)**. It states exactly
what a host is trusting, what the agent enforces locally to bound a
compromised dispatcher, and what it deliberately does not defend against
(notably: Docker-socket access is root-equivalent, so container-accepting
nodes belong on a dedicated, segmented machine). The workload policy —
which categories of work are accepted or refused, and why — is in
[WORKLOAD_POLICY.md](WORKLOAD_POLICY.md).

Found a security issue? See [SECURITY.md](SECURITY.md).

## Installing

- **Linux (systemd):** [agent/deploy/systemd/README.md](agent/deploy/systemd/README.md),
  or the one-shot [`scripts/install-agent.sh`](scripts/install-agent.sh).
- **Windows (NSSM):** [agent/deploy/windows/README.md](agent/deploy/windows/README.md).
- **Full operator walkthrough:** [docs/deployment.md](docs/deployment.md).

## Running the tests

```
cd agent
python -m pytest          # agent suite
cd ../hadcd_workloads
python -m pytest          # workload suite
```

CI runs both suites on Python 3.11 and 3.12, plus `ruff` and
`pip-audit`, on every push.

## Relationship to the HADCD network

The HADCD dispatcher, scheduler, and billing service are separate,
proprietary software operated by the network operator. This licence covers
the agent only. Joining the network as a host requires an enrollment token
from the operator.

## License

Copyright (C) 2026 Brett Stone / Stonecraft Web Design Inc.

This program is free software: you can redistribute it and/or modify it
under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or (at your
option) any later version. See [LICENSE](LICENSE) for the full text.
