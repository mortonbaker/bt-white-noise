#!/usr/bin/env bash
# Fresh-Pi setup for the whitenoise Bose bridge.
#
# What it does:
#   - apt installs bluez + mpv + python3 + pipewire (most already on Pi OS)
#   - creates ~/white-noise/files (audio library directory)
#   - installs the two systemd --user units
#   - enables user lingering so services run without an active SSH session
#   - starts the portal
#
# What it does NOT do:
#   - pair your Bose (do that in the web portal after first boot)
#   - download audio (drop MP3s into ~/white-noise/files/ or use portal)
#   - set up the yt-dlp proxy host (optional; see README)

set -euo pipefail

cd "$(dirname "$0")"
HERE="$(pwd)"

echo "==> Installing dependencies..."
sudo apt-get update -qq
sudo apt-get install -y bluez mpv python3 pipewire pipewire-pulse wireplumber python3-pipx

mkdir -p "$HOME/white-noise/files"
mkdir -p "$HOME/.config/systemd/user"

echo "==> Installing portal + service files..."
cp "$HERE/whitenoise-portal.py" "$HOME/whitenoise-portal.py"
chmod +x "$HOME/whitenoise-portal.py"
cp "$HERE/whitenoise-portal.service" "$HOME/.config/systemd/user/whitenoise-portal.service"
cp "$HERE/white-noise.service" "$HOME/.config/systemd/user/white-noise.service"

echo "==> Reloading systemd + enabling services..."
systemctl --user daemon-reload
systemctl --user enable --now whitenoise-portal.service

echo "==> Enabling user lingering (services keep running without SSH)..."
sudo loginctl enable-linger "$USER" || true

# Friendliest URL we can print
HOSTLINE="$(hostname)"
IPLINE="$(hostname -I | awk '{print $1}')"

echo
echo "==============================================================="
echo "Done. Portal is running."
echo
echo "  http://${IPLINE}:8080/"
echo "  http://${HOSTLINE}.local:8080/  (if mDNS works on your LAN)"
echo
echo "Next steps:"
echo "  1. Drop an MP3 in ~/white-noise/files/  (or paste a YouTube URL in"
echo "     the portal — requires PROXY_HOST configured, see README)."
echo "  2. Put your Bose speaker in pairing mode and click '⇆ Scan + Pair'"
echo "     in the portal."
echo "  3. Click '▶ Play' once the BT status shows CONNECTED."
echo "==============================================================="
