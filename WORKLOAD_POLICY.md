# HADCD Workload Policy

This document defines which categories of compute work HADCD will and
will not accept onto its nodes, and *why*. It is the policy half of
the workload story; the technical enforcement half lives in
[`hadcd_workloads/`](hadcd_workloads/) (the handler registry) and in
the task-source adapters (forthcoming).

The policy exists because HADCD nodes are **physical hardware in
real homes and buildings, operated by real people**. Whatever runs
on a node runs through its operator's electricity, IP address, and
legal jurisdiction. "The network said it was fine" is not a defence
the operator inherits. The decisions here are about what risk the
project is willing to expose its operators to — not about what the
hardware is technically capable of.

---

## 1. Threat model

HADCD's trust model for *nodes* is "trusted, contracted
infrastructure". The trust model for *task originators* is the
opposite: once we accept
tasks from outside the network, originators are anonymous and may
be adversarial.

The risks the policy is designed to bound, in rough order of
severity:

1. **CSAM and other strict-liability content.** Generating, refining,
   or even transiently processing illegal imagery would expose the
   operator to criminal investigation regardless of intent. This is
   the dominant concern and the reason image-generation is refused
   wholesale.
2. **Non-consensual intimate imagery, deepfakes of real people.**
   Emerging civil and criminal exposure in multiple US states.
3. **Copyrighted-output generation at scale.** DMCA / civil exposure
   for the operator's IP and ISP relationship.
4. **Weaponised content / malware training or generation.** CFAA and
   related exposure if a node is used as a step in attacking a third
   party.
5. **Reputational and continuity risk.** Even acquitted operators
   lose hardware to seizure, time to legal process, and ISP service
   to AUP enforcement. "Won the case" is not "no harm done."

The policy is more conservative than what any individual law
strictly requires. The principle is **operator-protective by
default** — risk that lands on an operator must be one the project
has explicitly accepted, not one that arrived through omission.

---

## 2. Accepted workload categories

A workload category is **accepted** if it has been weighed against
the threat model above and the project has chosen to expose
operators to its risk. Acceptance is per-category, not per-task.

| Category | Status | Notes |
|----------|--------|-------|
| In-network synthetic tasks (`sleep`, `cpu_burn`, `fib`, `matrix_multiply`) | Accepted | The built-in handlers in [`hadcd_workloads/handlers.py`](hadcd_workloads/handlers.py). No external content. |
| In-network compute tasks submitted via the operator's own admin token | Accepted | Anything an authenticated operator submits to `/api/tasks` from inside their own deployment. The originator and operator are the same person. |
| Text-only LLM inference (chat completion, summarisation, classification, embeddings) | Accepted, with conditions | Conditions in §4. Bounded output size, no media generation, no agentic browsing. |
| Scientific compute (numerical simulation, protein folding, signal processing) | Accepted | No content-generation surface; risk profile is similar to volunteer projects like Folding@home. |
| Model fine-tuning of *open-weight, published* language models on operator-curated data | Accepted | Operator-curated data only. Anonymous-submitter fine-tuning data is refused — see §3. |
| **Media generation (image / video / 3D / audio via ComfyUI)** — **operator-owned nodes ONLY** | Accepted, with a hard node restriction | Conditions in §4a. Dispatched **only** to a node where `owner_kind = operator` **and** `media_capable = true`; **never** to an independent/community node. The operator is then the node owner, the originator, and the responsible party — which is what removes the strict-liability exposure that keeps media off community nodes (§3). Enforced in code by the session assigner's media gate (`_node_serves_session_type`) on the dispatcher, not by prompt filtering. |

---

## 3. Refused workload categories

A workload category is **refused** if its worst-case realisation
exposes operators to risk the project will not accept on their
behalf. Refused categories are blocked at the workload registry: no
handler for them is registered, and `UnknownTaskType` rejects any
payload that names one.

| Category | Reason |
|----------|--------|
| **Image / video / 3D / audio generation on independent (community) nodes** | CSAM / NCII / deepfake strict-liability exposure landing on a host who did not originate the prompt. Refused on `owner_kind = independent` nodes wholesale, regardless of declared subject matter, prompt filtering, or platform-side controls. Non-negotiable. **On operator-owned nodes** the same generation is *accepted* (§2, §4a) because the operator is the originator and responsible party — the anonymous-submitter risk is absent. |
| **Voice cloning / speech synthesis of identifiable individuals** | Fraud and deepfake exposure. Generic TTS is deferred pending a category split. |
| **Anonymous-submitter fine-tuning data** | The operator cannot tell whether the training corpus contains illegal or infringing material, and would inherit liability for the resulting weights. |
| **Agentic workloads with outbound network access** (web-browsing agents, autonomous shopping agents, tool-using agents that make their own HTTP requests) | The node becomes a step in the agent's actions against third parties — CFAA, ToS-violation, and abuse-reporting exposure. |
| **Cryptocurrency mining** *(all coins, miners, and pools refused **except** the two §5 adapters named here)* | The two named exceptions: **NiceHash GPU mining** (via T-Rex) and **Monero (XMR) CPU mining via P2Pool** (via XMRig). Both are opt-in per node by the operator; leaving the binary paths unconfigured disables them. Every other coin, miner, or pool is refused and adding one requires a §3/§5 amendment. |
| **Any workload requesting raw filesystem, raw socket, or kernel-level access** | Sandbox escape primitive; refused at the handler-registry level. |

This list is intentionally a denylist of **categories**, not a
keyword filter on prompts. Prompt filtering is brittle and provides
weak legal cover; refusing entire workload categories is enforceable
and defensible.

---

## 4. Conditions on accepted text inference

Text inference is accepted because its worst-case output is
qualitatively bounded (text, not media). The conditions that keep it
within that bound:

- **Output size cap.** A handler must declare a `max_output_bytes`
  and truncate beyond it. The default cap is 64 KiB. This rules out
  using a "text" handler to smuggle base64-encoded media.
- **No media MIME types in input or output.** Handlers reject
  payloads whose declared content type is anything other than text.
- **No tool-use / function-calling that performs side effects.**
  Read-only retrieval against an operator-controlled corpus is fine;
  anything that emits an outbound request from the node is refused
  per §3.
- **Logging is metadata-only.** Node-side logs record task IDs,
  durations, sizes, and outcomes — never prompts or outputs.
  Operators are not custodians of the content they processed.

A future PR formalises these as a `TextInferenceHandler` base class
that enforces them at the registry boundary. Until then they are
conventions for handler authors.

---

## 4a. Conditions on accepted media generation

Media generation (image/video/3D/audio via a ComfyUI session) is accepted
**only** under the operator-node restriction. The conditions that keep it
within bounds:

- **Operator-owned nodes only.** The session assigner dispatches a `media`
  session solely to a node with `owner_kind = operator` **and**
  `media_capable = true`. Independent/community nodes are never candidates.
  This is enforced in `dispatch/session_assigner.py`
  (`_node_serves_session_type`) and covered by
  `tests/test_media_workload.py` — it is a code invariant, not a convention.
- **Per-node opt-in.** `media_capable` is false until the node's operator
  configures the ComfyUI media opt-in on the agent (same shape as the mining
  opt-in): an unconfigured node serves no media.
- **Hardened container.** The ComfyUI container runs with the same profile as
  other session containers (drop all caps, no-new-privileges, isolated bridge
  network, reachable only over Tailscale/loopback).
- **The operator is the originator.** Media is generated for the operator's
  own client (their Minerva). There is no anonymous third-party submitter, so
  the strict-liability rationale that refuses media on community nodes (§3)
  does not apply here.

---

## 5. Task-source-specific policy

HADCD's dispatcher accepts tasks from multiple sources, in priority
order (lowest `source_tier` number wins). Each source has its own
policy posture.

> **Note — Salad:** An earlier design explored Salad Networks as a
> Tier 2 external compute source. After reviewing their integration
> model, Salad was rejected: they sell *GPU hours to buyers* on their
> platform; HADCD nodes would need to become Salad Nodes under Salad's
> ToS, which doesn't align with the project's operator-control and
> no-image-generation requirements. Salad is not in the codebase.

### Tier 1 (source_tier = 1) — In-network tasks
Tasks submitted by an authenticated operator to `/api/tasks` on a
HADCD deployment they control. **The operator is the originator.**
The accepted/refused categories in §2-3 still apply (an operator
cannot opt their own deployment into image generation), but there is
no anonymous-submitter risk. This is the default and the highest
priority for the dispatcher.

Sources: `in_network`

### Tier 2 (source_tier = 2) — Mining fill (opt-in per node)
A narrow exception to the no-mining rule in §3. The fill injector
fires when there are nodes demanding heat and the Tier 1 queue is
empty. Two fill adapters run at the same tier, targeting different
hardware:

#### 2a — GPU mining fill: NiceHash excavator (`gpu_mining_fill`)
- **Network: NiceHash** via the excavator binary. Auto-selects the
  most profitable algorithm on the operator's behalf. Pays in BTC to
  the operator's NiceHash wallet.
- **Binary: T-Rex Miner** (Linux-native NVIDIA miner, NiceHash stratum
  compatible). Runs as a subprocess; HADCD terminates it cleanly when
  the task ends. Payouts land as BTC in the NiceHash dashboard.
- **Opt-in mechanism:** the agent only runs this handler if
  `NICEHASH_TREX_PATH` is set in the agent environment. An
  unconfigured path is a clean skip, not an error.
- **GPU pressure detection:** the handler polls `nvidia-smi` every
  `MINING_POLL_INTERVAL_SEC` seconds. If it detects a non-miner
  CUDA compute process (e.g. a game, a local AI app), it suspends
  the miner (`SIGSTOP` / psutil) until pressure clears. This is a
  secondary safeguard; the primary gate is the fill-tier gating
  predicate (`should_pause_fill_tiers`) which prevents fill tasks
  from dispatching to a node with an active Sunshine session.

Sources: `gpu_mining_fill`

#### 2b — CPU mining fill: XMRig → P2Pool (`p2pool_fill`)
- **Coin: Monero (XMR).** CPU-friendly; does not compete with GPU
  inference or GPU mining workloads.
- **Pool: P2Pool** (decentralised — no pool operator, no KYC, no
  PPLNS rotation penalty for start/stop cycling). Pays per share
  block directly to the operator's wallet.
- **Miner: XMRig.** Open source, dominant Monero miner.
- **Opt-in mechanism:** the agent only runs this handler if
  `XMRIG_PATH` is set. An unconfigured path is a clean skip.
- **Thread headroom:** defaults to `cpu_count - 1` so the OS and
  agent always have at least one core.

Other Monero pools (Hashvault, SupportXMR, MoneroOcean, etc.)
are **not** accepted via this adapter. Changing pools requires a
§3/§5 amendment, not a config change.

Sources: `p2pool_fill`

**Session CSV log (both adapters):** Each completed mining session
appends one row to a local CSV file. Fields include start time, end
time, duration, worker name, and the first 8 characters of the
wallet address (never the full address — wallet addresses must not
appear in the task ledger or full logs). Actual payout amounts must
be reconciled from the NiceHash dashboard or P2Pool observer. This
CSV is the bookkeeping basis for CRA business-income reporting.

**Duration / task chunking:** Fill tasks are long-running by design
(default 30 minutes). When a Tier 1 task arrives the dispatcher will
preempt the node at the HADCD level — the current fill task is
recalled or times out, the higher-tier task runs, and the fill
injector re-queues after the node returns to idle. The miner
process exits cleanly via `terminate()` followed by `kill()` if it
doesn't exit within 10 seconds.

**Legal posture (Canadian operators):** Business-income tax treatment
on receipt at CAD FMV; capital-gain/loss on disposition. Possible
provincial / municipal energy and mining rules depending on location.
ISP AUP exposure. The operator is responsible for determining
applicability in their jurisdiction; the project's responsibility is
ensuring the tooling makes session-level records available.

### Tier 3 (source_tier = 3) — Synthetic heat-fill (failsafe)
The unconditional last-resort tier. When Tier 1 and Tier 2 produced
no work and the node is still demanding heat, the dispatcher emits a
short synthetic `cpu_burn` or `matrix_multiply` task. No external
surface, no payout, no opt-in needed — these are already-accepted
in-network workloads from
[`hadcd_workloads/handlers.py`](hadcd_workloads/handlers.py).

Tier 3 exists so that **heat dispatch is never blocked by an
external failure.** If the mining binaries are not configured or the
operator has not opted in, the system still delivers heat when the
building demands it. Pure resistive heating with no revenue beats
a cold pilot building.

Sources: `synthetic_heat_fill`

### Tier 99 (source_tier = 99) — Vast.AI provider windows (Phase 11c/11d ✅)
Weather-driven Vast.AI provider listing. When the hourly Open-Meteo
forecast puts outdoor temperature in the **moderate band** (above the
cold-mining threshold and below `setpoint_c − 2 °C`), the weather
poller writes `VastWindow` rows and the agent's `VastProvider` state
machine lists the GPU on Vast.AI (selling GPU time, not buying it).

The tier-99 label is the `source_tier` tag for self-generated tasks
in the queue. In practice, Vast.AI has **higher effective priority
than mining**: the agent checks `VastProviderState` before each
mining chunk and skips GPU mining entirely while `LISTED` or
`LISTING` — the GPU physically belongs to the renter.

Unlisting is always graceful: the marketplace offer is removed but
active rentals run to completion (`UNLISTING` → `UNLISTED` only when
`active_rental_count() == 0`).

Operator override (`"list"` / `"unlist"`) delivered via heartbeat
response takes absolute priority over the weather schedule.

Implementation: `agent/vast_provider.py` (state machine + CLI
wrapper), `backend/app/weather_poller.py` (cold/hot window
computation), `backend/app/api/nodes.py` (`GET
/{id}/vast_schedule`).

Sources: `gpu_rental_reserved`

---

## 6. Enforcement: where the policy lives in code

| Enforcement point | What it enforces |
|-------------------|-------------------|
| [`hadcd_workloads/registry.py`](hadcd_workloads/registry.py) | The set of `task_type` strings the system will execute. Refused categories are simply not registered. `UnknownTaskType` is the default-deny. |
| `Task.offload_suitable` (in the dispatcher backend, not in this repo) | Hard filter on the dispatcher path. Sensitive tasks never leave the central server. |
| `Task.sensitive` | Same shape; in-network data-handling rule. |
| GPU mining fill handler (`hadcd_workloads/gpu_mining_fill.py`) | Per-node opt-in via `NICEHASH_TREX_PATH` env var (T-Rex Miner, Linux NVIDIA). Skips if not configured. Suspends on GPU pressure. Writes session CSV. |
| P2Pool fill handler (`hadcd_workloads/p2pool_fill.py`) | Per-node opt-in via `XMRIG_PATH` env var. Skips if not configured. Writes session CSV. Only P2Pool is accepted; other pools rejected by hard-coded default. |
| Fill injector (`backend/app/dispatch/fill_injector.py`) | Idempotent per-source injection: only queues a fill task if no active task of that source exists. Respects `FILL_INJECTOR_ENABLED`. |
| Synthetic heat-fill source (Phase 9b) | Failsafe last resort. Emits in-network `cpu_burn` / `matrix_multiply` chunks when Tiers 1–2 are empty and heat demand persists. No external surface, no opt-in. |
| Node operator UI | Per-node participation toggle (already exists, `nodes.participating`); per-tier opt-ins (forthcoming). |

The principle is that **every refused category is unrepresentable**,
not merely unwanted. A future contributor who tries to register an
image-generation handler should have to delete a line of this
document and explain why, in a PR — not just merge a handler.

---

## 7. Operator responsibilities

If you operate a HADCD node — including your own — you are the
physical custodian of whatever runs on it. The policy in this
document reduces the risk you take on; it does not eliminate it.
Specifically:

- **You are responsible for the legality of operating compute-for-pay
  hardware in your jurisdiction.** Some municipalities treat
  residential mining as a regulated activity; some homeowner-insurance
  policies have "business use" exclusions that void coverage.
- **You are responsible for your network.** If your node accepts
  external compute, run it on a network segment that can be
  forensically distinguished from your personal/family activity. A
  static IP and a separate VLAN are cheap insurance.
- **You should consider an LLC** if you accept external compute
  beyond hobby scale. Thin liability shielding is still real
  shielding.
- **If you receive a legal notice** (DMCA, subpoena, abuse report)
  related to a node, stop accepting external tasks on that node
  immediately and consult a lawyer. The project will assist with
  technical facts but cannot represent you.

These are operator responsibilities, not project promises. The
project's promise is: the categories refused in §3 will stay refused,
and the enforcement in §6 will stay in place.

---

## 8. Review and changes

This policy is reviewed when any of the following happens:

- A new external task source is proposed.
- An existing accepted category produces an incident (legal notice,
  abuse report, operator complaint).
- US federal or any operator's state/local law materially changes
  the strict-liability landscape for generative AI workloads.

Changes to **§3 (refused categories)** require explicit project-owner
approval; they are not normal-PR-mergeable. Changes to §2 (accepted
categories) require the same. Changes to §4-7 may go through normal
review.

The current owner of this policy is the HADCD maintainer (Brett
Stone).
