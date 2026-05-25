#!/usr/bin/env python3
"""Pull /log from the bt-white-noise ESP32 and append new lines to ~/white-noise-esp.log.

Run as a systemd user service. Resilient to ESP32 reboots and network blips.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

ESP_URL = os.environ.get("ESP_URL", "http://192.168.0.195")
LOG_FILE = os.environ.get("LOG_FILE", os.path.expanduser("~/white-noise-esp.log"))

since = 0
last_warn = 0


def stamp():
    return datetime.now().isoformat(timespec="seconds")


def warn_once(msg):
    global last_warn
    if time.time() - last_warn > 60:
        with open(LOG_FILE, "a") as f:
            f.write(f"{stamp()}  # puller: {msg}\n")
        last_warn = time.time()


def main():
    global since
    with open(LOG_FILE, "a") as f:
        f.write(f"{stamp()}  # puller starting, target={ESP_URL} -> {LOG_FILE}\n")
    consecutive_resets = 0
    while True:
        try:
            req = urllib.request.Request(f"{ESP_URL}/log?since={since}")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            warn_once(f"unreachable: {e}")
            time.sleep(3)
            continue
        new_seq = data.get("seq", since)
        lines = data.get("lines", [])
        # ESP32 reboot detection: seq went backwards
        if new_seq < since:
            with open(LOG_FILE, "a") as f:
                f.write(f"{stamp()}  # puller: ESP32 rebooted (seq {since} -> {new_seq})\n")
            since = 0
            continue
        if lines:
            with open(LOG_FILE, "a") as f:
                for line in lines:
                    f.write(f"{stamp()}  {line}\n")
        since = new_seq
        time.sleep(1)


if __name__ == "__main__":
    main()
