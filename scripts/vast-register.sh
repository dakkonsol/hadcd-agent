#!/usr/bin/env bash
# =====================================================================
# HADCD Vast.AI host registration
# =====================================================================
#
# Runs once at second boot (after NVIDIA drivers have loaded) via
# hadcd-vast-register.service.  Safe to run manually at any time.
#
# What it does:
#   1. Reads VASTAI_API_KEY from /etc/hadcd-agent/agent.env.
#      Exits cleanly if the key is not set — no Vast.AI on this node.
#   2. Skips if VASTAI_MACHINE_ID is already set in agent.env.
#   3. Installs the vastai CLI into the agent venv if not present.
#   4. Authenticates the CLI with the API key.
#   5. Runs `vastai host register` (GPU benchmark, ~5-15 min).
#   6. Reads back the assigned Machine ID and writes it to agent.env
#      automatically so the operator does not need to look it up.
#   7. Writes /var/lib/hadcd-agent/vast-registered so this script is
#      a no-op on all future boots.
#
# If the Machine ID cannot be auto-detected, the script prints clear
# instructions for the manual one-liner.
#
# Usage:
#   Automatic: enabled by install-agent.sh; runs at boot #2 via
#              hadcd-vast-register.service.
#   Manual re-run: sudo bash /opt/hadcd-agent/scripts/vast-register.sh
#   Reset (re-register): sudo rm /var/lib/hadcd-agent/vast-registered
#                        sudo systemctl start hadcd-vast-register
# =====================================================================

set -euo pipefail

CONF_FILE=/etc/hadcd-agent/agent.env
REGISTERED_FLAG=/var/lib/hadcd-agent/vast-registered
VENV=/opt/hadcd-agent/.venv
VASTAI_CMD="$VENV/bin/vastai"
LOG_FILE=/var/log/hadcd-vast-register.log

info()  { echo "  [+] $*" | tee -a "$LOG_FILE"; }
warn()  { echo "  [!] $*" | tee -a "$LOG_FILE"; }
fatal() { echo "ERROR: $*" | tee -a "$LOG_FILE" >&2; exit 1; }

echo "" >> "$LOG_FILE"
echo "$(date '+%Y-%m-%d %H:%M:%S') — hadcd-vast-register starting" >> "$LOG_FILE"

# -----------------------------------------------------------------------
# Pre-flight
# -----------------------------------------------------------------------

if [ -f "$REGISTERED_FLAG" ]; then
    info "Already registered (flag exists at $REGISTERED_FLAG) — nothing to do."
    exit 0
fi

if [ ! -f "$CONF_FILE" ]; then
    warn "agent.env not found at $CONF_FILE — skipping Vast.AI registration."
    exit 0
fi

# Read VASTAI_API_KEY and VASTAI_MACHINE_ID from agent.env.
# Safely export only the two keys we care about; ignore comments.
_read_env() {
    grep -E "^$1=" "$CONF_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"
}
VASTAI_API_KEY="$(_read_env VASTAI_API_KEY)"
VASTAI_MACHINE_ID="$(_read_env VASTAI_MACHINE_ID)"

if [ -z "$VASTAI_API_KEY" ]; then
    info "VASTAI_API_KEY not set in agent.env — skipping Vast.AI registration."
    info "To enable Vast.AI integration, set VASTAI_API_KEY in $CONF_FILE"
    info "and then run: sudo systemctl start hadcd-vast-register"
    exit 0
fi

if [ -n "$VASTAI_MACHINE_ID" ]; then
    info "VASTAI_MACHINE_ID already set ($VASTAI_MACHINE_ID) — skipping registration."
    touch "$REGISTERED_FLAG"
    exit 0
fi

# Check that NVIDIA drivers are loaded — vastai host register will fail without them.
if ! command -v nvidia-smi &>/dev/null || ! nvidia-smi &>/dev/null 2>&1; then
    warn "nvidia-smi not available or GPU not visible."
    warn "NVIDIA drivers may not be loaded.  If you just installed them, reboot first:"
    warn "  sudo reboot"
    warn "Then the registration will resume automatically."
    exit 1
fi

info "NVIDIA GPU confirmed via nvidia-smi."

# -----------------------------------------------------------------------
# Install vastai CLI if not present
# -----------------------------------------------------------------------
if [ ! -f "$VASTAI_CMD" ]; then
    info "Installing Vast.AI CLI into agent venv ..."
    "$VENV/bin/pip" install --quiet vastai
    info "vastai CLI installed."
fi

# -----------------------------------------------------------------------
# Authenticate
# -----------------------------------------------------------------------
info "Setting Vast.AI API key ..."
"$VASTAI_CMD" set api-key "$VASTAI_API_KEY" >> "$LOG_FILE" 2>&1

# -----------------------------------------------------------------------
# Register the machine
# -----------------------------------------------------------------------
info "Running vastai host register (GPU benchmark — takes 5-15 minutes) ..."
info "Follow progress: tail -f $LOG_FILE"

if ! "$VASTAI_CMD" host register >> "$LOG_FILE" 2>&1; then
    warn "vastai host register exited with an error."
    warn "Check the log: cat $LOG_FILE"
    warn "Common causes:"
    warn "  * GPU not visible to CUDA — reboot and retry"
    warn "  * Port forwarding required — see docs/vast-ai-host-setup.md"
    warn "  * API key revoked — regenerate at vast.ai and update agent.env"
    exit 1
fi

info "vastai host register completed."

# -----------------------------------------------------------------------
# Detect assigned Machine ID
# -----------------------------------------------------------------------
# `vastai show machines` prints a table; the Machine ID is the first
# numeric column of the first data row.
MACHINE_ID=$("$VASTAI_CMD" show machines 2>/dev/null | python3 -c "
import sys
for line in sys.stdin:
    parts = line.split()
    if parts and parts[0].isdigit():
        print(parts[0])
        break
" 2>/dev/null || echo "")

if [ -n "$MACHINE_ID" ]; then
    info "Machine ID detected: $MACHINE_ID"

    # Write VASTAI_MACHINE_ID into agent.env.
    if grep -q '^VASTAI_MACHINE_ID=' "$CONF_FILE"; then
        # Update existing (probably empty) placeholder.
        sed -i "s|^VASTAI_MACHINE_ID=.*|VASTAI_MACHINE_ID=$MACHINE_ID|" "$CONF_FILE"
    else
        # Append if not present.
        echo "VASTAI_MACHINE_ID=$MACHINE_ID" >> "$CONF_FILE"
    fi
    info "VASTAI_MACHINE_ID=$MACHINE_ID written to $CONF_FILE"
else
    warn "Could not auto-detect Machine ID from 'vastai show machines'."
    warn "Run manually and add the ID to agent.env:"
    warn "  vastai show machines"
    warn "  sudo nano $CONF_FILE   # set VASTAI_MACHINE_ID=<number>"
fi

# -----------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------
touch "$REGISTERED_FLAG"
info "Vast.AI registration complete."
info ""
info "Next step: set pricing for this machine in the Vast.AI dashboard."
info "See docs/vast-ai-host-setup.md — Step 6."
info ""
info "The HADCD agent will handle listing/unlisting automatically"
info "based on weather-driven cold windows."
