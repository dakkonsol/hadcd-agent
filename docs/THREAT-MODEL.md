# HADCD Node Agent — Threat Model

This document states, plainly, what a host takes on by running the
agent, what the agent defends against on the host's behalf, and what it
deliberately does **not** defend against. It exists because the whole
point of publishing the agent under the AGPL is that a prospective host
can decide, from the source, whether the trust it asks for is trust
they are willing to give.

If a claim here disagrees with the code, the code is the bug — please
report it (see [SECURITY.md](../SECURITY.md)).

---

## 1. What the agent is

The agent is the **host-side** half of HADCD. It runs on a machine the
host owns, in the host's building, on the host's electricity and IP
address. It:

- makes **outbound** HTTPS calls to a dispatcher the host configures;
- reports heat demand and hardware capability;
- runs the compute the dispatcher assigns — containerised sessions,
  mining fill, or a synthetic CPU/GPU burn — so the waste heat warms the
  building.

The dispatcher, scheduler, and billing service are separate, private
software operated by the network operator. This repository is the agent
and the complete set of workloads it can run
([`hadcd_workloads/`](../hadcd_workloads)), and nothing else.

---

## 2. Trust boundaries

There are three parties. The agent's security posture is different
toward each.

| Party | Trust | Why |
|-------|-------|-----|
| **The host** (operator of this machine) | Fully trusted | They own the hardware and run the agent. The agent acts on their behalf. |
| **The dispatcher** the host enrolled with | **Trusted to assign work; NOT trusted to be uncompromised** | The host chooses their dispatcher, but a compromised or malicious dispatcher must not be able to escalate from "assign a task" to "own the host". §4 is about bounding this. |
| **Task originators** (whoever ultimately submitted the work) | Untrusted / potentially adversarial | On the open network, originators are anonymous. This is the reason for [WORKLOAD_POLICY.md](../WORKLOAD_POLICY.md). |

The central assumption a host is asked to accept: **you trust your
dispatcher's operator to send you legitimate work.** The agent's job is
to make that the *only* trust you must extend — not to silently widen it
into "you trust your dispatcher with root on your machine."

---

## 3. The Docker-socket reality (read this before running container work)

The agent runs container tasks as **sibling containers** via the host's
Docker daemon (`/var/run/docker.sock`). This is inherent to the design
and cannot be papered over:

> **Access to the Docker socket is root-equivalent on the host.** Any
> party who can make the agent start an arbitrary container can, in the
> general case, obtain root on the host.

The agent narrows this considerably (§4), but it does not eliminate it,
because the daemon itself is the powerful object. Two consequences a
host must weigh:

- If you do **not** want to extend root-equivalent trust to your
  dispatcher, do **not** enrol a node that will accept container tasks
  on a machine you care about. Run container-accepting nodes on a
  dedicated box on a segmented network.
- The main agent's systemd unit is hardened (`NoNewPrivileges`,
  `ProtectSystem`, `ProtectHome`, `PrivateTmp`), but that hardening
  protects the host *from the agent process*, not the host *from a
  container the daemon launches*. Those are different boundaries.

Nodes that only run mining fill, synthetic heat, or text inference do
not expose this surface.

---

## 4. What the agent enforces locally

These are the mitigations that keep "the dispatcher assigns work" from
becoming "the dispatcher controls the host." They are enforced on the
node, in this repository — they do **not** depend on the dispatcher
having behaved.

### 4.1 Container mount allowlist
Bind-mount sources in a container task's payload are rejected unless
they are either the agent's own blob-staging temp directories or sit
under a prefix the **host** placed in `CONTAINER_MOUNT_ALLOWLIST`. The
payload can never widen this. This stops a dispatcher from mounting
`/etc`, the state directory, or the Docker socket into a container.
Enforced in [`hadcd_workloads/container.py`](../hadcd_workloads/container.py);
tested in `hadcd_workloads/tests/test_container_security_floor.py`.

### 4.2 Mandatory hardening floor
A task marked `hardened` (all client-submitted work is) always runs
with `cap_drop=ALL`, `no-new-privileges`, `privileged=False`, and an
isolated bridge network — even if those fields were stripped or loosened
in the payload. `network_mode=host` is refused for every dispatched
task. A host can force this floor onto **every** container task,
regardless of what the dispatcher claims, with
`CONTAINER_REQUIRE_HARDENED=true` — recommended for independent hosts
that do not run their own dispatcher.

### 4.3 Bounded listening surfaces
The agent opens no port to the public internet. The surfaces it does
open are each scoped locally:

| Surface | Bind | Auth | Notes |
|---------|------|------|-------|
| P2P storage server | node's Tailscale IP (loopback if Tailscale is down) | content hash acts as a capability | Never all-interfaces; a SHA-256 is only a capability while the port is tailnet-only, so the bind enforces that. |
| Rental session containers (SSH/Ollama/ComfyUI) | node's Tailscale IP | per-session `secrets.token_hex` / `uuid4`, never reused | Published on the tailnet, not Docker's default `0.0.0.0`. |
| First-boot setup wizard (`provision`) | LAN, until setup completes | **setup code shown on the node console**, required for `/save` | Values with control characters rejected (env-injection guard); request bodies capped. Service self-disables once configured. |
| WiFi provisioner | setup AP, first boot only | — | Rejects `nmcli` option-injection SSIDs; body capped. |

### 4.4 Credential and identity handling
- The node's bearer token lives in the state file (`AGENT_STATE_FILE`),
  written atomically (`tempfile` + `os.replace`), and is expected to be
  readable only by the agent user. Treat it like a password file.
- Mining wallet addresses and API keys live only in the agent
  environment. They are never placed in task payloads; the session CSV
  logs only the first 8 characters of a wallet address.
- TLS verification is on by default. The only exceptions are documented
  loopback calls to a local Sunshine instance's self-signed cert.

### 4.5 Path-traversal defence on blob/storage I/O
Server-supplied blob filenames are reduced to a bare basename and the
resolved destination is verified to stay inside the base directory. The
P2P storage route matches exactly 64 hex characters, so `..` and path
separators cannot reach the filesystem join.

### 4.6 Workload category denylist
Entire categories of work are refused by not registering a handler for
them (CSAM-risk media on community nodes, agentic outbound-network
workloads, arbitrary filesystem/socket/kernel access, all mining pools
but the two named opt-in adapters). This is enforced at the registry,
not by prompt filtering. See [WORKLOAD_POLICY.md](../WORKLOAD_POLICY.md).

---

## 5. Residual risks (what the agent does NOT defend against)

Stated honestly so a host is not surprised:

- **A compromised dispatcher issuing container tasks.** §4 bounds the
  blast radius (no arbitrary mounts, mandatory hardening, no host
  networking), but because the Docker socket is root-equivalent (§3), a
  determined attacker who fully controls the dispatcher and can reach a
  container-accepting node still has a larger surface than on a
  mining-only node. The mitigation is operational: segment the network,
  use a dedicated machine, or set `CONTAINER_REQUIRE_HARDENED`.
- **A malicious host.** Out of scope by definition — the host owns the
  machine. HADCD's protection against a bad *host* (e.g. lying about
  work done) lives on the dispatcher side, not here.
- **Physical access to the node.** The setup-code gate assumes the
  console/screen is seen only by the legitimate installer.
- **Supply-chain compromise of dependencies.** Dependencies are pinned;
  CI runs `pip-audit`. This narrows but does not eliminate the risk.
- **Vast.ai / mining-pool / thermostat account security.** Those
  credentials grant control of the host's own third-party accounts; the
  agent stores them locally but their account security is the host's.

---

## 6. Hardening checklist for a cautious host

1. Run container-accepting nodes on a dedicated machine, on a network
   segment separable from personal/family traffic.
2. Set `CONTAINER_REQUIRE_HARDENED=true` unless you operate the
   dispatcher yourself.
3. Leave `CONTAINER_MOUNT_ALLOWLIST` empty unless you specifically need
   a host directory mounted, and then scope it as narrowly as possible.
4. Keep the agent on a tailnet; do not port-forward any of its ports.
5. Keep the state file and `agent.env` readable only by the agent user
   (the install scripts do this; replicate it if you deviate).
6. Prefer a node that only runs mining/heat-fill if you are not
   comfortable extending container-level trust to your dispatcher.
