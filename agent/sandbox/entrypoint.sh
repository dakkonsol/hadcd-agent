#!/bin/bash
# Phase 10h — HADCD Sandbox container entrypoint
#
# Startup sequence:
#   1. Xvfb     — headless X display (virtual screen)
#   2. PulseAudio — audio sink (Sunshine requires one for audio capture)
#   3. D-Bus session bus
#   4. XFCE4 desktop
#   5. Sunshine  — game-streaming server (Moonlight connects here)
#
# The /config volume is expected to be bind-mounted from the host so that
# Sunshine credentials and paired-client database survive sandbox restarts.
set -euo pipefail

CONFIG_DIR="${SUNSHINE_CONFIG_DIR:-/config}"
SUNSHINE_CONF="${CONFIG_DIR}/sunshine.conf"
CREDS_MARKER="${CONFIG_DIR}/.creds_initialized"

mkdir -p "${CONFIG_DIR}"

# ── 1. Virtual framebuffer ────────────────────────────────────────────────────
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &
XVFB_PID=$!
export DISPLAY=:99
sleep 1
echo "[sandbox] Xvfb started (pid ${XVFB_PID})"

# ── 2. PulseAudio ─────────────────────────────────────────────────────────────
pulseaudio --start --exit-idle-time=-1 --daemon 2>/dev/null || true
sleep 0.5
echo "[sandbox] PulseAudio started"

# ── 3. D-Bus session bus ──────────────────────────────────────────────────────
eval "$(dbus-launch --sh-syntax)"
echo "[sandbox] D-Bus session started"

# ── 4. XFCE4 desktop ─────────────────────────────────────────────────────────
startxfce4 &
sleep 2
echo "[sandbox] XFCE4 desktop started"

# ── 5a. Write Sunshine config on first boot ───────────────────────────────────
if [ ! -f "${SUNSHINE_CONF}" ]; then
    cat > "${SUNSHINE_CONF}" <<EOF
# HADCD Sandbox — Sunshine configuration (auto-generated)
credentials_file = ${CONFIG_DIR}/sunshine_state.json
origin_web_ui_allowed = pc
key_rightalt_to_key_win = enabled
port = 47990
min_log_level = info
EOF
    echo "[sandbox] Sunshine config written to ${SUNSHINE_CONF}"
fi

# ── 5b. Set admin credentials on first boot ───────────────────────────────────
# The handler polls https://localhost:48000/api/* with Basic auth hadcd:hadcd.
# Sunshine's --creds flag writes the salted hash into the state file.
if [ ! -f "${CREDS_MARKER}" ]; then
    sunshine --creds hadcd hadcd "${SUNSHINE_CONF}" 2>/dev/null || true
    touch "${CREDS_MARKER}"
    echo "[sandbox] Sunshine credentials initialized (hadcd:hadcd)"
fi

# ── 5c. Launch Sunshine ───────────────────────────────────────────────────────
echo "[sandbox] Starting Sunshine (web UI on :47990 → host :48000)"
exec sunshine "${SUNSHINE_CONF}"
