#!/usr/bin/env bash
# Swap the currently-playing audio. Downloads a new YouTube video and atomically
# replaces ~/white-noise/current.mp3. The ESP32 picks it up on its next loop
# (or after a power cycle / restart via HA).
#
# Usage:
#   ./swap-white-noise.sh "https://www.youtube.com/watch?v=<id>"

set -euo pipefail
YT_URL="${1:?Usage: $0 <youtube-url>}"

SHARE_DIR="$HOME/white-noise"
export PATH="$HOME/.local/bin:$PATH"

TMP="$(mktemp -d)"
trap "rm -rf $TMP" EXIT

echo "Downloading new audio..."
yt-dlp \
  -f 'bestaudio' \
  -x --audio-format mp3 \
  --audio-quality 128K \
  --postprocessor-args "ffmpeg:-ar 44100 -ac 2" \
  -o "$TMP/source.%(ext)s" \
  "$YT_URL"

NEW="$(find "$TMP" -name 'source.*' | head -1)"
if [ -z "$NEW" ] || [ ! -s "$NEW" ]; then
  echo "ERROR: download failed (no output)"
  exit 1
fi

# Snapshot previous for easy revert
if [ -f "$SHARE_DIR/current.mp3" ]; then
  cp "$SHARE_DIR/current.mp3" "$SHARE_DIR/previous.mp3"
fi

# Atomic replace
mv "$NEW" "$SHARE_DIR/current.mp3.new"
mv "$SHARE_DIR/current.mp3.new" "$SHARE_DIR/current.mp3"
echo "Swapped to: $SHARE_DIR/current.mp3  ($(du -h "$SHARE_DIR/current.mp3" | cut -f1))"
echo
echo "ESP32 will pick up the new file at the start of its next loop."
echo "To force immediate switch, restart the ESP32 (cycle USB power or trigger reset)."
echo "Previous file saved as $SHARE_DIR/previous.mp3 if you want to revert."
