# bt-white-noise

ESP32 firmware + Linux companion service that streams audio from an HTTP source to a
Bluetooth A2DP speaker (Bose SoundLink Mini and similar). Includes on-chip noise generators
as a fallback. Designed for "play this rainstorm forever to a wireless speaker the moment
it powers on" — built for baby white-noise.


> **Two implementations live in this repo:**
> - **`pi/`** — Raspberry Pi 5 + bluez + PipeWire + mpv + web portal. **This is the one that works.** See [pi/README.md](pi/README.md).
> - **`src/` (this directory)** — original ESP32 firmware. Pairs and plays on-chip generated noise reliably, but streaming network audio to it hits hard radio-coexistence and IRAM limits. Kept for documentation of what was tried and why we moved on.

## What it does (ESP32 version, original attempt)

```
   ┌─────────────────┐                ┌──────────────────┐                 ┌────────────────┐
   │   atlas01       │  HTTP PCM      │   ESP32 (any     │   A2DP / BT     │  Bose          │
   │   (Linux box)   │ ─────────────► │   classic-BT     │ ──────────────► │  SoundLink     │
   │                 │  44.1k s16le   │   variant)       │   2.4 GHz       │  Mini (or any  │
   │  ffmpeg loops   │  1.4 Mbps      │                  │                 │  A2DP sink)    │
   │  MP3 → PCM      │                │  Ring buffer +   │                 │                │
   │  forever        │                │  A2DP source     │                 │                │
   └─────────────────┘                └──────────────────┘                 └────────────────┘
                                              │
                                              │ HTTP :80 (web UI + REST control)
                                              ▼
                                       ┌────────────────┐
                                       │  Phone / laptop │
                                       │  control UI    │
                                       └────────────────┘
```

- **atlas01-side** Python service (`pcm-streamer.py`) wraps `ffmpeg -stream_loop -1` and serves a
  forever-looping raw PCM stream at `http://atlas01:8081/pcm` (44.1 kHz, s16le, stereo,
  ~1.4 Mbps). No MP3 decoder needed on the ESP32.
- **ESP32 firmware** (`src/main.cpp`) runs two FreeRTOS tasks:
  - Stream task (core 1): HTTP GET → ring buffer (32 KB)
  - A2DP callback (core 0): ring buffer → Bose
- **Web UI** on `http://<esp32-ip>/` for source/volume control, live log, BT pairing.
- **Audit log puller** (`esp-log-puller.py`) tails the ESP32's `/log` endpoint to
  `~/white-noise-esp.log` on atlas01 for off-device debugging.
- **On-chip noise generators** (brown / pink / white) as a fallback when you don't want
  to leave a Linux box running.

Why streaming-from-Linux instead of decoding MP3 on the ESP32: the audio-tools URLStream +
MP3DecoderHelix + A2DPSource path uses ~92% of the ESP32's IRAM and crashed in my testing
(reboot loop). Pre-decoding to PCM on a real CPU is dramatically more reliable.

## Hardware

- **ESP32 with BT Classic** — original ESP32 (ESP-WROOM-32) or ESP32-D0WD-V3. **NOT**
  ESP32-S2/S3/C3/C6/H2 — those have BLE only, no A2DP. Verify with
  `esptool chip-id` — output should say "Wi-Fi, BT" (BT Classic).
- Any Bluetooth speaker that accepts A2DP source connections.
- A Linux box with `ffmpeg` and Python 3 to host the PCM stream (Raspberry Pi works fine).

## Setup

### 1. Audio source (atlas01 / any Linux host)

```bash
# Install deps
sudo apt install ffmpeg python3
pipx install yt-dlp        # for downloading from YouTube

# Download an audio file (e.g. a rainstorm)
bash white-noise-setup.sh "https://www.youtube.com/watch?v=<id>"

# Start the PCM streamer as a systemd user service
cp systemd/pcm-streamer.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pcm-streamer.service

# Optional: enable lingering so the service runs without an SSH session
sudo loginctl enable-linger "$USER"
```

Verify: `curl -s http://<linux-host>:8081/info` should return JSON with the file path and
PCM format. `curl -s --max-time 2 http://<linux-host>:8081/pcm | wc -c` should produce
~350 KB.

To swap the audio source later: `bash swap-white-noise.sh "<new-youtube-url>"`. The
ESP32 picks up the new file on the next stream reconnect (or just restart it).

### 2. ESP32 firmware

```bash
git clone https://github.com/<you>/bt-white-noise
cd bt-white-noise
cp include/secrets.h.example include/secrets.h
# Edit include/secrets.h with your WiFi + Bose BT name + stream URL
nano include/secrets.h

# Install PlatformIO if you haven't already
pipx install platformio

# Build + flash
pio run                       # ~30s incremental (first time may take ~3-5 min for toolchain)
pio run -t upload             # flash via /dev/ttyUSB0
```

The firmware will:
1. Connect to WiFi
2. Start an HTTP server on port 80 (status page + control endpoints)
3. Start a stream task that reads PCM from `STREAM_URL`
4. Begin a BT inquiry scan, accepting any device whose name contains "Bose" or "SoundLink"
5. Auto-reconnect on power-up to a previously paired Bose

### 3. First-time BT pairing

Put the Bose into pairing mode (hold the Bluetooth button until the pairing tone), then
visit `http://<esp32-ip>/` and click "⇆ (Re)pair". You should see a `[match] ... ACCEPT`
line in the log followed by `[A2DP] CONNECTED`. Audio starts immediately.

## Web UI

Visit `http://<esp32-ip>/` to get:

- Current BT connection state with spinner during CONNECTING
- Audio source toggle (HTTP stream vs on-chip noise)
- Noise type selector (brown / pink / white) when on-chip
- Volume buttons (40 / 70 / 100)
- Play / Stop / Pair buttons
- **Live log panel** auto-refreshing every 700ms — includes every BT inquiry result,
  connection state change, and stream event
- Ring-buffer fill % and underrun counter for debugging stream health

## REST endpoints

| Method | Path                  | Description                                |
| ------ | --------------------- | ------------------------------------------ |
| GET    | `/`                   | HTML control page                          |
| GET    | `/status.json`        | Current state                              |
| GET    | `/log?since=N`        | New log lines since seq N                  |
| POST   | `/pair`               | Force BT inquiry restart                   |
| POST   | `/play`               | Start streaming audio to A2DP              |
| POST   | `/stop`               | Stop (sends silence to A2DP)               |
| POST   | `/source/stream`      | Use HTTP PCM source                        |
| POST   | `/source/noise`       | Use on-chip noise generator                |
| POST   | `/noise/brown`        | Switch noise type to brown                 |
| POST   | `/noise/white`        | Switch noise type to white                 |
| POST   | `/noise/pink`         | Switch noise type to pink                  |
| POST   | `/volume?v=70`        | Set BT volume 0-100                        |

Atlas01-side streamer (`pcm-streamer.py`):

| Method | Path     | Description                            |
| ------ | -------- | -------------------------------------- |
| GET    | `/`      | Plain-text status                      |
| GET    | `/info`  | JSON with file path + PCM format       |
| GET    | `/pcm`   | Endless PCM stream (audio/L16 stereo)  |

## File layout

```
bt-white-noise/
├── platformio.ini                       # PIO config, pioarduino platform 55.x
├── src/main.cpp                         # ESP32 firmware
├── include/secrets.h.example            # Template — copy to secrets.h, fill in
├── pcm-streamer.py                      # Linux: HTTP PCM stream from MP3
├── esp-log-puller.py                    # Linux: tail ESP32 /log -> file
├── white-noise-setup.sh                 # Linux: install yt-dlp/ffmpeg, download MP3, start HTTP server
├── swap-white-noise.sh                  # Linux: swap which YouTube URL is current
├── bt-flash.sh                          # Convenience wrapper for pio build+upload+monitor
├── systemd/
│   ├── pcm-streamer.service             # Linux user service for the PCM streamer
│   ├── esp-log-puller.service           # Linux user service for the audit log puller
│   └── white-noise-http.service         # (legacy) static HTTP server for MP3 file
└── README.md
```

## Troubleshooting

**ESP32 boot-loops with no panic trace and only "started." repeating in serial:**
Out of IRAM. Slim the lib_deps in `platformio.ini`. The v1 code that combined audio-tools
URLStream + MP3DecoderHelix + A2DPSource hit this — the v4 code separates concerns by
pre-decoding on the Linux side.

**`[match]` line shows your DirecTV box:** OUIs lie. The matcher in v3+ matches on
device-name substring (`Bose` / `SoundLink`), not MAC prefix. If you renamed your
SoundLink to something exotic, edit the `ssidMatcher` function.

**BT pairs but audio is jittery / dropping out:** Move the ESP32 closer to your WiFi AP.
WiFi and BT share the same 2.4 GHz radio on ESP32; weak WiFi causes BT to suffer too.
`WiFi.setSleep(false)` is already on.

**Ring buffer underruns climbing:** WiFi to the Linux streamer is too slow. Check
`/status.json` → `buf_pct`. Should stay above 30%. If not, the ESP32's WiFi isn't keeping
up. Move the ESP32 closer to the AP or check for WiFi congestion on 2.4 GHz.

**Brown noise on-chip sounds silent:** The Bose Mini's tiny driver filters out the
sub-100 Hz content where most brown-noise energy lives. Use pink instead, or stream a
real audio file.

## License

MIT. The pschatzmann ESP32-A2DP library used by the firmware is GPLv3 — building binaries
that include it makes the resulting binary GPL.

## Credits

- ESP32-A2DP: https://github.com/pschatzmann/ESP32-A2DP
- pioarduino platform: https://github.com/pioarduino/platform-espressif32
