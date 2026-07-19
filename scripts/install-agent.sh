#!/usr/bin/env bash
# =====================================================================
# HADCD node agent — one-shot installer for Linux / systemd
# =====================================================================
#
# Installs the agent as a hardened systemd service with:
#   * automatic crash recovery (Restart=always)
#   * systemd watchdog (frozen event loop detection + restart)
#   * auto-start on boot
#   * restricted filesystem access (runs as unprivileged hadcd-agent user)
#
# Requirements:
#   * Debian/Ubuntu 22.04+ or RHEL/Rocky 8+ (any systemd 245+ distro)
#   * Python 3.11 or newer on PATH as python3.11 (or python3)
#   * Internet access for pip install
#
# Usage:
#   sudo bash scripts/install-agent.sh            # from the repo root
#
# After the installer finishes:
#   1. Edit /etc/hadcd-agent/agent.env  (HADCD_API, ENROLLMENT_TOKENS,
#      NODE_NAME, MAX_POWER_KW, BMS_SOURCE, and — if Sunshine is
#      installed — SESSION_SOURCE + SUNSHINE_PASSWORD).
#   2. Sanity-run in the foreground to confirm config (optional but
#      strongly recommended — the installer prints the exact command).
#   3. sudo systemctl start hadcd-agent

set -euo pipefail

# -----------------------------------------------------------------------
# Argument parsing
# -----------------------------------------------------------------------
# Flags accepted when the script is piped from curl (--remote mode) or
# called directly with pre-filled values.
HADCD_API=""
ENROLLMENT_TOKEN=""
NODE_NAME=""
NODE_TYPE=""
MAX_POWER_KW=""
REMOTE_MODE=false   # when true: git-clone the repo first

while [[ $# -gt 0 ]]; do
    case "$1" in
        --api)           HADCD_API="$2";         shift 2 ;;
        --token)         ENROLLMENT_TOKEN="$2";  shift 2 ;;
        --name)          NODE_NAME="$2";         shift 2 ;;
        --node-type)     NODE_TYPE="$2";         shift 2 ;;
        --max-power-kw)  MAX_POWER_KW="$2";      shift 2 ;;
        --remote)        REMOTE_MODE=true;        shift   ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------
INSTALL_DIR=/opt/hadcd-agent
CONF_DIR=/etc/hadcd-agent
DATA_DIR=/var/lib/hadcd-agent
SERVICE_DEST=/etc/systemd/system/hadcd-agent.service
AGENT_USER=hadcd-agent
AGENT_GROUP=hadcd-agent
GITHUB_REPO="https://github.com/dakkonsol/hadcd-agent.git"

# In remote mode we clone the repo to a temp dir and point REPO_ROOT there.
# In local mode REPO_ROOT is the parent of this script's directory.
if [ "$REMOTE_MODE" = "true" ]; then
    REPO_ROOT="$(mktemp -d)"
    _CLONE_CLEANUP=true
else
    REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
    _CLONE_CLEANUP=false
fi

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------
info()  { echo "  [+] $*"; }
warn()  { echo "  [!] $*"; }
fatal() { echo "ERROR: $*" >&2; exit 1; }
rule()  { printf '\n%s\n' "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; }

# -----------------------------------------------------------------------
# Pre-flight checks
# -----------------------------------------------------------------------
if [ "$(id -u)" != "0" ]; then
    fatal "Run this script as root:  sudo bash $0"
fi

# -----------------------------------------------------------------------
# Remote mode — clone the repo
# -----------------------------------------------------------------------
if [ "$REMOTE_MODE" = "true" ]; then
    rule
    echo "  REMOTE MODE — cloning HADCD repository"
    rule
    if ! command -v git &>/dev/null; then
        info "Installing git..."
        if command -v apt-get &>/dev/null; then
            apt-get install -y --no-install-recommends git
        elif command -v dnf &>/dev/null; then
            dnf install -y git
        else
            fatal "Cannot install git — unsupported package manager."
        fi
    fi
    git clone --depth=1 "$GITHUB_REPO" "$REPO_ROOT"
    info "Repository cloned to $REPO_ROOT"
    # Register a cleanup trap so the temp dir is removed on exit.
    trap 'rm -rf "$REPO_ROOT"' EXIT
fi

if ! command -v python3.11 &>/dev/null && ! command -v python3 &>/dev/null; then
    fatal "Python 3.11+ is required. Install it and re-run."
fi

# Prefer python3.11 explicitly, fall back to system python3.
if command -v python3.11 &>/dev/null; then
    PYTHON=python3.11
else
    PYTHON=python3
fi

# Check the Python version is ≥ 3.11.
PY_VER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR="${PY_VER%%.*}"
PY_MINOR="${PY_VER#*.}"
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    fatal "Python 3.11 or newer required (found $PY_VER). Install python3.11 and re-run."
fi

if ! command -v systemctl &>/dev/null; then
    fatal "systemd is required. This installer does not support other init systems."
fi

# -----------------------------------------------------------------------
# Step 1 — service user
# -----------------------------------------------------------------------
rule
echo "  STEP 1/7 — Service user"
rule
if id -u "$AGENT_USER" &>/dev/null; then
    info "User '$AGENT_USER' already exists — skipping creation."
else
    useradd --system \
        --home "$DATA_DIR" \
        --no-create-home \
        --shell /usr/sbin/nologin \
        --comment "HADCD node agent" \
        "$AGENT_USER"
    info "Created system user: $AGENT_USER"
fi

# Add the agent user to the docker group (if docker is installed).
if getent group docker &>/dev/null; then
    usermod -aG docker "$AGENT_USER"
    info "Added $AGENT_USER to the 'docker' group (Docker socket access)."
else
    warn "Docker group not found. If you install Docker later, run:"
    warn "  sudo usermod -aG docker $AGENT_USER && sudo systemctl restart hadcd-agent"
fi

# -----------------------------------------------------------------------
# Step 2 — directories
# -----------------------------------------------------------------------
rule
echo "  STEP 2/7 — Directories"
rule
install -d -m 0755               "$INSTALL_DIR"
install -d -m 0750               "$CONF_DIR"
install -d -m 0750 -o "$AGENT_USER" -g "$AGENT_GROUP" "$DATA_DIR"
info "Directories ready:  $INSTALL_DIR  $CONF_DIR  $DATA_DIR"

# -----------------------------------------------------------------------
# Step 3 — agent code
# -----------------------------------------------------------------------
rule
echo "  STEP 3/7 — Agent code"
rule
# Copy agent/ and hadcd_workloads/ from the repo; these are the only two
# directories needed at runtime.
for pkg in agent hadcd_workloads; do
    src="$REPO_ROOT/$pkg"
    if [ ! -d "$src" ]; then
        fatal "Expected '$src' to exist. Run the installer from the repo root."
    fi
    cp -r "$src" "$INSTALL_DIR/"
    info "Copied $pkg/ to $INSTALL_DIR/$pkg"
done

# Copy scripts/ so helper scripts (vast-register.sh etc.) are available
# at /opt/hadcd-agent/scripts/ after install.
install -d -m 0755 "$INSTALL_DIR/scripts"
cp "$REPO_ROOT/scripts/"*.sh "$INSTALL_DIR/scripts/"
chmod +x "$INSTALL_DIR/scripts/"*.sh
info "Scripts copied to $INSTALL_DIR/scripts/"
# The agent/ directory should be owned by root; hadcd-agent can read it.
chown -R root:root "$INSTALL_DIR"

# -----------------------------------------------------------------------
# Step 3b — Mining binaries (optional, pre-bundled in autoinstall ISO)
# -----------------------------------------------------------------------
# When the ISO was built with --with-mining, T-Rex and XMRig land at
# $REPO_ROOT/opt/trex/ and $REPO_ROOT/opt/xmrig/.  Install them now.
# If the directories are absent this is a clean no-op — mining is simply
# disabled until the operator installs the binaries manually.
#
# T-Rex (GPU mining via NiceHash stratum, payouts in BTC):
#   Binary: /opt/trex/t-rex
#   Config: NICEHASH_TREX_PATH=/opt/trex/t-rex in agent.env
#
# XMRig (CPU mining via P2Pool, payouts in XMR):
#   Binary: /opt/xmrig/xmrig
#   Config: XMRIG_PATH=/opt/xmrig/xmrig in agent.env
for tool in trex xmrig; do
    src="$REPO_ROOT/opt/$tool"
    dst="/opt/$tool"
    # The executable inside each directory matches the tool name, except
    # T-Rex unpacks as 't-rex' (with a hyphen).
    bin_name="$tool"
    [ "$tool" = "trex" ] && bin_name="t-rex"
    if [ -d "$src" ]; then
        install -d -m 0755 "$dst"
        cp -r "$src"/. "$dst/"
        find "$dst" -maxdepth 1 -name "$bin_name" -exec chmod +x {} \;
        info "Installed $dst/$bin_name (bundled by autoinstall ISO)."
    else
        info "$tool not bundled — install manually if needed (see docs/mining-setup.md)."
    fi
done

# -----------------------------------------------------------------------
# Step 3c — NVIDIA GPU drivers
# -----------------------------------------------------------------------
# Detects NVIDIA hardware and installs the recommended driver via
# ubuntu-drivers.  On success, writes a reboot flag so the first-boot
# service can reboot immediately after this script exits — drivers must
# be loaded before the agent (and Vast.AI registration) can use the GPU.
rule
echo "  STEP 3c — NVIDIA GPU drivers"
rule
_HAS_NVIDIA=false
if command -v lspci &>/dev/null; then
    if lspci 2>/dev/null | grep -qiE "nvidia|geforce|quadro|tesla"; then
        _HAS_NVIDIA=true
    fi
fi

if [ "$_HAS_NVIDIA" = "true" ]; then
    info "NVIDIA GPU detected."
    if command -v ubuntu-drivers &>/dev/null; then
        info "Running ubuntu-drivers autoinstall (downloads ~500 MB, allow 5-10 min) ..."
        if ubuntu-drivers autoinstall; then
            info "NVIDIA drivers installed successfully."
            info "A reboot is required — the first-boot service will reboot automatically."
            # Signal hadcd-firstboot.service to reboot once this script exits.
            touch /var/lib/hadcd-agent/pending-driver-reboot
        else
            warn "ubuntu-drivers autoinstall failed."
            warn "Install drivers manually after setup:"
            warn "  sudo ubuntu-drivers autoinstall && sudo reboot"
        fi
    else
        warn "ubuntu-drivers not found.  Install NVIDIA drivers manually:"
        warn "  sudo apt install ubuntu-drivers-common"
        warn "  sudo ubuntu-drivers autoinstall && sudo reboot"
    fi
else
    info "No NVIDIA GPU detected — skipping driver install."
    info "(Add a GPU later? Run: sudo ubuntu-drivers autoinstall && sudo reboot)"
fi

# -----------------------------------------------------------------------
# Step 4 — Python venv + dependencies
# -----------------------------------------------------------------------
rule
echo "  STEP 4/7 — Python venv"
rule
if [ ! -d "$INSTALL_DIR/.venv" ]; then
    "$PYTHON" -m venv "$INSTALL_DIR/.venv"
    info "Created venv at $INSTALL_DIR/.venv"
else
    info "Venv already exists — skipping creation."
fi
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet \
    -r "$INSTALL_DIR/agent/requirements.txt"
info "Dependencies installed."

# -----------------------------------------------------------------------
# Step 5 — env / config file
# -----------------------------------------------------------------------
rule
echo "  STEP 5/7 — Configuration file"
rule
if [ -f "$CONF_DIR/agent.env" ]; then
    info "$CONF_DIR/agent.env already exists — leaving it untouched."
else
    cp "$INSTALL_DIR/agent/config.env.example" "$CONF_DIR/agent.env"
    chown root:"$AGENT_GROUP" "$CONF_DIR/agent.env"
    chmod 0640 "$CONF_DIR/agent.env"
    info "Template installed at $CONF_DIR/agent.env"
fi

# Patch in values supplied on the command line (--api, --token, --name,
# --node-type, --max-power-kw).  Each sed call is idempotent: it replaces
# the placeholder value in the template or an existing value.
_patch_env() {
    local key="$1" value="$2"
    if grep -q "^${key}=" "$CONF_DIR/agent.env"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$CONF_DIR/agent.env"
    else
        echo "${key}=${value}" >> "$CONF_DIR/agent.env"
    fi
}

_ENV_PATCHED=false
if [ -n "$HADCD_API" ];        then _patch_env "HADCD_API"          "$HADCD_API";         _ENV_PATCHED=true; fi
if [ -n "$ENROLLMENT_TOKEN" ]; then _patch_env "ENROLLMENT_TOKENS"  "$ENROLLMENT_TOKEN";  _ENV_PATCHED=true; fi
if [ -n "$NODE_NAME" ];        then _patch_env "NODE_NAME"          "$NODE_NAME";         _ENV_PATCHED=true; fi
if [ -n "$NODE_TYPE" ];        then _patch_env "NODE_TYPE"          "$NODE_TYPE";                            fi
if [ -n "$MAX_POWER_KW" ];     then _patch_env "MAX_POWER_KW"       "$MAX_POWER_KW";                         fi

if [ "$_ENV_PATCHED" = "true" ]; then
    info "Core values written to $CONF_DIR/agent.env (api, token, name)."
else
    warn "EDIT THIS FILE before starting the service (see summary below)."
fi

# -----------------------------------------------------------------------
# Step 6 — systemd service unit
# -----------------------------------------------------------------------
rule
echo "  STEP 6/7 — systemd service unit"
rule
SERVICE_SRC="$INSTALL_DIR/agent/deploy/systemd/hadcd-agent.service"
if [ ! -f "$SERVICE_SRC" ]; then
    fatal "Service file not found at $SERVICE_SRC — was the agent code copied?"
fi
cp "$SERVICE_SRC" "$SERVICE_DEST"

# WiFi provisioning service (one-shot; no-op when ethernet is connected).
WIFI_SVC_SRC="$INSTALL_DIR/agent/deploy/systemd/hadcd-wifi-provision.service"
WIFI_SVC_DEST=/etc/systemd/system/hadcd-wifi-provision.service
if [ -f "$WIFI_SVC_SRC" ]; then
    cp "$WIFI_SVC_SRC" "$WIFI_SVC_DEST"
    systemctl daemon-reload
    systemctl enable hadcd-wifi-provision
    info "WiFi provisioner service installed and enabled."
else
    warn "hadcd-wifi-provision.service not found — skipping WiFi provisioner install."
    systemctl daemon-reload
fi
systemctl enable hadcd-agent   # enable for auto-start on boot; don't start yet

# Vast.AI registration service (one-shot; runs after NVIDIA reboot if
# VASTAI_API_KEY is set in agent.env and machine is not yet registered).
VAST_SVC_SRC="$INSTALL_DIR/agent/deploy/systemd/hadcd-vast-register.service"
VAST_SVC_DEST=/etc/systemd/system/hadcd-vast-register.service
if [ -f "$VAST_SVC_SRC" ]; then
    cp "$VAST_SVC_SRC" "$VAST_SVC_DEST"
    systemctl enable hadcd-vast-register
    info "Vast.AI registration service installed and enabled."
fi

info "Service unit installed at $SERVICE_DEST"
info "Service enabled for auto-start on boot."
info "(The service is NOT started yet — edit the config file first.)"

# -----------------------------------------------------------------------
# Step 6b — Home Assistant (optional)
# -----------------------------------------------------------------------
# When the ISO was built with --with-homeassistant, the builder writes a
# marker file (WITH_HOMEASSISTANT) into the source tree alongside the
# scripts.  If it is present, delegate to install-ha.sh which installs
# Docker + HA Container + the provisioner web wizard.
rule
echo "  STEP 6b — Home Assistant (optional)"
rule
HA_MARKER="$REPO_ROOT/WITH_HOMEASSISTANT"
HA_SCRIPT="$REPO_ROOT/scripts/install-ha.sh"
if [ -f "$HA_MARKER" ]; then
    if [ -f "$HA_SCRIPT" ]; then
        info "WITH_HOMEASSISTANT marker found — running install-ha.sh ..."
        bash "$HA_SCRIPT"
    else
        warn "WITH_HOMEASSISTANT marker found but install-ha.sh is missing."
        warn "  Install Home Assistant manually after setup."
    fi
else
    info "No Home Assistant marker — skipping HA install."
    info "(Rebuild the ISO with --with-homeassistant to enable this step.)"
fi

# -----------------------------------------------------------------------
# Step 7 — AC power / BIOS auto-start
# -----------------------------------------------------------------------
rule
echo "  STEP 7/7 — Auto-start on AC power"
rule
# If ipmitool is available and an IPMI interface is accessible (server
# hardware), configure the chassis power policy in one command.
# Consumer mini PCs / desktops require a BIOS visit — print instructions.
AC_CONFIGURED=false
if command -v ipmitool &>/dev/null; then
    if ipmitool chassis status &>/dev/null 2>&1; then
        if ipmitool chassis policy always-on &>/dev/null 2>&1; then
            info "IPMI detected — chassis power policy set to 'always-on'. ✓"
            AC_CONFIGURED=true
        else
            warn "IPMI detected but 'chassis policy always-on' failed — set manually."
        fi
    fi
fi

if [ "$AC_CONFIGURED" = "false" ]; then
    echo ""
    echo "  This machine's power-on-after-AC-loss policy must be set in BIOS."
    echo "  Reboot, enter BIOS/UEFI setup, and find the setting below for your"
    echo "  hardware. Set it to 'Power On' / 'Always On'."
    echo ""
    echo "  ┌─ Intel NUC ────────────────────────────────────────────────────┐"
    echo "  │  Power → After Power Failure → Power On                        │"
    echo "  ├─ ASUS desktop / mini PC ────────────────────────────────────── ┤"
    echo "  │  Advanced → APM Configuration →                                │"
    echo "  │    Restore on AC Power Loss → Power On                         │"
    echo "  ├─ ASRock ────────────────────────────────────────────────────── ┤"
    echo "  │  Advanced → ACPI Configuration →                               │"
    echo "  │    Restore after AC Power Loss → Power On                      │"
    echo "  ├─ Gigabyte ──────────────────────────────────────────────────── ┤"
    echo "  │  Settings → Miscellaneous → AC Back → Always On                │"
    echo "  ├─ Beelink / Minisforum / generic mini PC (AMI BIOS) ─────────── ┤"
    echo "  │  Chipset → South Bridge → AC Loss Power State → Always On      │"
    echo "  ├─ MSI ───────────────────────────────────────────────────────── ┤"
    echo "  │  Settings → Advanced → Power Management →                      │"
    echo "  │    Restore after AC Power Loss → Always On                     │"
    echo "  └─────────────────────────────────────────────────────────────── ┘"
    echo ""
    echo "  Effect: toggling the wall switch / power bar becomes the on/off"
    echo "  switch. The machine boots automatically; no one needs to be on-site."
fi

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
rule
echo ""
echo "  Installation complete.  What to do next:"
echo ""
if [ "$_ENV_PATCHED" = "true" ]; then
echo "  1. (Optional) Review the config file — core values were pre-filled:"
echo "       sudo nano $CONF_DIR/agent.env"
echo ""
echo "     If Sunshine is installed, also add:"
echo "       SESSION_SOURCE=sunshine"
echo "       SUNSHINE_PASSWORD=<your Sunshine admin password>"
echo ""
else
echo "  1. Edit the config file:"
echo "       sudo nano $CONF_DIR/agent.env"
echo ""
echo "     At minimum, set:"
echo "       HADCD_API          — URL of the central HADCD server"
echo "       ENROLLMENT_TOKENS  — enrollment secret from the central operator"
echo "       NODE_NAME          — a human-readable name for this building"
echo "       MAX_POWER_KW       — sustained power draw of the compute hardware"
echo ""
echo "     If Sunshine is installed, also set:"
echo "       SESSION_SOURCE=sunshine"
echo "       SUNSHINE_PASSWORD=<your Sunshine admin password>"
echo ""
fi
echo "  2. Sanity-run in the foreground (optional but recommended):"
echo "       sudo -u $AGENT_USER \\"
echo "         env \$(grep -v '^#' $CONF_DIR/agent.env | xargs) \\"
echo "         $INSTALL_DIR/.venv/bin/python -m agent run"
echo "     Watch for 'enrolled as node <UUID>'. Press Ctrl-C to stop."
echo ""
echo "  3. Start the service:"
echo "       sudo systemctl start hadcd-agent"
echo "       sudo systemctl status hadcd-agent"
echo "       journalctl -u hadcd-agent -f"
if [ "$AC_CONFIGURED" = "false" ]; then
    echo ""
    echo "  4. Set the BIOS power-on-after-AC-loss option (see Step 7 above)"
    echo "     to enable unattended recovery from power outages."
fi
rule
echo ""
