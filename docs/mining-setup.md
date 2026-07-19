# Mining Setup — HADCD Node Guide

HADCD uses two mining fill-tiers to convert idle heat demand into
revenue when no dispatched workloads are available:

| Tier | Handler | What it mines | Pool |
|---|---|---|---|
| `gpu_mining_fill` | T-Rex Miner | Most profitable GPU algorithm (auto via NiceHash) | NiceHash stratum |
| `p2pool_fill` | XMRig | Monero (XMR) CPU mining | P2Pool (decentralised) |

Both run only when the BMS reports active heat demand AND no
higher-priority dispatched work is available.  The agent starts and
stops them automatically — you never touch them after initial setup.

---

## Part 1 — NiceHash (GPU mining)

### What you need

| Item | Where to get it | Goes in |
|---|---|---|
| NiceHash account | nicehash.com | — |
| BTC payout wallet address | NiceHash dashboard → Wallet | `NICEHASH_WALLET` in agent.env |
| T-Rex Miner binary (Linux) | github.com/trexminer/T-Rex/releases | `/opt/trex/t-rex` on each node |

No per-machine registration.  Every node uses the same wallet address.
Mining revenue from all nodes pools into one NiceHash account.

---

### Step 1 — Create a NiceHash account

1. Go to **https://www.nicehash.com** → Sign Up.
2. Enable **Two-Factor Authentication**:
   - Account → Security → Enable 2FA
   - Use an authenticator app (Google Authenticator, Authy, etc.)
   - 2FA protects your dashboard and payout settings.
3. Set up a **withdrawal method** while you are here:
   - Wallet → Withdraw → add a bank account or crypto address
   - NiceHash pays out in BTC to your internal wallet; you withdraw from there

---

### Step 2 — Get your BTC wallet address

NiceHash provides an internal BTC wallet.  Find your deposit/payout
address at:

**Dashboard → Wallet → BTC → Copy address**

The address looks like: `bc1q...` or `1A1z...` (legacy format).

This is what goes in `agent.env` as `NICEHASH_WALLET`.

**Security rules:**

| Rule | Why |
|---|---|
| Store it only in `/etc/hadcd-agent/agent.env` | Treat like a password — it routes your earnings |
| Never paste it in chat, email, or code | The session CSV only logs the first 8 characters |
| If exposed, generate a new NiceHash deposit address | Old address still works but create a fresh one |

---

### Step 3 — Install T-Rex Miner on each node

> **If you built the autoinstall ISO with `--with-mining`**, T-Rex is
> already installed at `/opt/trex/t-rex` — skip this step.

Otherwise, download the latest Linux release from:

**https://github.com/trexminer/T-Rex/releases**

Pick the `t-rex-<version>-linux.tar.gz` asset.

```bash
sudo mkdir -p /opt/trex
sudo tar -xzf t-rex-*-linux.tar.gz -C /opt/trex/
sudo chmod +x /opt/trex/t-rex
```

Verify:

```bash
/opt/trex/t-rex --version
```

---

### Step 4 — Configure agent.env

```env
# NiceHash GPU mining (T-Rex Miner)
NICEHASH_TREX_PATH=/opt/trex/t-rex
NICEHASH_WALLET=your_btc_address_here
NICEHASH_WORKER_NAME=                  # optional — defaults to hostname
NICEHASH_POOL_HOST=auto.nicehash.com
NICEHASH_POOL_PORT=9200
NICEHASH_ALGO=ethash                   # optional — NiceHash auto-selects most profitable
MINING_GPU_INDEX=0
MINING_GPU_PRESSURE_PCT=20
MINING_GPU_RESUME_PCT=10
MINING_POLL_INTERVAL_SEC=10
MINING_PAYOUT_LOG=/var/lib/hadcd-agent/gpu_mining_sessions.csv
```

Leave `NICEHASH_TREX_PATH` empty to disable GPU mining on a node
that has no GPU.

---

### Step 5 — Verify in the NiceHash dashboard

Once the agent is running and a `gpu_mining_fill` task fires:

1. NiceHash Dashboard → Mining → Workers
2. Your node's hostname (or `NICEHASH_WORKER_NAME`) should appear
3. Hash rate and estimated earnings will show within a few minutes

---

### Earnings reconciliation

Each completed GPU mining session appends a row to:

```
/var/lib/hadcd-agent/gpu_mining_sessions.csv
```

Columns: `start, end, duration_sec, worker, gpu_model, wallet_prefix`

Cross-reference against the NiceHash dashboard for CRA income
reporting.  The CSV records time; the dashboard records dollar amounts.

---

## Part 2 — P2Pool / XMRig (CPU mining)

### What you need

| Item | Where to get it | Goes in |
|---|---|---|
| Monero wallet address | Any XMR wallet app | `XMR_WALLET_ADDRESS` in agent.env |
| XMRig binary (Linux) | github.com/xmrig/xmrig/releases | `/opt/xmrig/xmrig` on each node |

**No account required.**  P2Pool is fully decentralised — there is no
company, no sign-up, no KYC, and no minimum payout.  Revenue goes
directly on-chain to your wallet address.

---

### Step 1 — Get a Monero wallet

You need a Monero (XMR) wallet address.  Options:

| Wallet | Platform | Notes |
|---|---|---|
| **Cake Wallet** | iOS / Android | Easiest for mobile; open source |
| **Monero GUI** | Windows / macOS / Linux | Official desktop wallet |
| **Feather Wallet** | Windows / macOS / Linux | Lightweight desktop option |

Your wallet address starts with `4` and is ~95 characters long.

**Do not use an exchange address** (Binance, Kraken, etc.) for mining
payouts — many exchanges reject small mining deposits.  Use a
self-custodied wallet.

---

### Step 2 — Install XMRig on each node

> **If you built the autoinstall ISO with `--with-mining`**, XMRig is
> already installed at `/opt/xmrig/xmrig` — skip this step.

Otherwise, download the latest Linux release from:

**https://github.com/xmrig/xmrig/releases**

Pick the `xmrig-<version>-linux-x64.tar.gz` asset.

Install on the node:

```bash
sudo mkdir -p /opt/xmrig
sudo tar -xzf xmrig-*-linux-x64.tar.gz --strip-components=1 -C /opt/xmrig/
sudo chmod +x /opt/xmrig/xmrig
```

Verify:

```bash
/opt/xmrig/xmrig --version
```

---

### Step 3 — Configure agent.env

```env
# P2Pool / XMRig CPU mining
XMRIG_PATH=/opt/xmrig/xmrig
XMR_WALLET_ADDRESS=your_monero_address_here
P2POOL_NODE_URL=p2pool.io:3333       # P2Pool mini — right for most nodes
XMRIG_WORKER_NAME=                   # optional — defaults to hostname
XMRIG_THREADS=0                      # 0 = auto (all cores minus one)
CPU_MINING_POLL_SEC=15
CPU_MINING_PAYOUT_LOG=/var/lib/hadcd-agent/cpu_mining_sessions.csv
```

**P2Pool mini vs main:**

| Pool | URL | Use when |
|---|---|---|
| P2Pool mini | `p2pool.io:3333` | Single GPU / entry-level rig (recommended) |
| P2Pool main | `p2pool.io:3334` | High hash rate — pays less frequently but larger amounts |

For a typical HADCD node, use P2Pool mini.

---

### Step 4 — Verify payouts

P2Pool payouts appear directly in your Monero wallet within hours of
mining a share.  No withdrawal step needed.

Check your wallet balance in the app.  Each payout is a small on-chain
transaction from the P2Pool share chain.

You can also monitor your worker at:
**https://p2pool.io/mini/** → search your wallet address

---

### Earnings reconciliation

Each completed CPU mining session appends a row to:

```
/var/lib/hadcd-agent/cpu_mining_sessions.csv
```

Columns: `start, end, duration_sec, worker, wallet_prefix`

XMRig does not report dollar amounts — check the current XMR/CAD rate
and your wallet balance for CRA income reporting.

---

## Summary — what to collect before deploying nodes

| Credential | Where | agent.env variable |
|---|---|---|
| NiceHash BTC wallet address | NiceHash dashboard → Wallet → BTC | `NICEHASH_WALLET` |
| Monero wallet address | Your XMR wallet app | `XMR_WALLET_ADDRESS` |

Binaries (T-Rex Miner, XMRig) are installed per-node — not credentials,
just software.  Both can be added to the autoinstall first-boot process
once the pilot is validated.

---

## Security summary

Both wallet addresses are treated as passwords throughout HADCD:

- Stored only in `/etc/hadcd-agent/agent.env` (mode 0640)
- Never written to task payloads, backend logs, or the task ledger
- Session CSV logs record only the first 8 characters for reconciliation
- No central server ever sees your full wallet address
