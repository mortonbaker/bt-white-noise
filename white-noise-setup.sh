#!/usr/bin/env bash
# One-shot installer for the white-noise audio source on atlas01.
# - Installs yt-dlp + ffmpeg if missing
# - Downloads a YouTube white-noise video and transcodes to a Bose-friendly MP3
# - Sets up a systemd user service that serves the MP3 over HTTP on :8080
# - Idempotent: re-running just refreshes the file
#
# Usage:
#   ./white-noise-setup.sh "https://www.youtube.com/watch?v=<id>"
#
# The Bose ESP32 firmware pulls from http://192.168.0.143:8080/current.mp3

set -euo pipefail

YT_URL="${1:-}"
SHARE_DIR="$HOME/white-noise"
SERVE_PORT="8080"

if [ -z "$YT_URL" ]; then
  echo "Usage: $0 <youtube-url>"
  echo "Example: $0 'https://www.youtube.com/watch?v=nMfPqeZjc2c'  # 10hr brown noise"
  exit 1
fi

mkdir -p "$SHARE_DIR"

# 1. Install yt-dlp + ffmpeg if needed
if ! command -v yt-dlp >/dev/null 2>&1; then
  echo "Installing yt-dlp via pipx..."
  pipx install yt-dlp
  export PATH="$HOME/.local/bin:$PATH"
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "Installing ffmpeg via apt..."
  sudo apt-get update -qq
  sudo apt-get install -y ffmpeg
fi

# 2. Download YouTube audio → MP3 @ 44.1kHz stereo (Bose-friendly, ESP32-friendly)
TMP="$(mktemp -d)"
trap "rm -rf $TMP" EXIT

echo "Downloading audio from YouTube..."
"$HOME/.local/bin/yt-dlp" \
  -f 'bestaudio' \
  -x --audio-format mp3 \
  --audio-quality 128K \
  --postprocessor-args "ffmpeg:-ar 44100 -ac 2" \
  -o "$TMP/source.%(ext)s" \
  "$YT_URL"

# Find the resulting file (yt-dlp picks the extension)
DOWNLOADED="$(find "$TMP" -name 'source.*' | head -1)"
if [ -z "$DOWNLOADED" ]; then
  echo "ERROR: yt-dlp didn't produce an output file"
  exit 1
fi
echo "Got: $DOWNLOADED  ($(du -h "$DOWNLOADED" | cut -f1))"

# 3. Move to share dir as current.mp3 (atomic via mv)
mv "$DOWNLOADED" "$SHARE_DIR/current.mp3.new"
mv "$SHARE_DIR/current.mp3.new" "$SHARE_DIR/current.mp3"
echo "Installed: $SHARE_DIR/current.mp3"

# 4. Set up systemd user service for the HTTP server
mkdir -p "$HOME/.config/systemd/user"
SERVICE_FILE="$HOME/.config/systemd/user/white-noise-http.service"
cat > "$SERVICE_FILE" << EOF
[Unit]
Description=White noise HTTP server (serves $SHARE_DIR on :$SERVE_PORT)
After=network.target

[Service]
Type=simple
WorkingDirectory=$SHARE_DIR
ExecStart=/usr/bin/python3 -m http.server $SERVE_PORT --bind 0.0.0.0
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

# 5. Enable + (re)start the service
systemctl --user daemon-reload
systemctl --user enable --now white-noise-http.service
sleep 1

# 6. Sanity check
if curl -sI "http://localhost:$SERVE_PORT/current.mp3" | grep -q "200 OK"; then
  echo
  echo "DONE. White noise is being served at:"
  echo "  http://192.168.0.143:$SERVE_PORT/current.mp3"
  echo
  echo "The ESP32 firmware will pull from this URL."
else
  echo "WARNING: HTTP server didn't return 200. Check:"
  echo "  journalctl --user -u white-noise-http.service -n 30"
fi

# 7. Make sure the service survives reboots WITHOUT the user being logged in
sudo loginctl enable-linger "$USER" 2>/dev/null || true
echo
echo "User lingering enabled — service will run on boot even without SSH session."
