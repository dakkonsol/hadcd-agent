#!/usr/bin/env bash
# =====================================================================
# HADCD — Home Assistant Container + provisioner installer
# =====================================================================
#
# Called automatically by install-agent.sh when the ISO was built with
# --with-homeassistant. Can also be run standalone:
#   sudo bash scripts/install-ha.sh
#
# What this script does:
#   1. Installs Docker Engine (official apt repo) if not present.
#   2. Creates /etc/homeassistant/  for HA config persistence.
#   3. Installs hadcd-ha.service    — runs HA Container (port 8123).
#   4. Installs hadcd-provision.service — runs the phone-friendly
#      setup wizard (port 8080) until provisioning is complete.
#   5. Enables both services so they start on every boot.
#
# After installation, the operator:
#   a. Connects phone to the same local network as the node.
#   b. Opens  http://hadcd-node.local:8123  to complete HA onboarding
#      and add your thermostat integration (one-time, ~5 min on phone).
#   c. Opens  http://hadcd-node.local:8080  (HADCD provisioner) to enter
#      the HA long-lived token, pick the thermostat, set the dispatcher
#      URL, and save agent.env — no SSH required.
#
# =====================================================================

set -euo pipefail

info()  { echo "  [HA] $*"; }
warn()  { echo "  [HA!] $*"; }
fatal() { echo "  [HA ERROR] $*" >&2; exit 1; }
rule()  { printf '\n%s\n' "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; }

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR=/opt/hadcd-agent
HA_CONFIG_DIR=/etc/homeassistant

[ "$(id -u)" = "0" ] || fatal "Run this script as root:  sudo bash $0"

# -----------------------------------------------------------------------
# Step 1 — Docker Engine
# -----------------------------------------------------------------------
rule
echo "  Installing Docker Engine"
rule
if command -v docker &>/dev/null; then
    info "Docker already installed: $(docker --version)"
else
    info "Installing Docker Engine via official apt repository ..."
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg

    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | tee /etc/apt/sources.list.d/docker.list >/dev/null

    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin

    systemctl enable docker
    systemctl start docker
    info "Docker Engine installed and started."

    # Add the hadcd-agent service user to the docker group if it exists.
    if id hadcd-agent &>/dev/null; then
        usermod -aG docker hadcd-agent
        info "Added hadcd-agent to the docker group."
    fi
fi

# -----------------------------------------------------------------------
# Step 2 — Home Assistant config directory
# -----------------------------------------------------------------------
rule
echo "  Creating HA config directory"
rule
mkdir -p "$HA_CONFIG_DIR"
info "HA config will persist at $HA_CONFIG_DIR"

# -----------------------------------------------------------------------
# Step 3 — hadcd-ha.service
# -----------------------------------------------------------------------
rule
echo "  Installing hadcd-ha.service (HA Container on port 8123)"
rule
HA_SVC_SRC="$INSTALL_DIR/agent/deploy/systemd/hadcd-ha.service"
if [ -f "$HA_SVC_SRC" ]; then
    cp "$HA_SVC_SRC" /etc/systemd/system/hadcd-ha.service
    systemctl daemon-reload
    systemctl enable hadcd-ha.service
    info "hadcd-ha.service installed and enabled."
    info "HA Container will start on next boot (or: sudo systemctl start hadcd-ha)."
else
    warn "hadcd-ha.service not found at $HA_SVC_SRC — skipping."
fi

# -----------------------------------------------------------------------
# Step 4 — HADCD provisioner
# -----------------------------------------------------------------------
rule
echo "  Installing HADCD provisioner (setup wizard on port 8080)"
rule
# The provisioner runs as `python -m agent provision` from the agent
# venv (see hadcd-provision.service), so there is no separate script to
# install — only the service unit below.
PROVISION_SVC_SRC="$INSTALL_DIR/agent/deploy/systemd/hadcd-provision.service"
if [ -f "$PROVISION_SVC_SRC" ]; then
    cp "$PROVISION_SVC_SRC" /etc/systemd/system/hadcd-provision.service
    systemctl daemon-reload
    systemctl enable hadcd-provision.service
    info "hadcd-provision.service installed and enabled."
else
    warn "hadcd-provision.service not found at $PROVISION_SVC_SRC — skipping."
fi

# -----------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------
rule
echo ""
echo "  Home Assistant setup complete.  Next steps:"
echo ""
echo "  The node will start HA automatically on next boot."
echo "  To start it now:  sudo systemctl start hadcd-ha"
echo ""
echo "  Once HA is running, open this page on your phone:"
echo "    http://hadcd-node.local:8123"
echo ""
echo "  1. Create your HA account (first-time setup, ~60 seconds)"
echo "  2. Skip device discovery for now"
echo "  3. Settings → Integrations → Add Integration → search your thermostat brand"
echo "     (e.g. Tuya, Moes, Ecobee, Nest) and follow the pairing steps"
echo "  4. Settings → Profile (your name) → Long-Lived Access Tokens"
echo "     Create a token named 'hadcd', copy it"
echo ""
echo "  Then open the HADCD provisioner on your phone:"
echo "    http://hadcd-node.local:8080"
echo ""
echo "  Paste the token, pick your thermostat, fill in the dispatcher URL"
echo "  and enrollment token, press Save. The node agent will start"
echo "  automatically and appear in the HADCD dashboard."
echo ""
rule
