#!/usr/bin/env python3
"""PCM streamer for ESP32 → Bose bridge.

Serves an ffmpeg-transcoded PCM stream of ~/white-noise/current.mp3 at
http://atlas01:8081/pcm. Loops forever via ffmpeg -stream_loop -1.

Format: 44.1 kHz, 16-bit signed little-endian, stereo. Bitrate ~1.4 Mbps.

GET /         — small status page
GET /pcm      — endless PCM stream (audio/L16)
GET /info     — JSON with file/state info
"""
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AUDIO_FILE = os.environ.get("AUDIO_FILE", os.path.expanduser("~/white-noise/current.mp3"))
PORT = int(os.environ.get("PORT", "8081"))

# Active stream processes for cleanup
active_streams: set[subprocess.Popen] = set()
streams_lock = threading.Lock()


def make_ffmpeg(audio_file: str) -> subprocess.Popen:
    # Output 22.05 kHz mono s16le — 4x less WiFi traffic to the ESP32 than
    # 44.1 kHz stereo. ESP32 upsamples in firmware (nearest-neighbor, which
    # is fine for noise / rain content).
    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-re",
        "-stream_loop", "-1",
        "-i", audio_file,
        "-f", "s16le",
        "-ar", "22050",
        "-ac", "1",
        "-",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Quiet — systemd journal captures stderr only on errors
        if "200" not in (args[1] if len(args) > 1 else ""):
            sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def do_GET(self):
        if self.path.startswith("/pcm"):
            return self.serve_pcm()
        if self.path == "/info":
            return self.serve_info()
        if self.path == "/":
            return self.serve_root()
        self.send_response(404)
        self.end_headers()

    def serve_root(self):
        body = (
            f"PCM streamer\n"
            f"file: {AUDIO_FILE}\n"
            f"PCM endpoint: GET /pcm  (44.1kHz s16le stereo)\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_info(self):
        try:
            sz = os.path.getsize(AUDIO_FILE)
        except OSError:
            sz = -1
        body = json.dumps({
            "file": AUDIO_FILE,
            "size_bytes": sz,
            "sample_rate": 22050,
            "channels": 1,
            "bits": 16,
            "bytes_per_sec": 22050 * 1 * 2,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_pcm(self):
        if not os.path.exists(AUDIO_FILE):
            self.send_response(503)
            self.end_headers()
            return
        proc = make_ffmpeg(AUDIO_FILE)
        with streams_lock:
            active_streams.add(proc)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "audio/L16; rate=22050; channels=1")
            self.send_header("Cache-Control", "no-cache, no-store")
            # No Content-Length — streaming forever
            self.end_headers()
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try: proc.kill()
                except Exception: pass
            with streams_lock:
                active_streams.discard(proc)


def cleanup(*_):
    with streams_lock:
        for p in list(active_streams):
            try: p.terminate()
            except Exception: pass
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"PCM streamer on :{PORT}  file={AUDIO_FILE}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
