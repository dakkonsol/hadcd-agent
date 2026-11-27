# Steady GPU fan tiers

The agent can hold the GPU fans at a **fixed level for the kind of work it is
running**, ramping slowly between levels. A card's stock 0 dB curve stops the
fans when cool and then spins them up hard. On a heat node running long,
sustained loads, that stop/start cycle is most of the noise.

| Tier | When | Default |
|---|---|---|
| `idle` | Nothing running | `FAN_IDLE_PCT=30` |
| `fill` | Dispatcher task in flight (mining, offloaded inference, heat fill) or autonomous mining | `FAN_FILL_PCT=40` |
| `rental` | Customer rental session, Vast.AI draining a renter, or listed on Vast.AI with GPU utilisation ≥ `FAN_RENTAL_UTIL_PCT` | `FAN_RENTAL_PCT=50` |

Changes between levels ramp over `FAN_RAMP_SEC` (default 30 s).

**Safety.** Fixed speeds have no thermal feedback, so the agent hands the fans
back to the **driver's automatic curve** when:

- the GPU reaches `FAN_GUARD_TEMP_C` (80 °C). Tier control resumes once the GPU
  cools below `FAN_GUARD_RELEASE_C` (72 °C).
- the host has a Sunshine session open (it's your GPU then).
- no GPU reading is available.
- the agent stops.
- any fan command fails. Fan control is then disabled for the rest of the run.

Only GPU fans are controlled. Set case and AIO fans to fixed speeds in the BIOS.
Tune the tier levels so that, at full sustained load, the GPU stays at or below
~70 °C, well under the guard.

## Setup

Setting GPU fan speed needs root, and the agent runs as the unprivileged
`hadcd-agent` user. A tiny root-owned helper does the one privileged call.

Requirements: NVIDIA driver 520+, and `nvidia-ml-py` for the system Python.

```bash
sudo apt-get install -y python3-pip
sudo pip3 install --break-system-packages nvidia-ml-py   # or your distro's package

sudo install -o root -g root -m 0755 scripts/hadcd-gpu-fan /usr/local/sbin/hadcd-gpu-fan
echo 'hadcd-agent ALL=(root) NOPASSWD: /usr/local/sbin/hadcd-gpu-fan *' \
  | sudo tee /etc/sudoers.d/hadcd-gpu-fan >/dev/null
sudo chmod 0440 /etc/sudoers.d/hadcd-gpu-fan
sudo visudo -c

# Test as the agent user:
sudo -u hadcd-agent sudo -n /usr/local/sbin/hadcd-gpu-fan set 40
nvidia-smi --query-gpu=fan.speed --format=csv
sudo -u hadcd-agent sudo -n /usr/local/sbin/hadcd-gpu-fan auto
```

The helper accepts only `[--gpu N] set 0..100`, `[--gpu N] auto` or
`[--gpu N] get` and rejects everything else. `get` prints the NVML *target*
fan speed. When the agent takes the fans over from the driver, it starts its ramp
from that target, because some cards (seen on an RTX 3090) always report
`0 %` in `nvidia-smi`'s fan reading even while the fans spin. Updating the
helper needs no sudoers change. It must stay root-owned and not writable by `hadcd-agent`;
otherwise the sudoers rule becomes a privilege escalation.

Then, in `/etc/hadcd-agent/agent.env` (back the file up first):

```
FAN_CONTROL_ENABLED=true
```

Restart the agent only when no rental session container is active.

`FAN_CONTROL_BACKEND=nvml` sets speeds in-process instead. It works only if
the agent runs as root, so it is not recommended.

## Logs

```
fan: taking control at 22% (tier=fill)
fan: tier -> rental
fan: GPU at 81 C >= guard 80 C — handing fans back to the driver curve
fan: restoring driver fan curve (host session active)
```
