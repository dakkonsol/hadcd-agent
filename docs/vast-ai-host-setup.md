# Vast.AI Host Setup — HADCD Node Guide

This document covers everything needed to register a HADCD node as a
Vast.AI GPU host.  Follow it once per Vast.AI account (for the API key)
and once per physical node (for the Machine ID).

---

## What Vast.AI does for HADCD

HADCD's Phase 11 integration lists your node on the Vast.AI GPU
marketplace during cold-weather windows and unlists it when the window
ends.  Renters pay Vast.AI; Vast.AI pays you minus ~20% commission.
The agent handles listing/unlisting automatically — you never touch the
Vast.AI dashboard for day-to-day operation.

---

## Step 1 — Create a Vast.AI host account

1. Go to **https://vast.ai/become-a-host** and create an account.
2. Enable **Two-Factor Authentication** for your account login:
   - Account menu → Security → Enable 2FA
   - Use an authenticator app (Google Authenticator, Authy, etc.)
   - 2FA protects your dashboard and payout settings.
3. Set up a **payout method** while you are here:
   - Billing → Withdraw → Add payout method
   - Options: bank transfer (ACH/wire), PayPal, or crypto
   - Minimum withdrawal is typically $10–20 USD
   - **Do this before you start earning** so you are not blocked later.

---

## Step 2 — Get your API key

The API key lets the HADCD agent list and unlist your machines
programmatically without a browser login.

1. Account menu → **API Keys** → generate or copy your key.
2. The key is a long alphanumeric string (~64 characters).

**Security rules — non-negotiable:**

| Rule | Why |
|---|---|
| Store it only in `/etc/hadcd-agent/agent.env` on each node | It grants full account control |
| Never paste it into chat, email, Slack, or a GitHub commit | Treat it like a bank password |
| If it is ever exposed anywhere, regenerate it immediately | Old key is dead the moment it leaves your `.env` |
| One key works for all machines under your account | You do not need separate keys per node |

---

## Step 3 — Install the Vast.AI CLI on a node

Run this after the node is up and the HADCD agent venv exists:

```bash
sudo /opt/hadcd-agent/.venv/bin/pip install vastai
```

Authenticate the CLI with your API key:

```bash
sudo -u hadcd-agent /opt/hadcd-agent/.venv/bin/vastai set api-key YOUR_KEY_HERE
```

Verify it works:

```bash
sudo -u hadcd-agent /opt/hadcd-agent/.venv/bin/vastai show user
```

---

## Step 4 — Register the machine

Each physical node must be registered once.  This runs a GPU benchmark
and assigns a permanent Machine ID.

```bash
sudo -u hadcd-agent /opt/hadcd-agent/.venv/bin/vastai host register
```

The process takes 5–15 minutes (GPU benchmark).  When it completes,
note the **Machine ID** — a number like `12345`.

Find it any time with:

```bash
sudo -u hadcd-agent /opt/hadcd-agent/.venv/bin/vastai show machines
```

---

## Step 5 — Configure agent.env

Edit `/etc/hadcd-agent/agent.env` on the node and set:

```env
# Vast.AI provider integration
VASTAI_API_KEY=your_key_here
VASTAI_MACHINE_ID=12345
```

Also set the node's location (used to determine cold-weather windows):

```env
NODE_LATITUDE=45.4215
NODE_LONGITUDE=-75.6972
NODE_LOCATION_LABEL=Ottawa, ON, CA
```

Start the agent:

```bash
sudo systemctl start hadcd-agent
sudo systemctl status hadcd-agent
```

---

## Step 6 — Set pricing on the Vast.AI dashboard

The agent lists and unlists your machine, but it does not set pricing.
Do this once per machine in the Vast.AI dashboard:

1. Dashboard → Machines → your machine → Edit
2. Set a **$/GPU-hour** price.  Check what comparable hardware lists at
   on the marketplace to stay competitive.
3. Configure disk space available to renters.
4. Enable the machine listing.

The agent will then take it on/off the market automatically based on
weather.  Your pricing stays set — you only change it if you want to
adjust rates.

---

## Machine ID log

Keep a record of your registered machines here or in a secure note.

| Node name | Location | Machine ID | Registered |
|---|---|---|---|
| *(fill in after each registration)* | | | |

---

## Rotating a compromised API key

If your API key is ever exposed (pasted in chat, found in a log, etc.):

1. vastai.com → Account → API Keys → **Regenerate**
2. Update `/etc/hadcd-agent/agent.env` on every node:
   ```bash
   sudo nano /etc/hadcd-agent/agent.env
   # Update VASTAI_API_KEY=new_key_here
   sudo systemctl restart hadcd-agent
   ```
3. The old key stops working immediately upon regeneration.

---

## Troubleshooting

**`vastai show user` returns an auth error**
The API key in `agent.env` is wrong or the key was regenerated.
Re-run `vastai set api-key YOUR_KEY_HERE` and restart the agent.

**`vastai host register` fails with a CUDA error**
NVIDIA drivers are not loaded.  Check: `nvidia-smi`.  If it fails,
reinstall drivers: `sudo ubuntu-drivers autoinstall && sudo reboot`.

**Machine shows as offline in Vast.AI dashboard**
The Vast.AI software (vastnbd / vastai daemon) may not be running.
Check: `sudo systemctl status vastai` or re-run `vastai host register`.

**Agent lists the machine but no rentals come in**
Normal — the marketplace is competitive.  Check your pricing against
similar hardware at cloud.vast.ai.  Lower $/GPU-hour increases demand.

**Port forwarding warning in Vast.AI dashboard**
Vast.AI may require certain ports to be open (22, and a range for
container SSH).  Configure your router to forward these to the node's
LAN IP.  Vast.AI's dashboard shows exactly which ports are needed.
