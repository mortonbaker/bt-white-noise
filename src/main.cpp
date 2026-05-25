// ESP32 → Bose SoundLink white-noise bridge (v4)
//
// Sources: on-chip noise generators (brown/white/pink) + HTTP PCM stream from atlas01.
// Web UI with live log, name-only BT matcher.
//
// Endpoints:
//   GET  /              status page with buttons + live log
//   GET  /status.json   {bt, source, noise, streaming, volume, last_seq, buf_pct}
//   GET  /log?since=N   { seq, lines[] }
//   POST /pair          cancel + restart inquiry scan
//   POST /play          start streaming PCM into A2DP
//   POST /stop          stop streaming (silence into A2DP)
//   POST /source/{noise,stream}     switch audio source
//   POST /noise/{brown,white,pink}  switch noise generator type
//   POST /volume?v=70   BT volume 0-100

#include <WiFi.h>
#include <WebServer.h>
#include <esp_log.h>
#include <esp_gap_bt_api.h>
#include "BluetoothA2DPSource.h"
#include "secrets.h"

#ifndef STREAM_URL
#define STREAM_URL "http://192.168.0.143:8081/pcm"
#endif

WebServer http(80);
BluetoothA2DPSource a2dp;

// Forward decls
void ensureStreamTask();

enum NoiseType { BROWN, WHITE, PINK };
enum Source    { SRC_STREAM, SRC_NOISE };

volatile Source    currentSource = SRC_NOISE;    // default noise; stream lazily enabled
volatile NoiseType noiseMode = PINK;             // pink survives the speaker filter best
volatile bool      streaming = true;
volatile int       btVolume = 80;

// Static circular buffer for PCM bytes (BSS, no heap allocation)
// 8 KB = 2048 frames = ~46ms latency at 44.1 kHz stereo s16
static const size_t RB_BYTES = 8 * 1024;
static uint8_t pcmRing[RB_BYTES];
static volatile uint32_t pcmHead = 0;   // next write position
static volatile uint32_t pcmTail = 0;   // next read position
static portMUX_TYPE pcmMux = portMUX_INITIALIZER_UNLOCKED;
static volatile bool streamTaskStarted = false;

static inline size_t pcmUsed() {
  uint32_t h, t;
  portENTER_CRITICAL(&pcmMux);
  h = pcmHead; t = pcmTail;
  portEXIT_CRITICAL(&pcmMux);
  return (h - t) & (RB_BYTES - 1);
}
static inline size_t pcmFree() { return RB_BYTES - 1 - pcmUsed(); }

// Producer writes n bytes; returns bytes actually written (may be less if full)
static size_t pcmWrite(const uint8_t* src, size_t n) {
  size_t free = pcmFree();
  if (n > free) n = free;
  if (n == 0) return 0;
  uint32_t h = pcmHead;
  size_t first = RB_BYTES - (h % RB_BYTES);
  if (first > n) first = n;
  memcpy(&pcmRing[h % RB_BYTES], src, first);
  if (n > first) memcpy(&pcmRing[0], src + first, n - first);
  portENTER_CRITICAL(&pcmMux);
  pcmHead = (h + n) & (RB_BYTES * 2 - 1); // wrap at 2*RB_BYTES to keep head-tail math simple
  portEXIT_CRITICAL(&pcmMux);
  return n;
}

// Consumer reads up to n bytes
static size_t pcmRead(uint8_t* dst, size_t n) {
  size_t used = pcmUsed();
  if (n > used) n = used;
  if (n == 0) return 0;
  uint32_t t = pcmTail;
  size_t first = RB_BYTES - (t % RB_BYTES);
  if (first > n) first = n;
  memcpy(dst, &pcmRing[t % RB_BYTES], first);
  if (n > first) memcpy(dst + first, &pcmRing[0], n - first);
  portENTER_CRITICAL(&pcmMux);
  pcmTail = (t + n) & (RB_BYTES * 2 - 1);
  portEXIT_CRITICAL(&pcmMux);
  return n;
}

// --- ring-buffer log ----------------------------------------------------------
static const int LOG_LINES = 80;
static const int LOG_LINE_LEN = 160;
static char logRing[LOG_LINES][LOG_LINE_LEN];
static volatile uint32_t logSeq = 0;
static portMUX_TYPE logMux = portMUX_INITIALIZER_UNLOCKED;

static void logLine(const char* fmt, ...) {
  char buf[LOG_LINE_LEN];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  int n = strlen(buf);
  while (n > 0 && (buf[n-1] == '\n' || buf[n-1] == '\r')) buf[--n] = 0;
  portENTER_CRITICAL(&logMux);
  uint32_t s = logSeq++;
  strncpy(logRing[s % LOG_LINES], buf, LOG_LINE_LEN - 1);
  logRing[s % LOG_LINES][LOG_LINE_LEN - 1] = 0;
  portEXIT_CRITICAL(&logMux);
  Serial.println(buf);
}

static int customVprintf(const char* fmt, va_list args) {
  char buf[LOG_LINE_LEN];
  int n = vsnprintf(buf, sizeof(buf), fmt, args);
  Serial.write((const uint8_t*)buf, (n > 0 && n < (int)sizeof(buf)) ? n : strlen(buf));
  while (n > 0 && (buf[n-1] == '\n' || buf[n-1] == '\r')) buf[--n] = 0;
  if (n > 0) {
    portENTER_CRITICAL(&logMux);
    uint32_t s = logSeq++;
    strncpy(logRing[s % LOG_LINES], buf, LOG_LINE_LEN - 1);
    logRing[s % LOG_LINES][LOG_LINE_LEN - 1] = 0;
    portEXIT_CRITICAL(&logMux);
  }
  return n;
}

// --- noise generators --------------------------------------------------------
//
// White: uniform random ±full-scale.
// Pink:  Paul Kellet's IIR filter cascade — sounds far more "real" than Voss-
//        McCartney. Constants from www.firstpr.com.au/dsp/pink-noise/ .
// Brown: single-pole IIR low-pass on white noise, properly tuned. The leak
//        factor (0.997) sets the corner around 50 Hz at 44.1 kHz which gives
//        a real rumble that even a tiny Bose driver can reproduce some of.
//
// All return int16_t in range ±32767. ESP32 has a single-precision FPU so
// float math here is cheap.

static inline float whiteFloat() {
  // 32-bit random -> float in [-1.0, 1.0)
  uint32_t r = esp_random();
  int32_t s = (int32_t)r;
  return (float)s / 2147483648.0f;
}

static inline int16_t whiteSample() {
  return (int16_t)((int32_t)(esp_random() & 0xFFFF) - 0x8000);
}

static inline int16_t pinkSample() {
  static float b0 = 0, b1 = 0, b2 = 0, b3 = 0, b4 = 0, b5 = 0, b6 = 0;
  float w = whiteFloat();
  b0 = 0.99886f * b0 + w * 0.0555179f;
  b1 = 0.99332f * b1 + w * 0.0750759f;
  b2 = 0.96900f * b2 + w * 0.1538520f;
  b3 = 0.86650f * b3 + w * 0.3104856f;
  b4 = 0.55000f * b4 + w * 0.5329522f;
  b5 = -0.7616f * b5 - w * 0.0168980f;
  float pink = b0 + b1 + b2 + b3 + b4 + b5 + b6 + w * 0.5362f;
  b6 = w * 0.115926f;
  // Kellet sum is roughly ±3-4 at unity input; scale to ~0.5 of full-scale
  pink *= 0.11f;
  if (pink > 1.0f) pink = 1.0f;
  if (pink < -1.0f) pink = -1.0f;
  return (int16_t)(pink * 32767.0f);
}

static inline int16_t brownSample() {
  // One-pole low-pass: y[n] = a*y[n-1] + (1-a)*x[n]
  // a = 0.997 gives ~21 Hz corner at 44.1 kHz; combined with the scale,
  // produces a satisfying rumble without DC drift.
  static float y = 0;
  float w = whiteFloat();
  y = 0.997f * y + 0.05f * w;
  // Light hard limit (shouldn't normally hit)
  if (y > 1.0f)  y =  1.0f;
  if (y < -1.0f) y = -1.0f;
  // Brown noise is naturally low-amplitude after filtering — boost so it's
  // actually audible through the speaker.
  float out = y * 6.0f;
  if (out > 1.0f)  out =  1.0f;
  if (out < -1.0f) out = -1.0f;
  return (int16_t)(out * 32767.0f);
}

// --- A2DP data callback ------------------------------------------------------
volatile uint32_t bufUnderruns = 0;
volatile uint32_t bytesServed = 0;

int32_t fillA2dp(Frame *frame, int32_t frame_count) {
  if (!streaming) {
    memset(frame, 0, sizeof(Frame) * frame_count);
    return frame_count;
  }
  if (currentSource == SRC_STREAM) {
    size_t needed = frame_count * sizeof(Frame);
    size_t got = pcmRead((uint8_t*)frame, needed);
    if (got < needed) {
      memset((uint8_t*)frame + got, 0, needed - got);
      bufUnderruns++;
    }
    bytesServed += got;
    return frame_count;
  }
  // noise source
  for (int i = 0; i < frame_count; i++) {
    int16_t s;
    switch (noiseMode) {
      case WHITE: s = whiteSample(); break;
      case PINK:  s = pinkSample();  break;
      case BROWN: default: s = brownSample(); break;
    }
    frame[i].channel1 = s;
    frame[i].channel2 = s;
  }
  return frame_count;
}

// --- HTTP stream task: pulls PCM from atlas01 into ring buffer --------------
TaskHandle_t streamTaskHandle = nullptr;
volatile bool streamTaskShouldRun = true;

// Parse URL like "http://192.168.0.143:8081/pcm" into host/port/path.
// Static buffers; not thread-safe but only used by one task.
static bool parseStreamUrl(char* host, int hostLen, int* port, char* path, int pathLen) {
  const char* u = STREAM_URL;
  if (strncmp(u, "http://", 7) != 0) return false;
  u += 7;
  int i = 0;
  while (*u && *u != ':' && *u != '/' && i < hostLen - 1) host[i++] = *u++;
  host[i] = 0;
  *port = 80;
  if (*u == ':') { u++; *port = atoi(u); while (*u && *u != '/') u++; }
  if (*u == '/') { snprintf(path, pathLen, "%s", u); }
  else { snprintf(path, pathLen, "/"); }
  return host[0] != 0;
}

void streamTask(void* arg) {
  uint8_t scratch[1024];
  char host[64], path[64];
  int port = 80;
  if (!parseStreamUrl(host, sizeof(host), &port, path, sizeof(path))) {
    logLine("[stream] bad URL: %s", STREAM_URL);
    vTaskDelete(NULL);
    return;
  }
  logLine("[stream] target host=%s port=%d path=%s", host, port, path);

  while (streamTaskShouldRun) {
    if (currentSource != SRC_STREAM || !streaming || WiFi.status() != WL_CONNECTED) {
      vTaskDelay(pdMS_TO_TICKS(500));
      continue;
    }
    WiFiClient client;
    client.setTimeout(5);
    logLine("[stream] connecting tcp://%s:%d", host, port);
    if (!client.connect(host, port)) {
      logLine("[stream] tcp connect failed, retry 3s");
      vTaskDelay(pdMS_TO_TICKS(3000));
      continue;
    }
    // Send HTTP/1.0 GET (no chunked encoding to deal with)
    client.printf("GET %s HTTP/1.0\r\nHost: %s\r\nConnection: keep-alive\r\n\r\n", path, host);
    // Read headers, skip until empty line
    int hdrLines = 0;
    while (client.connected() && hdrLines < 30) {
      String line = client.readStringUntil('\n');
      if (line.length() <= 1) break;  // empty line = end of headers
      hdrLines++;
    }
    logLine("[stream] HTTP connected, streaming PCM");
    unsigned long lastLog = millis();
    uint32_t streamBytes = 0;
    // Upsample buffer: each 16-bit mono input sample becomes 8 bytes of
    // stereo 44.1 kHz output (2 stereo frames, both channels = sample).
    uint8_t outbuf[1024 * 4];
    while (client.connected() && streamTaskShouldRun
           && currentSource == SRC_STREAM && streaming) {
      // Yield first so HTTP server / other tasks get CPU.
      vTaskDelay(pdMS_TO_TICKS(5));

      int avail = client.available();
      if (avail > 0) {
        // Read mono 22.05 kHz s16le samples; cap to 1024 bytes (512 samples).
        int rd = client.read(scratch, min(1024, avail));
        if (rd > 0 && (rd & 1) == 0) {  // need an even byte count
          int n_samples = rd / 2;
          const int16_t* in_s = (const int16_t*)scratch;
          int16_t* out_s = (int16_t*)outbuf;
          // Each input mono sample -> 4 output int16s (L, R, L, R) at 44.1 kHz
          for (int i = 0; i < n_samples; i++) {
            int16_t s = in_s[i];
            out_s[i * 4 + 0] = s;
            out_s[i * 4 + 1] = s;
            out_s[i * 4 + 2] = s;
            out_s[i * 4 + 3] = s;
          }
          size_t outBytes = (size_t)n_samples * 8;
          size_t written = 0;
          while (written < outBytes) {
            size_t w = pcmWrite(outbuf + written, outBytes - written);
            written += w;
            if (written < outBytes) vTaskDelay(pdMS_TO_TICKS(10));
          }
          streamBytes += rd;
        }
      }
      if (millis() - lastLog > 30000) {
        logLine("[stream] %u bytes streamed, ring used %u/%u, underruns=%u",
                streamBytes, (unsigned)pcmUsed(), (unsigned)RB_BYTES, (unsigned)bufUnderruns);
        lastLog = millis();
      }
    }
    client.stop();
    logLine("[stream] disconnected, reconnect in 1s");
    vTaskDelay(pdMS_TO_TICKS(1000));
  }
  vTaskDelete(NULL);
}

// --- BT matcher: name-only ---------------------------------------------------
static inline bool containsCI(const char* hay, const char* needle) {
  if (!hay || !needle) return false;
  size_t nl = strlen(needle);
  for (const char* p = hay; *p; p++) {
    if (strncasecmp(p, needle, nl) == 0) return true;
  }
  return false;
}
static char lastScannedName[64] = "(none)";
static int  lastScannedRssi = 0;
bool ssidMatcherWrapped(const char* ssid, esp_bd_addr_t addr, int rssi) {
  snprintf(lastScannedName, sizeof(lastScannedName), "%s [%02X:%02X:%02X:%02X:%02X:%02X]",
           ssid ? ssid : "?", addr[0], addr[1], addr[2], addr[3], addr[4], addr[5]);
  lastScannedRssi = rssi;
  bool nameHit = containsCI(ssid, "Bose") || containsCI(ssid, "SoundLink");
  logLine("[match] %02X:%02X:%02X:%02X:%02X:%02X rssi=%d name='%s' -> %s",
          addr[0], addr[1], addr[2], addr[3], addr[4], addr[5], rssi,
          ssid ? ssid : "(none)", nameHit ? "ACCEPT" : "reject");
  return nameHit;
}

// --- A2DP state callback -----------------------------------------------------
volatile esp_a2d_connection_state_t lastConnState = ESP_A2D_CONNECTION_STATE_DISCONNECTED;
void onConnState(esp_a2d_connection_state_t state, void* obj) {
  lastConnState = state;
  const char* s;
  switch (state) {
    case ESP_A2D_CONNECTION_STATE_DISCONNECTED: s = "DISCONNECTED"; break;
    case ESP_A2D_CONNECTION_STATE_CONNECTING:   s = "CONNECTING";   break;
    case ESP_A2D_CONNECTION_STATE_CONNECTED:    s = "CONNECTED";    break;
    case ESP_A2D_CONNECTION_STATE_DISCONNECTING:s = "DISCONNECTING";break;
    default: s = "?"; break;
  }
  logLine("[A2DP] connection state -> %s", s);
}
const char* connStateStr() {
  switch (lastConnState) {
    case ESP_A2D_CONNECTION_STATE_DISCONNECTED: return "DISCONNECTED";
    case ESP_A2D_CONNECTION_STATE_CONNECTING:   return "CONNECTING";
    case ESP_A2D_CONNECTION_STATE_CONNECTED:    return "CONNECTED";
    case ESP_A2D_CONNECTION_STATE_DISCONNECTING:return "DISCONNECTING";
    default: return "?";
  }
}
const char* sourceStr() {
  return currentSource == SRC_STREAM ? "stream" : "noise";
}
const char* noiseStr() {
  switch (noiseMode) { case BROWN: return "brown"; case WHITE: return "white"; case PINK: return "pink"; }
  return "?";
}

// --- HTTP handlers ----------------------------------------------------------
const char HTML_PAGE[] PROGMEM = R"PAGE(<!doctype html>
<html><head><meta charset="utf-8"><title>White noise</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font-family:system-ui;max-width:560px;margin:18px auto;padding:0 14px;background:#111;color:#eee}
h1{font-weight:300;margin:6px 0}
button{display:block;width:100%;padding:14px;margin:6px 0;font-size:16px;border:1px solid #444;background:#222;color:#eee;border-radius:8px;cursor:pointer}
button:active{transform:scale(0.98)}
button.active{background:#28a;border-color:#48c}
.go{background:#2c5}.stop{background:#a33}.pair{background:#36c}
.row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}
.row.two{grid-template-columns:1fr 1fr}
.row button{margin:0}
.stat{padding:12px;background:#1a1a1a;border-radius:8px;margin:12px 0;display:grid;grid-template-columns:auto 1fr;gap:6px 16px;align-items:center}
.k{color:#888;font-size:13px}
.v{font-size:16px}
.ok{color:#5c5}.bad{color:#c55}.warn{color:#dc6}
.spin{display:inline-block;width:12px;height:12px;border:2px solid #36c;border-top:2px solid transparent;border-radius:50%;animation:r 1s linear infinite;margin-right:6px;vertical-align:-2px}
@keyframes r{to{transform:rotate(360deg)}}
#log{background:#000;color:#9c9;font-family:ui-monospace,monospace;font-size:11px;padding:8px;border-radius:6px;height:260px;overflow-y:scroll;white-space:pre;margin-top:14px;border:1px solid #333}
.log-hdr{display:flex;justify-content:space-between;align-items:center;margin-top:14px;color:#888;font-size:12px}
.sect{color:#888;font-size:13px;margin-top:14px;margin-bottom:4px}
</style></head>
<body>
<h1>White noise → Bose</h1>
<div class="stat">
  <div class="k">Bluetooth</div><div class="v" id="bt">…</div>
  <div class="k">Source</div><div class="v" id="source">…</div>
  <div class="k">Stream</div><div class="v" id="stream">…</div>
  <div class="k">Volume</div><div class="v" id="vol">…</div>
  <div class="k">Buffer / underruns</div><div class="v" id="buf">—</div>
  <div class="k">Last scanned</div><div class="v" id="rssi">—</div>
</div>
<button class="go" onclick="p('/play')">▶ Play</button>
<button class="stop" onclick="p('/stop')">■ Stop</button>
<button class="pair" onclick="p('/pair')">⇆ (Re)pair — put Bose in pairing mode first</button>
<div class="sect">Source</div>
<div class="row two">
  <button id="b-stream" onclick="p('/source/stream')">📡 Rain (HTTP)</button>
  <button id="b-noise"  onclick="p('/source/noise')">🌀 On-chip noise</button>
</div>
<div class="sect">Noise type (when on-chip)</div>
<div class="row">
  <button id="b-brown" onclick="p('/noise/brown')">Brown</button>
  <button id="b-white" onclick="p('/noise/white')">White</button>
  <button id="b-pink"  onclick="p('/noise/pink')">Pink</button>
</div>
<div class="sect">Volume</div>
<div class="row">
  <button onclick="setVol(40)">vol 40</button>
  <button onclick="setVol(70)">vol 70</button>
  <button onclick="setVol(100)">vol 100</button>
</div>
<div class="log-hdr"><span>Log (live)</span><span id="seq"></span></div>
<div id="log"></div>
<script>
let lastSeq = 0;
async function p(url){
  document.body.style.opacity = 0.6;
  try{ await fetch(url,{method:'POST'}); }catch(e){}
  document.body.style.opacity = 1;
  refresh();
}
async function setVol(v){
  try{ await fetch('/volume?v='+v,{method:'POST'}); }catch(e){}
  refresh();
}
function setActive(prefix, key){
  ['stream','noise','brown','white','pink'].forEach(k=>{
    const el = document.getElementById('b-'+k);
    if (el) el.classList.toggle('active', k===key);
  });
}
async function refresh(){
  try{
    const r=await(await fetch('/status.json')).json();
    const bt = document.getElementById('bt');
    bt.innerHTML = (r.bt==='CONNECTING' ? '<span class="spin"></span>' : '') + r.bt;
    bt.className = 'v ' + (r.bt==='CONNECTED' ? 'ok' : (r.bt==='CONNECTING' ? 'warn' : 'bad'));
    document.getElementById('source').textContent = r.source + (r.source==='noise' ? ' / ' + r.noise : '');
    document.getElementById('stream').textContent=r.streaming?'on':'off';
    document.getElementById('vol').textContent=r.volume;
    document.getElementById('buf').textContent = r.buf_pct + '% full, ' + r.underruns + ' underruns';
    document.getElementById('rssi').textContent=r.last_rssi+' dBm @ '+r.last_seen;
    setActive('source', r.source);
    if (r.source==='noise') setActive('noise', r.noise);
    else setActive('noise', null);
  }catch(e){}
}
async function tailLog(){
  try{
    const r=await(await fetch('/log?since='+lastSeq)).json();
    if (r.lines && r.lines.length){
      const el = document.getElementById('log');
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
      r.lines.forEach(line => { el.textContent += line + '\n'; });
      if (atBottom) el.scrollTop = el.scrollHeight;
      lastSeq = r.seq;
      document.getElementById('seq').textContent = 'seq '+r.seq;
    }
  }catch(e){}
}
refresh();
tailLog();
setInterval(refresh, 1000);
setInterval(tailLog, 700);
</script></body></html>
)PAGE";

void handleRoot() { http.send_P(200, "text/html", HTML_PAGE); }

void handleStatus() {
  int bufPct = (int)(pcmUsed() * 100 / RB_BYTES);
  String body = "{";
  body += "\"bt\":\"";       body += connStateStr(); body += "\",";
  body += "\"source\":\"";   body += sourceStr();    body += "\",";
  body += "\"noise\":\"";    body += noiseStr();     body += "\",";
  body += "\"streaming\":";  body += streaming ? "true" : "false"; body += ",";
  body += "\"volume\":";     body += btVolume;       body += ",";
  body += "\"last_seen\":\"";body += lastScannedName;body += "\",";
  body += "\"last_rssi\":";  body += lastScannedRssi;body += ",";
  body += "\"buf_pct\":";    body += bufPct;         body += ",";
  body += "\"underruns\":";  body += (unsigned long)bufUnderruns;
  body += "}";
  http.send(200, "application/json", body);
}

void handleLog() {
  uint32_t since = 0;
  if (http.hasArg("since")) since = (uint32_t)http.arg("since").toInt();
  String body = "{\"seq\":";
  portENTER_CRITICAL(&logMux);
  uint32_t cur = logSeq;
  uint32_t start = since;
  if (cur > LOG_LINES && start < cur - LOG_LINES) start = cur - LOG_LINES;
  body += cur;
  body += ",\"lines\":[";
  bool first = true;
  for (uint32_t i = start; i < cur; i++) {
    if (!first) body += ",";
    first = false;
    body += "\"";
    const char* src = logRing[i % LOG_LINES];
    for (const char* p = src; *p; p++) {
      char c = *p;
      if (c == '"')       body += "\\\"";
      else if (c == '\\') body += "\\\\";
      else if (c == '\n') body += "\\n";
      else if ((unsigned char)c < 0x20) body += " ";
      else                body += c;
    }
    body += "\"";
  }
  body += "]}";
  portEXIT_CRITICAL(&logMux);
  http.send(200, "application/json", body);
}

void handlePair() {
  logLine("[CMD] pair requested — cancel + restart inquiry");
  a2dp.cancel_discovery();
  delay(300);
  esp_bt_gap_start_discovery(ESP_BT_INQ_MODE_GENERAL_INQUIRY, 10, 0);
  http.send(200, "text/plain", "scanning");
}
void handlePlay()   { streaming = true;  logLine("[CMD] play");  http.send(200, "text/plain", "playing"); }
void handleStop()   { streaming = false; logLine("[CMD] stop");  http.send(200, "text/plain", "stopped"); }
void handleSrcStream() {
  currentSource = SRC_STREAM;
  logLine("[CMD] source=stream");
  ensureStreamTask();
  http.send(200, "text/plain", "stream");
}
void handleSrcNoise()  { currentSource = SRC_NOISE;  logLine("[CMD] source=noise");  http.send(200, "text/plain", "noise");  }
void handleBrown()  { noiseMode = BROWN; logLine("[CMD] noise=brown"); http.send(200, "text/plain", "brown"); }
void handleWhite()  { noiseMode = WHITE; logLine("[CMD] noise=white"); http.send(200, "text/plain", "white"); }
void handlePink()   { noiseMode = PINK;  logLine("[CMD] noise=pink");  http.send(200, "text/plain", "pink");  }
void handleVolume() {
  if (http.hasArg("v")) {
    int v = http.arg("v").toInt();
    v = max(0, min(100, v));
    btVolume = v;
    a2dp.set_volume(v);
    logLine("[CMD] volume=%d", v);
  }
  http.send(200, "text/plain", "ok");
}

void ensureStreamTask() {
  if (streamTaskStarted) return;
  // Pin to core 0 (PROTOCOL CPU). Arduino loop() and the HTTP server run on core 1.
  // Keeping them on separate cores so the HTTP UI stays responsive while audio streams.
  BaseType_t ok = xTaskCreatePinnedToCore(streamTask, "stream", 4096, NULL, 1, &streamTaskHandle, 0);
  if (ok != pdPASS) {
    logLine("[!] stream task spawn failed (heap free=%u)", (unsigned)ESP.getFreeHeap());
    return;
  }
  streamTaskStarted = true;
}

void setup() {
  Serial.begin(115200);
  delay(400);

  logLine("=== bt-white-noise v4.6 boot ===");
  logLine("free heap at start: %u", (unsigned)ESP.getFreeHeap());

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  logLine("WiFi connecting to '%s'...", WIFI_SSID);
  int retries = 0;
  while (WiFi.status() != WL_CONNECTED && retries < 60) {
    delay(500); retries++;
  }
  if (WiFi.status() == WL_CONNECTED) {
    logLine("WiFi OK — IP: %s", WiFi.localIP().toString().c_str());
  } else {
    logLine("WiFi FAILED");
  }

  http.on("/",                HTTP_GET,  handleRoot);
  http.on("/status.json",     HTTP_GET,  handleStatus);
  http.on("/log",             HTTP_GET,  handleLog);
  http.on("/pair",            HTTP_POST, handlePair);
  http.on("/play",            HTTP_POST, handlePlay);
  http.on("/stop",            HTTP_POST, handleStop);
  http.on("/source/stream",   HTTP_POST, handleSrcStream);
  http.on("/source/noise",    HTTP_POST, handleSrcNoise);
  http.on("/noise/brown",     HTTP_POST, handleBrown);
  http.on("/noise/white",     HTTP_POST, handleWhite);
  http.on("/noise/pink",      HTTP_POST, handlePink);
  http.on("/volume",          HTTP_POST, handleVolume);
  http.begin();
  logLine("HTTP server on :80");

  a2dp.set_valid_cod_service(0xFFFF);
  a2dp.set_ssid_callback(ssidMatcherWrapped);
  a2dp.set_auto_reconnect(true, 10);
  a2dp.set_volume(btVolume);
  a2dp.set_on_connection_state_changed(onConnState);
  a2dp.set_data_callback_in_frames(fillA2dp);
  logLine("Starting A2DP scan (name match: 'Bose' or 'SoundLink')");
  a2dp.start();
  logLine("free heap after a2dp.start: %u", (unsigned)ESP.getFreeHeap());

  // Lower BT classic TX power some — the Bose is in the same room
  // (RSSI ~-25 dBm). Default is +4 dBm; -6 to +3 dBm range frees radio time
  // for WiFi without breaking BT handshake (N12 was too low to stay connected).
  esp_bredr_tx_power_set(ESP_PWR_LVL_N6, ESP_PWR_LVL_P3);
  logLine("BT TX power set to N6..P3 (~-6 to +3 dBm)");

  // Quiet ESP-IDF logs once we're running — they were flooding the serial
  // and the ring buffer (and the inquiry-scan [match] lines from libraries
  // chew CPU).
  esp_log_level_set("*", ESP_LOG_WARN);
  logLine("Visit http://%s for control. POST /source/stream to enable HTTP streaming.",
          WiFi.localIP().toString().c_str());
}

void loop() {
  http.handleClient();
  delay(2);
}
