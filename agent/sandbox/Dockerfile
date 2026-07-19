# Phase 10h — HADCD Disposable Sandbox image
# Ubuntu 22.04 + XFCE4 virtual desktop + Sunshine game-streaming server
#
# Build (from repo root):
#   docker build -t hadcd-sandbox:latest agent/sandbox/
#
# Sunshine is pinned to a known-good release.  To upgrade, bump
# SUNSHINE_VERSION and verify the .deb name at:
#   https://github.com/LizardByte/Sunshine/releases
FROM ubuntu:22.04

ARG SUNSHINE_VERSION=0.23.1
ENV DEBIAN_FRONTEND=noninteractive \
    TZ=UTC \
    LANG=C.UTF-8

# ── Desktop + audio + streaming prerequisites ─────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        # Virtual framebuffer
        xvfb \
        # Lightweight desktop environment
        xfce4 \
        xfce4-terminal \
        # Session bus (required by XFCE and Sunshine)
        dbus-x11 \
        # Utility for display queries
        x11-utils \
        # Audio server — Sunshine requires a PulseAudio sink for audio capture
        pulseaudio \
        # Download tool for Sunshine DEB
        curl \
        ca-certificates \
        # sudo / misc
        sudo \
        procps \
    && rm -rf /var/lib/apt/lists/*

# ── Install Sunshine ──────────────────────────────────────────────────────────
# The DEB installs its own shared-library deps via apt.
# If this URL 404s on a future version, check the GitHub release assets:
#   https://github.com/LizardByte/Sunshine/releases/tag/v${SUNSHINE_VERSION}
RUN apt-get update \
    && curl -fsSL \
        "https://github.com/LizardByte/Sunshine/releases/download/v${SUNSHINE_VERSION}/sunshine_ubuntu-22.04_amd64.deb" \
        -o /tmp/sunshine.deb \
    && apt-get install -y --no-install-recommends /tmp/sunshine.deb \
    && rm /tmp/sunshine.deb \
    && rm -rf /var/lib/apt/lists/*

# ── Entrypoint ────────────────────────────────────────────────────────────────
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# ── Ports (host maps these to +10 to avoid collision with Mode-2 Sunshine) ────
# Container → Host offset mapping:
#   47984/tcp → 47994  (Moonlight RTSP discovery)
#   47989/tcp → 47999  (Moonlight HTTP)
#   47990/tcp → 48000  (Sunshine HTTPS web UI + API)
#   47998/udp → 48008  (Sunshine video stream)
#   47999/udp → 48009  (Sunshine control)
#   48000/udp → 48010  (Sunshine audio)
#   48010/tcp → 48020  (Sunshine control TCP)
EXPOSE 47984/tcp 47989/tcp 47990/tcp \
       47998/udp 47999/udp 48000/udp 48010/tcp

# Sunshine config + paired-client DB live here; mount for persistence.
VOLUME ["/config"]

ENTRYPOINT ["/entrypoint.sh"]
