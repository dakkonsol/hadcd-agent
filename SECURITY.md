# Security Policy

## Reporting a vulnerability

If you find a security issue in the HADCD node agent, please report it
privately first — do not open a public issue for anything exploitable.

- **Email:** brett.david.stone@gmail.com
- **Subject:** start it with `[HADCD-SECURITY]` so it is triaged quickly.
- Please include: the affected file(s) and version/commit, a description
  of the impact, and a minimal reproduction or proof of concept if you
  have one.

You will get an acknowledgement within a few days. Once a fix is
available, coordinated disclosure is welcome and contributors who report
responsibly will be credited unless they prefer otherwise.

## Scope

This repository is the **host-side agent** and the workloads it runs.
In scope:

- the agent (`agent/`) and workload package (`hadcd_workloads/`);
- the setup/provisioning surfaces, the P2P storage server, and the
  rental-session container handling;
- the deploy assets under `agent/deploy/` and `scripts/`.

Out of scope here (report separately if you have a contact for it): the
private dispatcher, scheduler, and billing services, which are not part
of this repository.

## What to expect

The trust boundaries and the mitigations the agent enforces locally are
documented in [docs/THREAT-MODEL.md](docs/THREAT-MODEL.md). A report that
demonstrates a way to cross one of those boundaries — for example,
escaping the container mount allowlist, defeating the hardening floor,
reaching a listening surface that should be tailnet-only, or writing
outside a blob's base directory — is exactly what we want to hear about.

Known and documented residual risks (see §5 of the threat model),
such as the root-equivalence of Docker socket access, are already
understood; a report is still welcome if you have a concrete mitigation
that does not break the sibling-container design.

## Supported versions

The agent is pre-1.0. Security fixes land on `main`; there is no
back-port branch yet. Run the latest `main`.
