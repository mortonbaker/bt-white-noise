# Raspberry Pi implementation

This is the path that actually works for "stream audio continuously to a Bose
SoundLink Mini and survive reboots." Built on a **Raspberry Pi 5** running
Debian 13 (Trixie). The ESP32 firmware in the parent directory was an
interesting experiment but ran into hard radio-coexistence and memory limits.

## What this gives you

- **Web portal at `http://<pi>:8080/`** — pair Bose, switch tracks, set
  volume, view live status, paste a YouTube URL to add new tracks.
- **Always-on mpv player** as a systemd user service, looping the active track
  forever.
- **PipeWire auto-routing** — when the Bose powers on, it becomes the default
  audio sink and mpv's output follows it. Power off → silence. Power on →
  audio resumes automatically.
- **YouTube downloads via SSH proxy** — YouTube bot-blocks the Pi's IP for
  yt-dlp, so the portal SSHes to a sibling host (atlas01) and pulls the file
  back via scp.

## Architecture

```
   ┌────────────────┐    ssh + scp     ┌────────────────────┐
   │   atlas01      │ ──────────────►  │  Pi 5 (whitenoise) │
   │ (any Linux box │  yt-dlp proxy    │                    │
   │  with yt-dlp)  │                  │  - whitenoise-     │
   └────────────────┘                  │    portal.py :8080 │
                                        │  - mpv loops file  │
                                        │  - bluez pairs Bose│
                                        │  - PipeWire routes │     A2DP / BT
                                        │                    │ ──────────────► Bose
                                        └────────────────────┘
```

## Files

| File | What |
| ---- | ---- |
| `whitenoise-portal.py` | Python web UI on :8080 — status, pair, tracks, volume, YT downloader |
| `white-noise.service` | systemd --user unit running mpv on the active track |
| `whitenoise-portal.service` | systemd --user unit running the portal |
| `setup.sh` | One-shot installer for a fresh Pi |

## Requirements

- Debian 12+ (tested on Debian 13 / Pi OS Bookworm-onwards)
- Raspberry Pi with built-in Bluetooth Classic (Pi 3 / 4 / 5 all fine; Pi Zero
  2 W also works but BT range is shorter)
- An audio MP3 file in `~/white-noise/files/`. The portal can pull more from
  YouTube via the proxy.

## Setup

```bash
# Clone the repo
git clone https://github.com/mortonbaker/bt-white-noise
cd bt-white-noise/pi

# Run the installer (installs bluez + mpv + sets up systemd user services)
bash setup.sh

# Drop at least one MP3 in ~/white-noise/files/
# (or download one via the portal once it's up)

# Open the portal
xdg-open http://localhost:8080/   # or visit from another device on the LAN
```

### YouTube proxy (optional)

If you want to download tracks via the portal's URL input, the Pi needs a
"download proxy" — a separate machine that can run `yt-dlp` (YouTube
bot-blocks Pis directly). Steps:

1. Pick any Linux box on your LAN (or somewhere reachable).
2. Install `yt-dlp` there: `pipx install yt-dlp`
3. Authorize the Pi's SSH pubkey on it (`ssh-copy-id user@host` from the Pi).
4. Set the env var when starting the portal: `PROXY_HOST=user@host.local`

Default proxy host is `morton@atlas01.local` — change it via the systemd
service file or `PROXY_HOST=` env var.

If you don't want the YouTube downloader feature, just don't set
`PROXY_HOST` and the URL input won't work, but everything else does.

## First-time Bluetooth pairing

1. Visit the portal at `http://<pi-ip>:8080/`.
2. Clear the Bose pairing list (hold the BT button on the speaker for ~10s
   until "device list cleared" voice prompt).
3. Put the Bose into pairing mode (short-press BT button, blue flashing).
4. Click **⇆ Scan + Pair** in the portal.
5. Watch the "Pair task log" — you should see scan results, then `Pairing
   successful` and `Connection successful`.
6. Click **▶ Play**. Audio starts.

The Bose is now trusted; powering it on later will auto-reconnect within ~5s.

## Configuration

Environment variables read by `whitenoise-portal.py`:

| Var | Default | Description |
| --- | ------- | ----------- |
| `BOSE_MAC` | `60:AB:D2:35:9C:85` | The MAC address of YOUR Bose. Override. |
| `PORT` | `8080` | HTTP port |
| `SERVICE` | `white-noise` | systemd unit name for mpv player |
| `PROXY_HOST` | `morton@atlas01.local` | yt-dlp proxy host (SSH target) |

Edit `whitenoise-portal.service` to set them via `Environment=` lines, or
edit the constants at the top of the .py file.

## REST endpoints

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/` | HTML control page |
| GET | `/status.json` | Full state — BT, volume, current track, tasks |
| GET | `/log` | Tail of the white-noise.service journal |
| POST | `/pair` | Run bluetoothctl scan+pair+trust+connect against `BOSE_MAC` |
| POST | `/connect` | bluetoothctl connect |
| POST | `/disconnect` | bluetoothctl disconnect |
| POST | `/play` | systemctl start mpv service |
| POST | `/stop` | systemctl stop mpv service |
| POST | `/volume?v=70` | wpctl set-volume @DEFAULT_AUDIO_SINK@ |
| POST | `/track?name=X.mp3` | Switch active track (atomic symlink swap + service restart) |
| POST | `/add-track?url=...` | Download a YouTube URL via SSH proxy |
| POST | `/delete-track?name=X.mp3` | Remove a file from the library |

## Why this works where the ESP32 didn't

The ESP32 implementation in this repo's parent directory ran into:

- IRAM exhaustion (audio-tools URLStream + MP3DecoderHelix + A2DPSource
  consumed ~92% of IRAM; reboot loops).
- Heap exhaustion after BT init (~44 KB free heap, task creation failed).
- 2.4 GHz radio contention: BT TX + WiFi RX shared the same antenna; HTTP UI
  became unreachable while streaming.

A Pi has separate WiFi/BT silicon (BCM43455 or similar), gigabytes of RAM,
and a real Linux audio stack (PipeWire). The hardware just isn't fighting
itself for the radio in the same way.
