#!/usr/bin/env python3
"""whitenoise portal — web control panel for the Bose SoundLink Mini bridge.

Hosts an HTTP UI on :8080 with status, pair/connect/disconnect, play/stop, volume,
and a tail of the white-noise.service journal.

Run as a systemd user service. Shells out to bluetoothctl + wpctl + systemctl.
"""

import json
import os
import re
import shlex
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BOSE_MAC = os.environ.get("BOSE_MAC", "60:AB:D2:35:9C:85")
BOSE_NAME_HINT = "Bose"  # name substring fallback if MAC not seen
PORT = int(os.environ.get("PORT", "8080"))
SERVICE = os.environ.get("SERVICE", "white-noise")  # systemd user unit
LIBRARY_DIR = os.path.expanduser("~/white-noise/files")
CURRENT_LINK = os.path.expanduser("~/white-noise/current.mp3")

# Long-running task tracking (e.g. /pair scans for ~10s in a background thread)
running_tasks: dict[str, dict] = {}
tasks_lock = threading.Lock()


def sh(cmd: str, timeout: int = 15) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(cmd, 124, stdout=e.stdout or "", stderr=str(e))


def bt_paired() -> bool:
    r = sh("bluetoothctl devices Paired")
    return BOSE_MAC.lower() in (r.stdout or "").lower()


def bt_connected() -> bool:
    r = sh(f"bluetoothctl info {BOSE_MAC}")
    return "Connected: yes" in (r.stdout or "")


def bt_rssi():
    r = sh(f"bluetoothctl info {BOSE_MAC}")
    m = re.search(r"RSSI: (-?\d+)", r.stdout or "")
    return int(m.group(1)) if m else None


def get_volume_pct():
    r = sh("wpctl get-volume @DEFAULT_AUDIO_SINK@")
    m = re.search(r"Volume:\s*([\d.]+)", r.stdout or "")
    return int(float(m.group(1)) * 100) if m else None


def set_volume_pct(v: int):
    v = max(0, min(100, int(v)))
    sh(f"wpctl set-volume @DEFAULT_AUDIO_SINK@ {v/100:.2f}")


def service_active() -> bool:
    r = sh(f"systemctl --user is-active {SERVICE}")
    return (r.stdout or "").strip() == "active"


AUDIO_EXTS = (".mp3", ".opus", ".webm", ".m4a", ".ogg", ".flac", ".wav", ".aac")


def list_tracks() -> list[dict]:
    if not os.path.isdir(LIBRARY_DIR):
        return []
    out = []
    for name in sorted(os.listdir(LIBRARY_DIR)):
        if not name.lower().endswith(AUDIO_EXTS):
            continue
        path = os.path.join(LIBRARY_DIR, name)
        try:
            sz = os.path.getsize(path)
        except OSError:
            continue
        # Friendly title: strip the trailing _<YT-id>.<ext>
        title = re.sub(r"_[A-Za-z0-9_-]{11}\.[a-z0-9]+$", "", name, flags=re.IGNORECASE)
        # Final cleanup: trim residual extension if no YT id matched, replace _
        for e in AUDIO_EXTS:
            if title.lower().endswith(e):
                title = title[: -len(e)]
                break
        title = title.replace("_", " ").strip() or name
        out.append({"file": name, "title": title, "size_bytes": sz})
    return out


def current_track() -> str | None:
    try:
        target = os.readlink(CURRENT_LINK)
        return os.path.basename(target)
    except OSError:
        return None


def switch_track(name: str) -> bool:
    # Validate against library to prevent path traversal
    if "/" in name or ".." in name or not name.lower().endswith(AUDIO_EXTS):
        return False
    target = os.path.join(LIBRARY_DIR, name)
    if not os.path.isfile(target):
        return False
    # Atomic symlink swap
    tmp = CURRENT_LINK + ".tmp"
    try:
        if os.path.lexists(tmp):
            os.remove(tmp)
        os.symlink(target, tmp)
        os.replace(tmp, CURRENT_LINK)
    except OSError:
        return False
    # Restart service so mpv picks up the new file
    sh(f"systemctl --user restart {SERVICE}")
    return True


def default_sink() -> str:
    r = sh("wpctl status")
    out = r.stdout or ""
    # Find the default sink line — it has '*' marker
    for line in out.splitlines():
        if "*" in line and "." in line and ("Audio" in line or "Sink" in line or True):
            # crude — return any '*' line
            return line.strip()
    return ""


def status() -> dict:
    sink = ""
    r = sh("wpctl status")
    lines = (r.stdout or "").splitlines()
    in_sinks = False
    for ln in lines:
        if "Sinks:" in ln:
            in_sinks = True
            continue
        if in_sinks:
            if ln.strip() == "":
                break
            if "*" in ln:
                sink = ln.strip()
                break
    with tasks_lock:
        task_states = {k: v for k, v in running_tasks.items()}
    return {
        "bt_paired": bt_paired(),
        "bt_connected": bt_connected(),
        "bt_rssi": bt_rssi(),
        "bose_mac": BOSE_MAC,
        "volume": get_volume_pct(),
        "playing": service_active(),
        "default_sink": sink,
        "tasks": task_states,
        "tracks": list_tracks(),
        "current_track": current_track(),
    }


# ---------- background pair task ----------
def pair_flow():
    log = []
    def step(msg, cmd=None):
        if cmd:
            r = sh(cmd, timeout=20)
            log.append(f"$ {cmd}")
            if r.stdout: log.append(r.stdout.rstrip())
            if r.stderr: log.append(f"stderr: {r.stderr.rstrip()}")
            log.append(f"exit {r.returncode}")
        else:
            log.append(msg)
        with tasks_lock:
            running_tasks["pair"] = {"state": "running", "log": list(log)}
    step("starting pair flow")
    # Make sure agent + scan are armed
    step(None, "bluetoothctl power on")
    step(None, "bluetoothctl agent on")
    step(None, "bluetoothctl default-agent")
    # Short scan to catch the Bose in pairing mode
    step(None, "bluetoothctl --timeout 12 scan on")
    step(None, f"bluetoothctl pair {BOSE_MAC}")
    step(None, f"bluetoothctl trust {BOSE_MAC}")
    step(None, f"bluetoothctl connect {BOSE_MAC}")
    with tasks_lock:
        running_tasks["pair"] = {"state": "done", "log": list(log)}


def kick_pair():
    with tasks_lock:
        cur = running_tasks.get("pair")
        if cur and cur.get("state") == "running":
            return False
        running_tasks["pair"] = {"state": "running", "log": []}
    threading.Thread(target=pair_flow, daemon=True).start()
    return True


# ---------- background download task ----------
def sanitize_filename(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_\- ]+", "", s)
    s = re.sub(r"\s+", "_", s.strip())
    return s[:60] or "track"


PROXY_HOST = os.environ.get("PROXY_HOST", "morton@atlas01.local")
PROXY_TMP = "/tmp/wn-dl"


def ssh_call(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a remote command via SSH to the download proxy.

    Uses bash -lc so the remote sources ~/.profile and picks up pipx's
    ~/.local/bin on PATH (where yt-dlp lives on atlas01). The whole
    bash -lc argument is passed as a single ssh arg so ssh's word-joining
    doesn't break the command boundaries."""
    inner = " ".join(shlex.quote(x) for x in cmd)
    remote = f"bash -lc {shlex.quote(inner)}"
    full = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", PROXY_HOST, remote]
    return subprocess.run(full, capture_output=True, text=True, **kw)


def download_flow(url: str):
    """YouTube downloads are bot-blocked on the Pi. Proxy through atlas01:
    SSH atlas01 → yt-dlp into /tmp → scp file back → cleanup remote."""
    log: list[str] = []

    def append(line: str):
        log.append(line)
        with tasks_lock:
            running_tasks["download"] = {"state": "running", "log": list(log), "url": url}

    append(f"== download requested: {url} ==")
    append(f"proxy: {PROXY_HOST}")

    # Extract video ID for filename uniqueness
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})", url)
    vid = m.group(1) if m else "ext"

    # Get title via proxy
    try:
        tp = ssh_call(["yt-dlp", "--print", "%(title)s", "--no-warnings", "--skip-download", url], timeout=30)
        title = (tp.stdout or "").strip().splitlines()[0] if tp.stdout else ""
        if tp.returncode != 0 and tp.stderr:
            append(f"title fetch stderr: {tp.stderr.strip()[:200]}")
    except Exception as e:
        title = ""
        append(f"title lookup failed: {e}")

    safe = sanitize_filename(title) if title else vid
    # No extension yet — yt-dlp will pick one (.opus/.m4a/.webm) based on source.
    # We skip the slow MP3 re-encode; mpv plays anything.
    safe_base = f"{safe}_{vid}"

    if title:
        append(f"title: {title}")
    append(f"target base: {safe_base}.*")

    # Dedupe by YouTube video ID — any existing file matching ..._<vid>.<ext>
    if vid != "ext" and os.path.isdir(LIBRARY_DIR):
        pattern = f"_{vid.lower()}."
        existing = [f for f in os.listdir(LIBRARY_DIR)
                    if f.lower().endswith(AUDIO_EXTS) and pattern in f.lower()]
        if existing:
            append(f"video {vid} already in library as: {existing[0]}")
            with tasks_lock:
                running_tasks["download"] = {"state": "done", "log": list(log), "file": existing[0]}
            return

    # Step 1: download on the proxy in native format (skip mp3 transcode)
    # yt-dlp picks the extension from the source codec.
    remote_outtmpl = f"{PROXY_TMP}/{safe_base}.%(ext)s"
    append("starting yt-dlp on proxy (no transcode)...")
    remote_cmd = (
        f"mkdir -p {PROXY_TMP} && yt-dlp -f bestaudio -o {shlex.quote(remote_outtmpl)} "
        f"--no-warnings --newline {shlex.quote(url)} "
        # Print the final filename on stdout for us to parse:
        f"--print after_move:filepath"
    )
    ssh_remote = f"bash -lc {shlex.quote(remote_cmd)}"
    proc = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", PROXY_HOST, ssh_remote],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    assert proc.stdout is not None
    # The last non-empty line that looks like an absolute path is the file
    final_remote_path = ""
    for line in proc.stdout:
        line = line.rstrip()
        append(line)
        if line.startswith(PROXY_TMP + "/"):
            final_remote_path = line
    proc.wait()
    if proc.returncode != 0:
        append(f"== yt-dlp failed on proxy (exit {proc.returncode}) ==")
        with tasks_lock:
            running_tasks["download"] = {"state": "failed", "log": list(log)}
        return

    # Fallback: if --print didn't show a path, glob for it
    if not final_remote_path:
        ls = ssh_call(["ls", "-1"] + [f"{PROXY_TMP}/{safe_base}.{e[1:]}" for e in AUDIO_EXTS], timeout=10)
        for line in (ls.stdout or "").splitlines():
            if line.strip().startswith(PROXY_TMP):
                final_remote_path = line.strip()
                break
    if not final_remote_path:
        append("== couldn't determine downloaded filename ==")
        with tasks_lock:
            running_tasks["download"] = {"state": "failed", "log": list(log)}
        return

    out_name = os.path.basename(final_remote_path)
    out_path = os.path.join(LIBRARY_DIR, out_name)

    # Step 2: scp to library
    append(f"transferring {out_name} from proxy...")
    scp = subprocess.run(
        ["scp", "-o", "BatchMode=yes", f"{PROXY_HOST}:{final_remote_path}", out_path],
        capture_output=True, text=True, timeout=180,
    )
    if scp.returncode != 0:
        append(f"scp failed: {scp.stderr.strip()[:300]}")
        with tasks_lock:
            running_tasks["download"] = {"state": "failed", "log": list(log)}
        return

    # Step 3: cleanup remote
    ssh_call(["rm", "-f", final_remote_path], timeout=10)

    if os.path.exists(out_path):
        append(f"== complete ({os.path.getsize(out_path)//1024//1024} MB) ==")
        with tasks_lock:
            running_tasks["download"] = {"state": "done", "log": list(log), "file": out_name}
    else:
        append("== file missing after scp ==")
        with tasks_lock:
            running_tasks["download"] = {"state": "failed", "log": list(log)}


def kick_download(url: str) -> bool:
    if not url.startswith("http"):
        return False
    with tasks_lock:
        cur = running_tasks.get("download")
        if cur and cur.get("state") == "running":
            return False
        running_tasks["download"] = {"state": "running", "log": [], "url": url}
    threading.Thread(target=download_flow, args=(url,), daemon=True).start()
    return True


# ---------- HTML ----------
HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>whitenoise</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font-family:system-ui;max-width:640px;margin:18px auto;padding:0 14px;background:#0f0f10;color:#eee}
h1{font-weight:300;margin:6px 0 14px}
button{display:block;width:100%;padding:14px;margin:6px 0;font-size:16px;border:1px solid #444;background:#222;color:#eee;border-radius:8px;cursor:pointer}
button:active{transform:scale(0.99)}
.go{background:#2c5}.stop{background:#a33}.pair{background:#36c}.warn{background:#a73}
.row{display:grid;gap:6px}
.row2{grid-template-columns:1fr 1fr}
.row3{grid-template-columns:1fr 1fr 1fr}
.row button{margin:0}
.stat{padding:12px;background:#1a1a1a;border-radius:8px;margin:12px 0;display:grid;grid-template-columns:auto 1fr;gap:6px 16px;align-items:center}
.k{color:#888;font-size:13px}
.v{font-size:15px}
.ok{color:#5c5}.bad{color:#c55}.dim{color:#888}
.spin{display:inline-block;width:12px;height:12px;border:2px solid #36c;border-top:2px solid transparent;border-radius:50%;animation:r 1s linear infinite;margin-right:6px;vertical-align:-2px}
@keyframes r{to{transform:rotate(360deg)}}
.sect{color:#888;font-size:13px;margin-top:14px;margin-bottom:4px}
pre{background:#000;color:#9c9;font-family:ui-monospace,monospace;font-size:11px;padding:8px;border-radius:6px;max-height:300px;overflow-y:auto;white-space:pre-wrap;margin-top:8px;border:1px solid #333}
.muted{color:#666;font-size:12px;margin-top:4px}
</style></head>
<body>
<h1>🌧️ whitenoise</h1>
<div class="stat">
  <div class="k">Bluetooth</div><div class="v" id="bt">…</div>
  <div class="k">Bose</div><div class="v" id="paired">…</div>
  <div class="k">Playing</div><div class="v" id="playing">…</div>
  <div class="k">Volume</div><div class="v" id="vol">…</div>
  <div class="k">Default sink</div><div class="v dim" id="sink" style="font-size:11px;word-break:break-word">…</div>
</div>

<button class="pair" onclick="p('/pair')">⇆ Scan + Pair (Bose must be in pairing mode)</button>
<div class="row row2">
  <button onclick="p('/connect')">Connect</button>
  <button onclick="p('/disconnect')">Disconnect</button>
</div>

<div class="sect">Track library</div>
<div id="tracks"></div>
<div class="muted" id="current-track-line"></div>

<div class="sect">Add new track from YouTube</div>
<div style="display:flex;gap:6px">
  <input id="yt-url" placeholder="https://www.youtube.com/watch?v=..." style="flex:1;padding:14px;background:#222;color:#eee;border:1px solid #444;border-radius:8px;font-size:15px">
  <button onclick="addTrack()" style="flex:0 0 auto;width:auto;padding:14px 18px;margin:0">📥 Add</button>
</div>
<pre id="download-log" style="display:none">(idle)</pre>

<div class="sect">Playback</div>
<div class="row row2">
  <button class="go" onclick="p('/play')">▶ Play</button>
  <button class="stop" onclick="p('/stop')">■ Stop</button>
</div>

<div class="sect">Volume</div>
<div class="row row3">
  <button onclick="setVol(40)">40</button>
  <button onclick="setVol(70)">70</button>
  <button onclick="setVol(100)">100</button>
</div>

<div class="sect">Pair task log</div>
<pre id="pair-log">(idle)</pre>

<div class="sect">white-noise.service journal (last 30)</div>
<pre id="journal">…</pre>
<div class="muted">Bose MAC: <span id="mac"></span></div>

<script>
async function p(url){
  document.body.style.opacity=0.6;
  try{ await fetch(url,{method:'POST'}); }catch(e){}
  document.body.style.opacity=1;
  refresh(); tailJournal();
}
async function setVol(v){
  try{ await fetch('/volume?v='+v,{method:'POST'}); }catch(e){}
  refresh();
}
async function setTrack(name){
  document.body.style.opacity=0.6;
  try{ await fetch('/track?name='+encodeURIComponent(name),{method:'POST'}); }catch(e){}
  document.body.style.opacity=1;
  refresh(); tailJournal();
}
async function addTrack(){
  const el = document.getElementById('yt-url');
  const url = el.value.trim();
  if (!url) return;
  try{
    const r = await fetch('/add-track?url='+encodeURIComponent(url),{method:'POST'});
    if (r.ok) el.value = '';
  }catch(e){}
  refresh();
}
async function refresh(){
  try{
    const r=await(await fetch('/status.json')).json();
    const btEl=document.getElementById('bt');
    const connecting = r.tasks && r.tasks.pair && r.tasks.pair.state==='running';
    let label = r.bt_connected ? 'CONNECTED' : (connecting ? 'PAIRING…' : 'DISCONNECTED');
    btEl.innerHTML = (connecting ? '<span class="spin"></span>' : '') + label;
    btEl.className = 'v ' + (r.bt_connected ? 'ok' : (connecting ? '' : 'bad'));
    document.getElementById('paired').textContent = r.bt_paired
      ? ('paired' + (r.bt_rssi!=null ? ' · '+r.bt_rssi+' dBm' : ''))
      : 'not paired';
    document.getElementById('paired').className = 'v ' + (r.bt_paired ? 'ok' : 'dim');
    document.getElementById('playing').textContent = r.playing ? 'on' : 'off';
    document.getElementById('playing').className = 'v ' + (r.playing ? 'ok' : 'dim');
    document.getElementById('vol').textContent = (r.volume==null ? '—' : (r.volume+'%'));
    document.getElementById('sink').textContent = r.default_sink || '(no sink available)';
    document.getElementById('mac').textContent = r.bose_mac;
    // Render tracks list
    const tEl = document.getElementById('tracks');
    const tracks = r.tracks || [];
    if (tracks.length === 0) {
      tEl.innerHTML = '<div class="muted">(no tracks in ~/white-noise/files/)</div>';
    } else {
      tEl.innerHTML = tracks.map(t => {
        const active = t.file === r.current_track;
        const mb = Math.round(t.size_bytes / 1024 / 1024);
        return '<button onclick="setTrack(\''+t.file.replace(/'/g,"\\'")+'\')" style="'+(active?'background:#28a;border-color:#48c':'')+'">' +
          (active ? '▶ ' : '') + t.title + ' <span class="dim" style="font-size:11px">('+mb+'MB)</span></button>';
      }).join('');
    }
    document.getElementById('current-track-line').textContent = r.current_track ? 'Active: ' + r.current_track : '(no track selected)';
    if (r.tasks && r.tasks.pair){
      const pl = document.getElementById('pair-log');
      pl.textContent = (r.tasks.pair.log || []).join('\n') || ('('+r.tasks.pair.state+')');
    }
    if (r.tasks && r.tasks.download){
      const dl = document.getElementById('download-log');
      dl.style.display = 'block';
      dl.textContent = (r.tasks.download.log || []).join('\n') || ('('+r.tasks.download.state+')');
    }
  }catch(e){}
}
async function tailJournal(){
  try{
    const t=await(await fetch('/log')).text();
    document.getElementById('journal').textContent = t || '(no entries)';
  }catch(e){}
}
refresh(); tailJournal();
setInterval(refresh, 1000);
setInterval(tailJournal, 3000);
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence per-request log

    def _send(self, code, mime, body):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self._send(200, "text/html; charset=utf-8", HTML)
        if u.path == "/status.json":
            return self._send(200, "application/json", json.dumps(status()))
        if u.path == "/log":
            r = sh(f"journalctl --user -u {SERVICE} -n 30 --no-pager")
            return self._send(200, "text/plain; charset=utf-8", r.stdout or "")
        self._send(404, "text/plain", "not found")

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/pair":
            kicked = kick_pair()
            return self._send(200, "text/plain", "kicked" if kicked else "already running")
        if u.path == "/connect":
            sh(f"bluetoothctl connect {BOSE_MAC}")
            return self._send(200, "text/plain", "ok")
        if u.path == "/disconnect":
            sh(f"bluetoothctl disconnect {BOSE_MAC}")
            return self._send(200, "text/plain", "ok")
        if u.path == "/play":
            sh(f"systemctl --user start {SERVICE}")
            return self._send(200, "text/plain", "ok")
        if u.path == "/stop":
            sh(f"systemctl --user stop {SERVICE}")
            return self._send(200, "text/plain", "ok")
        if u.path == "/volume":
            v = q.get("v", ["70"])[0]
            set_volume_pct(int(v))
            return self._send(200, "text/plain", "ok")
        if u.path == "/track":
            name = q.get("name", [""])[0]
            ok = switch_track(name)
            return self._send(200 if ok else 400, "text/plain", "ok" if ok else "invalid track")
        if u.path == "/add-track":
            url = q.get("url", [""])[0]
            ok = kick_download(url)
            return self._send(200 if ok else 409, "text/plain", "kicked" if ok else "busy or bad url")
        if u.path == "/delete-track":
            name = q.get("name", [""])[0]
            if "/" in name or ".." in name or not name.lower().endswith(".mp3"):
                return self._send(400, "text/plain", "bad name")
            target = os.path.join(LIBRARY_DIR, name)
            cur = current_track()
            if name == cur:
                return self._send(409, "text/plain", "track is currently active")
            try:
                os.remove(target)
                return self._send(200, "text/plain", "deleted")
            except OSError as e:
                return self._send(500, "text/plain", f"delete failed: {e}")
        self._send(404, "text/plain", "not found")


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"whitenoise portal listening on :{PORT}  (Bose target {BOSE_MAC})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
