#!/usr/bin/env bash
# Build + flash the bt-white-noise firmware to the ESP32 on /dev/ttyUSB0.
# Run from ~/Code/bt-white-noise on atlas01.

set -euo pipefail

if [ ! -f "include/secrets.h" ]; then
  echo "ERROR: include/secrets.h missing. Copy from the example and fill in:"
  echo "  cp include/secrets.h.example include/secrets.h"
  echo "  nano include/secrets.h"
  exit 1
fi

export PATH="$HOME/.local/bin:$PATH"

# First build can be slow (~3-5 min as PIO pulls toolchain + libs). Subsequent are ~30s.
echo "Building firmware..."
pio run

echo "Flashing /dev/ttyUSB0..."
pio run -t upload

echo
echo "Flashed. Monitoring serial output. Ctrl+C to exit."
echo "Look for 'starting A2DP' and 'started.' lines."
echo
pio device monitor --baud 115200
