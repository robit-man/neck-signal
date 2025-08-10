#!/usr/bin/env python3
"""
app.py — NKN QR handshake, presence, prioritized command topic, color+depth streams, serial control + live previews.

What's in here:
- Dual capture: COLOR_URL (alias VIDEO_URL) + optional DEPTH_URL.
- Auto-detects OpenCV-friendly streams; otherwise falls back to a robust MJPEG parser.
- Streams via DM (always) and Topic (if ENABLE_TOPICS=1):
    * color: {event:"frame-color", data:<b64 JPEG>, w, h, uuid, seq, ts}
    * depth: {event:"frame-depth", data:<b64 PNG>,  w, h, uuid, seq, ts}
- Announces capabilities via {event:"streams", ...} including per-channel topics:
    * color.topic = <TOPIC_PREFIX>.rgb.<DEVICE_UUID>
    * depth.topic = <TOPIC_PREFIX>.depth.<DEVICE_UUID>
- Per-peer pacing/coalescing. /res affects RGB only; depth has DEPTH_SCALE (global).
- Commands via topic (low-latency) or DM (fallback). Node sidecar handles subscribe.

Updates in this build:
- Depth preview window (normalized grayscale) with FPS overlay to verify it's live.
- Console log whenever /res changes: prints address and new percent.
- Symmetric color/depth streaming, distinct topics, seq/ts to defeat caching.
- Orientation UI "Center" (or /center) maps to HOME.
"""

# ──────────────────────────────────────────────────────────────────────
# 0) venv bootstrap (stdlib)
# ──────────────────────────────────────────────────────────────────────
import os, sys, subprocess, json, time, threading, base64, re, signal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
VENV_DIR = BASE_DIR / "venv"
BIN_DIR  = VENV_DIR / ("Scripts" if os.name == "nt" else "bin")
PY_VENV  = BIN_DIR / "python"

def _in_venv() -> bool:
    try:
        return Path(sys.executable).resolve() == PY_VENV.resolve()
    except Exception:
        return False

def _create_venv():
    if VENV_DIR.exists(): return
    import venv; venv.EnvBuilder(with_pip=True).create(VENV_DIR)
    subprocess.check_call([str(PY_VENV), "-m", "pip", "install", "--upgrade", "pip"])

if not _in_venv():
    _create_venv()
    subprocess.check_call([str(PY_VENV), "-m", "pip", "install", "--quiet",
                           "numpy", "opencv-python", "pynacl", "requests", "pyserial"])
    os.execv(str(PY_VENV), [str(PY_VENV), *sys.argv])

# ──────────────────────────────────────────────────────────────────────
# 1) imports (inside venv)
# ──────────────────────────────────────────────────────────────────────
import argparse
from typing import Optional, Dict, Any, Tuple, List
import urllib.parse
import requests
import numpy as np
import cv2
import secrets
import uuid as _uuid
import shutil
from subprocess import Popen, PIPE
from nacl.signing import SigningKey, VerifyKey
import serial  # pyserial

# ──────────────────────────────────────────────────────────────────────
# 2) args & .env
# ──────────────────────────────────────────────────────────────────────
cli = argparse.ArgumentParser()
cli.add_argument("--video-url", default=os.environ.get("VIDEO_URL", os.environ.get("COLOR_URL", "http://127.0.0.1:8080/video/rs_color")))
cli.add_argument("--color-url", default=os.environ.get("COLOR_URL", None))
cli.add_argument("--depth-url", default=os.environ.get("DEPTH_URL", ""))
cli.add_argument("--stream-hz", type=int, default=int(os.environ.get("STREAM_HZ", os.environ.get("COLOR_HZ","24"))))
cli.add_argument("--depth-hz",  type=int, default=int(os.environ.get("DEPTH_HZ","10")))
cli.add_argument("--scan-max-width", type=int, default=int(os.environ.get("SCAN_MAX_WIDTH", "960")))
cli.add_argument("--label", default=os.environ.get("DEVICE_LABEL","neck-agent"))
cli.add_argument("--uuid", default=os.environ.get("DEVICE_UUID",""))
cli.add_argument("--subclients", type=int, default=int(os.environ.get("SUBCLIENTS","6")))
cli.add_argument("--presence-ttl", type=float, default=float(os.environ.get("PRESENCE_TTL","20")))
cli.add_argument("--enable-topics", type=int, default=int(os.environ.get("ENABLE_TOPICS","1")))
cli.add_argument("--cmd-topic-strategy", default=os.environ.get("CMD_TOPIC_STRATEGY","uuid"), choices=["uuid","pubhex"])
cli.add_argument("--cmd-topic-duration", type=int, default=int(os.environ.get("CMD_TOPIC_DURATION","4320")))
cli.add_argument("--jpeg-quality", type=int, default=int(os.environ.get("JPEG_QUALITY","65")))
cli.add_argument("--depth-scale", type=float, default=float(os.environ.get("DEPTH_SCALE","0.6")))
cli.add_argument("--max-b64", type=int, default=int(os.environ.get("MAX_B64","900000")))
args = cli.parse_args()

ENV_PATH = BASE_DIR / ".env"
def _load_env(path: Path) -> Dict[str, str]:
    d: Dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    return d
def _save_env(path: Path, env: Dict[str, str]) -> None:
    body = "".join(f"{k}={v}\n" for k, v in env.items())
    path.write_text(body)

dotenv = _load_env(ENV_PATH)
if "DEVICE_SEED_HEX" not in dotenv:
    dotenv["DEVICE_SEED_HEX"] = secrets.token_hex(32)
if "DEVICE_TOPIC_PREFIX" not in dotenv:
    dotenv["DEVICE_TOPIC_PREFIX"] = "roko-signaling"
if "REV_COUNTER" not in dotenv:
    dotenv["REV_COUNTER"] = "0"

# normalize config
color_url = args.color_url or args.video_url or dotenv.get("COLOR_URL") or dotenv.get("VIDEO_URL") or "http://127.0.0.1:8080/video/rs_color"

dotenv["VIDEO_URL"]      = color_url
dotenv["COLOR_URL"]      = color_url
dotenv["DEPTH_URL"]      = args.depth_url or dotenv.get("DEPTH_URL","")
dotenv["STREAM_HZ"]      = str(max(1, min(60, int(args.stream_hz))))
dotenv["COLOR_HZ"]       = dotenv["STREAM_HZ"]
dotenv["DEPTH_HZ"]       = str(max(1, min(60, int(args.depth_hz))))
dotenv["SCAN_MAX_WIDTH"] = str(max(0, min(2560, int(args.scan_max_width))))
dotenv["DEVICE_UUID"]    = args.uuid or dotenv.get("DEVICE_UUID") or str(_uuid.uuid4())
dotenv["SUBCLIENTS"]     = str(max(1, min(16, int(args.subclients))))
dotenv["PRESENCE_TTL"]   = str(max(5, int(args.presence_ttl)))
dotenv["ENABLE_TOPICS"]  = "1" if int(args.enable_topics) else "0"
dotenv["CMD_TOPIC_STRATEGY"] = args.cmd_topic_strategy
dotenv["CMD_TOPIC_DURATION"] = str(max(60, int(args.cmd_topic_duration)))
dotenv["JPEG_QUALITY"]   = str(max(30, min(95, int(args.jpeg_quality))))
dotenv["DEPTH_SCALE"]    = str(max(0.1, min(1.0, float(args.depth_scale))))
dotenv["MAX_B64"]        = str(max(200000, int(args.max_b64)))
_save_env(ENV_PATH, dotenv)

DEVICE_SEED_HEX = dotenv["DEVICE_SEED_HEX"].lower().replace("0x","")
TOPIC_PREFIX    = dotenv["DEVICE_TOPIC_PREFIX"]
REV_COUNTER     = int(dotenv["REV_COUNTER"])
COLOR_URL       = dotenv["COLOR_URL"]
DEPTH_URL       = dotenv.get("DEPTH_URL","")
COLOR_HZ        = max(1, min(60, int(dotenv["COLOR_HZ"])))
DEPTH_HZ        = max(1, min(60, int(dotenv["DEPTH_HZ"])))
SCAN_MAX_WIDTH  = max(0, min(2560, int(dotenv["SCAN_MAX_WIDTH"])))
DEVICE_UUID     = dotenv["DEVICE_UUID"]
DEVICE_LABEL    = args.label
SUBCLIENTS      = max(1, min(16, int(dotenv["SUBCLIENTS"])))
PRESENCE_TTL    = max(5, int(dotenv["PRESENCE_TTL"]))
ENABLE_TOPICS   = dotenv.get("ENABLE_TOPICS","0") == "1"
CMD_TOPIC_STRATEGY = dotenv.get("CMD_TOPIC_STRATEGY","uuid")
CMD_TOPIC_DURATION = max(60, int(dotenv.get("CMD_TOPIC_DURATION","4320")))
NKN_WALLET_SEED = os.environ.get("NKN_WALLET_SEED", DEVICE_SEED_HEX)
NKN_RPC_SERVER  = os.environ.get("NKN_RPC_SERVER","")
JPEG_QUALITY    = max(30, min(95, int(dotenv["JPEG_QUALITY"])))
DEPTH_SCALE     = max(0.1, min(1.0, float(dotenv["DEPTH_SCALE"])))
MAX_B64         = max(200000, int(dotenv.get("MAX_B64","900000")))

# flags + topics
HAS_DEPTH = False
COLOR_TOPIC = f"{TOPIC_PREFIX}.rgb.{DEVICE_UUID}"
DEPTH_TOPIC = f"{TOPIC_PREFIX}.depth.{DEVICE_UUID}"

# ──────────────────────────────────────────────────────────────────────
# 3) NKN bridge (Node sidecar)
# ──────────────────────────────────────────────────────────────────────
NODE_DIR = BASE_DIR / "device-bridge"
NODE_BIN = shutil.which("node")
NPM_BIN  = shutil.which("npm")
if not NODE_BIN or not NPM_BIN:
    sys.exit("‼️  Node.js and npm are required (Node 18+).")

PKG_JSON  = NODE_DIR / "package.json"
BRIDGE_JS = NODE_DIR / "nkn_device_bridge.js"
NODE_DIR.mkdir(exist_ok=True)

if not PKG_JSON.exists():
    print("→ Initializing device-bridge …")
    subprocess.check_call([NPM_BIN, "init", "-y"], cwd=NODE_DIR, stdout=subprocess.DEVNULL)
    subprocess.check_call([NPM_BIN, "install", "nkn-sdk@^1.3.6"], cwd=NODE_DIR, stdout=subprocess.DEVNULL)

BRIDGE_SRC = r"""
/* nkn_device_bridge.js — MultiClient DM + optional topic subscribe/publish. */
const nkn = require('nkn-sdk');
const readline = require('readline');

const SEED_HEX   = (process.env.DEVICE_SEED_HEX || '').toLowerCase().replace(/^0x/,'');
const IDENT      = process.env.DEVICE_IDENT || 'device';
const TOPIC_NS   = process.env.DEVICE_TOPIC_PREFIX || 'roko-signaling';
const SUBS       = Math.max(1, Math.min(16, parseInt(process.env.SUBCLIENTS || '6', 10)));
const ENABLE_TOPICS = (process.env.ENABLE_TOPICS || '0') === '1';
const CMD_TOPIC  = process.env.CMD_TOPIC || '';
const TOPIC_DUR  = Math.max(60, parseInt(process.env.CMD_TOPIC_DURATION || '4320', 10));
const RPC        = process.env.NKN_RPC_SERVER || '';
const WALLET_SEED= (process.env.NKN_WALLET_SEED || SEED_HEX).toLowerCase().replace(/^0x/,'');

function sendToPy(obj){ process.stdout.write(JSON.stringify(obj) + '\n'); }
function isFullAddr(s){ return typeof s === 'string' && /^[A-Za-z0-9_-]+\.[0-9a-f]{64}$/i.test((s||'').trim()); }
function isHex64(s){ return typeof s === 'string' && /^[0-9a-f]{64}$/i.test((s||'').trim()); }
function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }

async function sendDMWithRetry(client, dest, data, tries=2){
  if (!isFullAddr(dest) && !isHex64(dest)) return false;
  const body = JSON.stringify(data);
  const t = Math.max(1, Math.min(3, parseInt(tries || 2, 10)));
  for (let i=0;i<t;i++){
    try { await client.send(dest, body); return true; }
    catch(e){ await sleep(20); }
  }
  return false;
}

function parseInboundPayload(a,b){
  let src, payload, topic=null;
  if (a && typeof a === 'object' && (a.payload !== undefined || a.data !== undefined || a.src !== undefined)) {
    src = a.src || a.from || a.addr || '';
    payload = (a.payload !== undefined) ? a.payload : a.data;
    if (a.topic) topic = a.topic;
  } else { src = a; payload = b; }
  let msg;
  try {
    const txt = Buffer.isBuffer(payload) ? payload.toString('utf8')
              : (typeof payload==='string' ? payload : JSON.stringify(payload));
    try { msg = JSON.parse(txt); } catch { msg = { raw: txt }; }
  } catch { msg = {}; }
  return { src, topic, msg };
}

(async () => {
  if (!/^[0-9a-f]{64}$/i.test(SEED_HEX)) throw new RangeError('invalid hex seed');
  const client = new nkn.MultiClient({ seed: SEED_HEX, identifier: IDENT, numSubClients: SUBS });

  let wallet = null;
  if (ENABLE_TOPICS) {
    try {
      wallet = new nkn.Wallet({ seed: WALLET_SEED, rpcServer: RPC || undefined });
      client.wallet = wallet;
    } catch (e) {
      sendToPy({ type:'topics-error', error: String(e) });
    }
  }

  client.on('connect', async () => {
    sendToPy({ type: 'ready', address: client.addr, topicPrefix: TOPIC_NS, subs: SUBS });
    if (ENABLE_TOPICS && wallet && CMD_TOPIC) {
      try {
        await client.subscribe(CMD_TOPIC, TOPIC_DUR, 'cmd');
        sendToPy({ type:'topics-ready', cmdTopic: CMD_TOPIC, duration: TOPIC_DUR });
      } catch (e) {
        sendToPy({ type:'topics-error', error: String(e) });
      }
    } else if (ENABLE_TOPICS) {
      sendToPy({ type:'topics-error', error: 'wallet or CMD_TOPIC missing' });
    }
  });

  client.on('message', (a,b) => {
    const { src, topic, msg } = parseInboundPayload(a,b);
    sendToPy({ type:'message', src, topic: topic || null, msg });
  });

  const rl = readline.createInterface({ input: process.stdin });
  rl.on('line', async (line) => {
    if (!line) return;
    let cmd; try { cmd = JSON.parse(line); } catch { return; }
    if (cmd.type === 'dm') {
      await sendDMWithRetry(client, String(cmd.to||'').trim(), cmd.data, cmd.tries || 2);
    } else if (cmd.type === 'pub') {
      if (cmd.topic) { try { await client.publish(String(cmd.topic), JSON.stringify(cmd.data)); } catch {} }
    }
  });
})();
"""
if not BRIDGE_JS.exists() or BRIDGE_JS.read_text() != BRIDGE_SRC:
    BRIDGE_JS.write_text(BRIDGE_SRC)

def _cmd_topic_name(device_pubhex: str) -> str:
    return f"{TOPIC_PREFIX}.cmd.{DEVICE_UUID}" if CMD_TOPIC_STRATEGY=="uuid" else f"{TOPIC_PREFIX}.cmd.{device_pubhex}"

# crypto helpers
def b64url_encode(b: bytes) -> str: return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")
def b64url_decode(s: str) -> bytes:
    s = (s or "").strip().replace(" ", "+")
    pad = '=' * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s + pad)

def load_device_keys(seed_hex: str) -> Tuple[SigningKey, str]:
    seed = bytes.fromhex(seed_hex)
    sk = SigningKey(seed)
    pk = sk.verify_key.encode().hex()
    return sk, pk

DEVICE_SK, DEVICE_PUBHEX = load_device_keys(DEVICE_SEED_HEX)
CMD_TOPIC = _cmd_topic_name(DEVICE_PUBHEX)

def sign_token(body: dict) -> str:
    body_json = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = DEVICE_SK.sign(body_json).signature
    return f"{b64url_encode(body_json)}.{b64url_encode(sig)}"

# ──────────────────────────────────────────────────────────────────────
# 4) spawn sidecar
# ──────────────────────────────────────────────────────────────────────
bridge_env = os.environ.copy()
bridge_env["DEVICE_SEED_HEX"]     = DEVICE_SEED_HEX
bridge_env["DEVICE_IDENT"]        = os.environ.get("DEVICE_IDENT","device")
bridge_env["DEVICE_TOPIC_PREFIX"] = TOPIC_PREFIX
bridge_env["SUBCLIENTS"]          = str(SUBCLIENTS)
bridge_env["ENABLE_TOPICS"]       = "1" if ENABLE_TOPICS else "0"
bridge_env["CMD_TOPIC"]           = CMD_TOPIC
bridge_env["CMD_TOPIC_DURATION"]  = str(CMD_TOPIC_DURATION)
bridge_env["NKN_WALLET_SEED"]     = NKN_WALLET_SEED
if NKN_RPC_SERVER: bridge_env["NKN_RPC_SERVER"] = NKN_RPC_SERVER

bridge = Popen(
    [str(shutil.which("node")), str(BRIDGE_JS)],
    cwd=NODE_DIR, env=bridge_env,
    stdin=PIPE, stdout=PIPE, stderr=PIPE, text=True, bufsize=1
)

state: Dict[str, Any] = {
    "client_address": None,
    "topic_prefix": TOPIC_PREFIX,
    "subs": SUBCLIENTS,
    "topics_ready": False,
    "cmd_topic": CMD_TOPIC,
}

def _bridge_send(obj: dict):
    try:
        bridge.stdin.write(json.dumps(obj) + "\n"); bridge.stdin.flush()
    except Exception as e:
        print("bridge send error:", e)

def _dm(dest: str, data: dict, tries: int = 2):
    _bridge_send({"type":"dm","to":dest,"data":data,"tries":tries})

def _pub(topic: str, data: dict):
    if ENABLE_TOPICS:
        _bridge_send({"type":"pub","topic":topic,"data":data})

def _shutdown(*_):
    try:
        if bridge and bridge.poll() is None:
            bridge.terminate()
    except Exception:
        pass
    try:
        cv2.destroyAllWindows()
    except Exception:
        pass
    try:
        if ser is not None and ser.is_open:
            ser.close()
    except Exception:
        pass
    sys.exit(0)

def _bridge_reader():
    for line in bridge.stdout:
        line = (line or "").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        t = msg.get("type")
        if t == "ready":
            state["client_address"] = msg.get("address")
            state["topic_prefix"]   = msg.get("topicPrefix") or TOPIC_PREFIX
            state["subs"]           = int(msg.get("subs") or SUBCLIENTS)
            print(f"→ NKN ready: {state['client_address']}  subs={state['subs']}")
            for addr in list(addresses.keys()):
                _dm(addr, {"event":"hello","from": state["client_address"], "uuid": DEVICE_UUID})
                send_stream_info(addr, force=True)
        elif t == "topics-ready":
            state["topics_ready"] = True
            state["cmd_topic"]    = msg.get("cmdTopic") or CMD_TOPIC
            print(f"→ topics: ready, cmd_topic={state['cmd_topic']}, duration={msg.get('duration')}")
        elif t == "topics-error":
            state["topics_ready"] = False
            print("→ topics: disabled/fallback:", msg.get("error",""))
        elif t == "message":
            src = (msg.get("src") or "").strip()
            payload = msg.get("msg")
            topic = msg.get("topic")
            try:
                handle_inbound(src, payload, topic=topic)
            except Exception as e:
                print("inbound handler error:", e)

def _bridge_err():
    for line in bridge.stderr:
        sys.stderr.write(line)

threading.Thread(target=_bridge_reader, daemon=True).start()
threading.Thread(target=_bridge_err, daemon=True).start()

# ──────────────────────────────────────────────────────────────────────
# 5) address book + peers
# ──────────────────────────────────────────────────────────────────────
ADDR_PATH = BASE_DIR / "addresses.json"
def _load_addresses() -> Dict[str, Dict[str, Any]]:
    if ADDR_PATH.exists():
        try: return json.loads(ADDR_PATH.read_text())
        except: return {}
    return {}
def _save_addresses(d: Dict[str, Dict[str, Any]]):
    ADDR_PATH.write_text(json.dumps(d, indent=2))

peers: Dict[str, Dict[str, Any]] = {}
addresses = _load_addresses()

def _ensure_peer(addr: str, *, default_scale: float = 1.0) -> Dict[str, Any]:
    st = peers.get(addr)
    if st is None:
        st = {
            "scale": float(addresses.get(addr,{}).get("scale", default_scale)),
            "stats_on": False,
            "stats_hz": 1,
            "last_stats_ts": 0.0,
            "sent_since_tick": 0,
            "last_tick_ts": time.time(),
            "online": False,
            "last_online": 0.0,
            "last_color_ts": 0.0,
            "last_depth_ts": 0.0,
            "last_info_ts": 0.0,
        }
        peers[addr] = st
    return st

def _persist_peer(addr: str, label: str, scopes: List[str], exp: int, device: str, *, scale: float = 1.0):
    addresses[addr] = {
        "label": label,
        "scopes": scopes,
        "exp": int(exp),
        "device": device,
        "scale": float(scale),
        "last_seen": int(time.time())
    }
    _save_addresses(addresses)
    _ensure_peer(addr, default_scale=scale)

# ──────────────────────────────────────────────────────────────────────
# 6) serial: actuator commands
# ──────────────────────────────────────────────────────────────────────
SER_POSSIBLE = ["/dev/ttyUSB0","/dev/ttyUSB1","/dev/tty0","/dev/tty1","COM3","COM4"]
ser: Optional[serial.Serial] = None
def _open_serial():
    global ser
    if ser is not None and ser.is_open:
        return
    for p in SER_POSSIBLE:
        try:
            ser = serial.Serial(p, 115200, timeout=1)
            print("[serial] opened:", p)
            break
        except Exception:
            ser = None
    if ser is None:
        print("[serial] no device found (commands will still be ACKed)")
_open_serial()

ALLOWED = {"X": (-700,700,int),"Y":(-700,700,int),"Z":(-700,700,int),
           "H": (0,70,int), "S":(0,10,float), "A":(0,10,float),
           "R": (-700,700,int),"P":(-700,700,int)}
state_axes = {k: (1.0 if k in ("S","A") else 0) for k in ALLOWED}

def _validate_axes_cmd(cmd:str)->bool:
    if cmd.lower()=="home": return True
    seen=set()
    for tok in cmd.split(","):
        m = re.match(r"^([XYZHSARP])(-?\d+(?:\.\d+)?)$", tok.strip())
        if not m or m.group(1) in seen: return False
        k,val = m.group(1), m.group(2); low,high,cast = ALLOWED[k]
        try: v=cast(val)
        except: return False
        if not low<=v<=high: return False
        seen.add(k)
    return True

def _merge_axes_cmd(cmd:str)->str:
    if cmd.lower()=="home":
        for k in state_axes: state_axes[k]=1.0 if k in ("S","A") else 0
        return "HOME"
    for tok in cmd.split(","):
        m = re.match(r"^([XYZHSARP])(-?\d+(?:\.\d+)?)$", tok.strip())
        if m:
            k,val=m.group(1),m.group(2); state_axes[k]=ALLOWED[k][2](val)
    return ",".join(f"{k}{state_axes[k]}" for k in ["X","Y","Z","H","S","A","R","P"])

def _serial_send(full:str)->bool:
    try:
        _open_serial()
        if ser is not None and ser.is_open:
            ser.write((full+"\n").encode())
            return True
    except Exception as e:
        print("[serial] error:", e)
    return False

# ──────────────────────────────────────────────────────────────────────
# 7) capture helpers (OpenCV + MJPEG fallback)
# ──────────────────────────────────────────────────────────────────────
class LatestFrameGrabber:
    def __init__(self, url: str):
        self.url = url
        try:
            self.cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        except Exception:
            self.cap = cv2.VideoCapture(url)
        for prop, val in [(cv2.CAP_PROP_BUFFERSIZE,1),(cv2.CAP_PROP_FPS,120),(cv2.CAP_PROP_CONVERT_RGB,1)]:
            try: self.cap.set(prop, val)
            except Exception: pass
        if not self.cap or not self.cap.isOpened():
            raise RuntimeError(f"Could not open video source (OpenCV): {url}")
        self._lock = threading.Lock()
        self._latest: Optional[np.ndarray] = None
        self._stopped = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self):
        while not self._stopped.is_set():
            ok, frame = self.cap.read()
            if not ok:
                time.sleep(0.005); continue
            with self._lock:
                self._latest = frame

    def read(self) -> Optional[np.ndarray]:
        with self._lock:
            return self._latest

    def stop(self):
        self._stopped.set()
        try: self._t.join(timeout=0.5)
        except Exception: pass
        try:
            if self.cap: self.cap.release()
        except Exception: pass

class MJPEGGrabber:
    def __init__(self, url: str, timeout=8.0):
        self.url = url
        self.timeout = timeout
        self._lock = threading.Lock()
        a = None
        self._latest: Optional[np.ndarray] = a
        self._stopped = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self):
        sess = requests.Session()
        while not self._stopped.is_set():
            try:
                with sess.get(self.url, stream=True, timeout=self.timeout) as r:
                    ct = r.headers.get("Content-Type","")
                    boundary = None
                    if "boundary=" in ct:
                        boundary = ct.split("boundary=",1)[1].strip().strip('"')
                    if not boundary:
                        boundary = "frame"
                    boundary_bytes = (b"--" + boundary.encode("ascii",errors="ignore")) if not boundary.startswith("--") else boundary.encode("ascii",errors="ignore")
                    buf = b""
                    for chunk in r.iter_content(chunk_size=4096):
                        if self._stopped.is_set(): break
                        if not chunk: continue
                        buf += chunk
                        while True:
                            bidx = buf.find(boundary_bytes)
                            if bidx < 0:
                                if len(buf) > 2_000_000:
                                    buf = buf[-1_000_000:]
                                break
                            start = bidx + len(boundary_bytes)
                            hdr_end = buf.find(b"\r\n\r\n", start)
                            if hdr_end < 0: break
                            headers = buf[start:hdr_end].decode("latin1", errors="ignore")
                            body_start = hdr_end + 4
                            clen = None
                            for line in headers.split("\r\n"):
                                if ":" in line:
                                    k,v = line.split(":",1)
                                    if k.strip().lower() == "content-length":
                                        try: clen = int(v.strip())
                                        except: pass
                            if clen is not None:
                                if len(buf) - body_start < clen: break
                                body = buf[body_start: body_start + clen]
                                buf = buf[body_start + clen:]
                            else:
                                next_b = buf.find(boundary_bytes, body_start)
                                if next_b < 0: break
                                body = buf[body_start:next_b]
                                buf = buf[next_b:]
                            try:
                                arr = np.frombuffer(body, dtype=np.uint8)
                                img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
                                if img is not None:
                                    with self._lock:
                                        self._latest = img
                            except Exception:
                                pass
            except Exception:
                time.sleep(0.5)

    def read(self) -> Optional[np.ndarray]:
        with self._lock:
            return self._latest

    def stop(self):
        self._stopped.set()
        try: self._t.join(timeout=0.5)
        except Exception: pass

class AutoGrabber:
    def __init__(self, url: str):
        self.url = url
        self.impl = None
        self.kind = "unknown"
        try:
            self.impl = LatestFrameGrabber(url); self.kind = "opencv"
        except Exception:
            self.impl = MJPEGGrabber(url); self.kind = "mjpeg"
    def read(self) -> Optional[np.ndarray]:
        return None if not self.impl else self.impl.read()
    def stop(self):
        if self.impl:
            try: self.impl.stop()
            except Exception: pass

# helpers
def resize_keep_aspect(img: np.ndarray, max_w: int) -> Tuple[np.ndarray, float]:
    if max_w <= 0: return img, 1.0
    h, w = img.shape[:2]
    if w <= max_w: return img, 1.0
    new_w = max_w
    new_h = int(round(h * (new_w / w)))
    out = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
    scale = w / float(new_w)
    return out, scale

def draw_polys(img: np.ndarray, polys: List[np.ndarray], color=(0, 255, 0)):
    if not polys: return
    for p in polys:
        pts = np.asarray(p, dtype=np.int32).reshape(-1, 2)
        cv2.polylines(img, [pts], True, color, 2, cv2.LINE_AA)

# ──────────────────────────────────────────────────────────────────────
# 8) encoding + utils
# ──────────────────────────────────────────────────────────────────────
def _encode_jpeg_bgr(bgr: np.ndarray, quality: int = 65) -> Tuple[Optional[str], int, int, int]:
    try:
        ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok: return None, 0, 0, 0
        raw = buf.tobytes()
        h, w = bgr.shape[:2]
        return base64.b64encode(raw).decode("ascii"), len(raw), w, h
    except Exception:
        return None, 0, 0, 0

def _encode_png_gray8(gray8: np.ndarray) -> Tuple[Optional[str], int, int, int]:
    try:
        ok, buf = cv2.imencode(".png", gray8)
        if not ok: return None, 0, 0, 0
        raw = buf.tobytes()
        return base64.b64encode(raw).decode("ascii"), len(raw), gray8.shape[1], gray8.shape[0]
    except Exception:
        return None, 0, 0, 0

def _to_gray8(img: np.ndarray) -> Optional[np.ndarray]:
    if img is None: return None
    if len(img.shape) == 2:
        if img.dtype == np.uint16:
            m = int(img.max()) or 1
            return (img.astype(np.float32) * (255.0 / m)).astype(np.uint8)
        if img.dtype != np.uint8:
            return cv2.convertScaleAbs(img)
        return img
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

def _u16_to_u8_auto(d16: np.ndarray, lo_p=0.01, hi_p=0.99) -> np.ndarray:
    v = d16[d16 > 0]
    if v.size < 16:
        m = int(d16.max()) or 1
        return (d16.astype(np.float32) * (255.0 / m)).astype(np.uint8)
    lo = np.quantile(v, lo_p); hi = np.quantile(v, hi_p)
    if hi <= lo: hi = lo + 1
    u8 = np.clip((d16 - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)
    return u8

def _fit_b64_jpeg_by_res(bgr: np.ndarray, quality: int, max_b64: int, min_wh: int = 64) -> Tuple[Optional[str], int, int, int]:
    img = bgr
    while True:
        b64, raw_bytes, w, h = _encode_jpeg_bgr(img, quality=quality)
        if not b64: return None,0,0,0
        if len(b64) <= max_b64 or min(w,h) <= min_wh:
            return b64, raw_bytes, w, h
        nw = max(min_wh, int(img.shape[1] * 0.85))
        nh = max(min_wh, int(img.shape[0] * 0.85))
        if nw == img.shape[1] and nh == img.shape[0]:
            return b64, raw_bytes, w, h
        img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)

def _fit_b64_png_by_res(gray8: np.ndarray, max_b64: int, min_wh: int = 64) -> Tuple[Optional[str], int, int, int]:
    img = gray8
    while True:
        b64, raw_bytes, w, h = _encode_png_gray8(img)
        if not b64: return None,0,0,0
        if len(b64) <= max_b64 or min(w,h) <= min_wh:
            return b64, raw_bytes, w, h
        nw = max(min_wh, int(img.shape[1] * 0.85))
        nh = max(min_wh, int(img.shape[0] * 0.85))
        if nw == img.shape[1] and nh == img.shape[0]:
            return b64, raw_bytes, w, h
        img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)

def grant_for(client_pub_hex: str, scopes_list: List[str], exp_unix: int) -> dict:
    token_body = {"v":1,"sub":client_pub_hex,"scopes":scopes_list,"exp":int(exp_unix),
                  "device": state.get("client_address") or f"device.{DEVICE_PUBHEX}", "rc": REV_COUNTER}
    token = sign_token(token_body)
    return {"token": token, "exp": token_body["exp"], "scopes": token_body["scopes"], "device": token_body["device"]}

# ──────────────────────────────────────────────────────────────────────
# 9) inbound handling (topic + DM), presence, serial, and /res logging
# ──────────────────────────────────────────────────────────────────────
def _ack(addr: str, ok: bool, note: str):
    _dm(addr, {"event": "ack", "ok": bool(ok), "note": note})

def _mark_online(addr: str):
    st = _ensure_peer(addr)
    st["online"] = True
    st["last_online"] = time.time()

def send_stats_now(addr: str, st: Dict[str, Any], *, last_bytes: int = 0, w: int = 0, h: int = 0):
    now = time.time()
    tick_dt = max(1e-6, now - st.get("last_tick_ts", now))
    fps_out = st.get("sent_since_tick", 0) / tick_dt
    msg = {"event":"stats","fps_out":round(fps_out,2),"jpeg_bytes":int(last_bytes),
           "scale":round(st.get("scale",1.0),3),"w":int(w),"h":int(h),"ts":int(now*1000)}
    _dm(addr, msg)
    st["sent_since_tick"] = 0
    st["last_tick_ts"] = now

def _update_scale(addr: str, scale: float):
    st = _ensure_peer(addr)
    st["scale"] = float(max(0.1, min(1.0, scale)))
    pct = int(round(st["scale"]*100))
    print(f"[res] {addr} set to {pct}%")
    if addr in addresses:
        addresses[addr]["scale"] = float(st["scale"])
        addresses[addr]["last_seen"] = int(time.time())
        _save_addresses(addresses)
    _ack(addr, True, f"resolution set to {pct}%")

def send_stream_info(addr: str, *, force: bool = False):
    st = _ensure_peer(addr)
    now = time.time()
    if not force and (now - st.get("last_info_ts", 0.0) < 5.0):
        return
    info = {
        "event": "streams",
        "uuid": DEVICE_UUID,
        "color": { "mode": "dm" + ("+topic" if ENABLE_TOPICS else ""), "event": "frame-color", "format": "jpeg", "hz": COLOR_HZ, "quality": JPEG_QUALITY, "topic": COLOR_TOPIC },
        "rgb_url": COLOR_URL or "",
    }
    if HAS_DEPTH:
        info["depth"] = { "mode": "dm" + ("+topic" if ENABLE_TOPICS else ""), "event": "frame-depth", "format": "png", "hz": DEPTH_HZ, "scale": DEPTH_SCALE, "topic": DEPTH_TOPIC }
        info["depth_url"] = DEPTH_URL
    _dm(addr, info)
    st["last_info_ts"] = now
    print(f"[streams] announced to {addr} (color Hz={COLOR_HZ}, depth Hz={DEPTH_HZ if HAS_DEPTH else 0})")

def verify_invite_sig_v1(client_addr: str, v: str, scopes: str, exp: str, nonce_b64url: str, sig_b64url: str) -> Tuple[bool, Optional[str]]:
    s = (client_addr or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", s):
        pubhex = s.lower()
    else:
        m = re.fullmatch(r"[A-Za-z0-9_-]+\.([0-9a-fA-F]{64})", s)
        pubhex = m.group(1).lower() if m else None
    if not pubhex: return False, None
    try:
        vk = VerifyKey(bytes.fromhex(pubhex))
        canonical = f"{v}|{client_addr}|{scopes}|{exp}|{nonce_b64url}".encode("utf-8")
        sig = b64url_decode(sig_b64url)
        vk.verify(canonical, sig)
        return True, pubhex
    except Exception:
        return False, None

def verify_invite_sig_v2(a_b64u: str, s_short: str, e36: str, n_b64u: str, g_b64u: str) -> Tuple[bool, Optional[str]]:
    try:
        pubkey = b64url_decode(a_b64u)
        if len(pubkey) != 32: return False, None
        sig = b64url_decode(g_b64u)
        if len(sig) != 64: return False, None
        canonical = f"2|{a_b64u}|{s_short}|{e36}|{n_b64u}".encode("utf-8")
        VerifyKey(pubkey).verify(canonical, sig)
        return True, pubkey.hex()
    except Exception:
        return False, None

SCOPE_MAP = {'r':'video:rgb','d':'video:depth','m':'audio:mic','s':'audio:speaker','p':'control:ptz','c':'control:robot'}
def scopes_from_short(s_short: str) -> List[str]:
    out: List[str] = []
    for ch in (s_short or ''):
        full = SCOPE_MAP.get(ch)
        if full and full not in out: out.append(full)
    if not out: out = ['video:rgb']
    return out

def process_qr_payload(txt: str) -> Tuple[bool, str, Optional[str], List[str], int]:
    try:
        if not txt.startswith("nkn+invite:"):
            return False, "QR not an NKN invite", None, [], 0
        qs = txt[len("nkn+invite:"):]
        params = urllib.parse.parse_qs(qs, keep_blank_values=True)
        get = lambda k: (params.get(k,[""])[0] or "").strip()
        v = get("v") or "1"

        # v2 compact
        if v == "2" or get("a"):
            a  = get("a"); s = get("s"); e = get("e"); n = get("n"); g = get("g")
            ident = get("i") or "client"
            if not (a and s and e and n and g):
                return False, "Invite v2 missing required fields", None, [], 0
            ok, client_pub_hex = verify_invite_sig_v2(a, s, e, n, g)
            if not ok or not client_pub_hex:
                return False, "Bad v2 invite signature", None, [], 0
            try: exp_unix = int(e, 36)
            except Exception: return False, "Invalid v2 expiry", None, [], 0
            if exp_unix < int(time.time()) - 2:
                return False, "Invite expired", None, [], 0
            scopes_list = scopes_from_short(s)
            dest_addr = f"{ident}.{client_pub_hex}"
            grant = grant_for(client_pub_hex, scopes_list, exp_unix)
            _dm(dest_addr, {"v":2,"type":"grant","grant":grant}, tries=2)
            _persist_peer(dest_addr, ident, grant["scopes"], grant["exp"], grant["device"], scale=1.0)
            _dm(dest_addr, {"event":"hello","from": state.get("client_address"), "uuid": DEVICE_UUID})
            send_stream_info(dest_addr, force=True)
            return True, f"GRANTED (v2) to {dest_addr}", dest_addr, scopes_list, exp_unix

        # v1 legacy
        caddr  = get("clientAddr")
        label  = urllib.parse.unquote(get("label"))
        scopes_csv = get("scopes") or ""
        exp    = get("exp") or "0"
        nonce  = get("nonce")
        sig    = get("sig")
        ident  = get("i") or "client"

        if not (caddr and scopes_csv and exp and nonce and sig):
            return False, "Invite v1 missing required fields", None, [], 0
        ok_v1, client_pub_hex = verify_invite_sig_v1(caddr, v, scopes_csv, exp, nonce, sig)
        if not ok_v1 or not client_pub_hex:
            return False, "Bad v1 invite signature", None, [], 0
        scopes_list = [s.strip() for s in scopes_csv.split(",") if s.strip()]
        dest_addr = caddr.strip() if re.fullmatch(r"[A-Za-z0-9_-]+\.([0-9a-fA-F]{64})", (caddr or "").strip()) else f"{ident}.{client_pub_hex}"
        exp_unix = int(exp)
        grant = grant_for(client_pub_hex, scopes_list, exp_unix)
        _dm(dest_addr, {"v":1,"type":"grant","grant":grant}, tries=2)
        _persist_peer(dest_addr, label or ident, grant["scopes"], grant["exp"], grant["device"], scale=1.0)
        _dm(dest_addr, {"event":"hello","from": state.get("client_address"), "uuid": DEVICE_UUID})
        send_stream_info(dest_addr, force=True)
        return True, f"GRANTED (v1) to {dest_addr}", dest_addr, scopes_list, exp_unix

    except Exception as e:
        return False, f"QR error: {e}", None, [], 0

def handle_inbound(src_addr: str, payload: Any, *, topic: Optional[str] = None):
    if not src_addr: return
    _mark_online(src_addr)

    cmd = ""
    body = payload if isinstance(payload, dict) else {}
    if isinstance(payload, str):
        cmd = payload.strip()
    elif isinstance(payload, dict):
        cmd = (payload.get("cmd") or payload.get("data") or payload.get("raw") or "").strip()

    # Presence
    if isinstance(body, dict) and body.get("event") == "ping":
        _dm(src_addr, {"event":"hello","from": state.get("client_address"), "uuid": DEVICE_UUID})
        send_stream_info(src_addr); return
    if isinstance(body, dict) and body.get("event") == "hello":
        _dm(src_addr, {"event":"hello-ack","uuid": DEVICE_UUID})
        send_stream_info(src_addr); return

    # Orientation "Center" → HOME
    if isinstance(body, dict) and str(body.get("event","")).lower() in ("orient","orientation","orientation-press"):
        key = str(body.get("key") or body.get("btn") or body.get("button") or "").lower()
        if key in ("center","centre","middle","reset","home"):
            ok = _serial_send("HOME"); _ack(src_addr, ok, "center->HOME" if ok else "no serial"); return

    # Topic filter: only our cmd topic
    if topic and topic != state.get("cmd_topic"):
        return

    # /res
    if cmd.lower().startswith("/res"):
        parts = cmd.split()
        if len(parts) >= 2:
            p = parts[1].rstrip("%")
            try:
                pct = float(p); _update_scale(src_addr, pct/100.0); return
            except Exception:
                pass
        _ack(src_addr, False, "usage: /res <percent>"); return

    # /stats
    if cmd.lower().startswith("/stats"):
        st = _ensure_peer(src_addr)
        toks = cmd.split()
        if len(toks) == 1:
            _ack(src_addr, True, f"stats={'on' if st['stats_on'] else 'off'} hz={st['stats_hz']}"); return
        arg = toks[1].lower()
        if arg in ("on","off"):
            st["stats_on"] = (arg == "on"); _ack(src_addr, True, f"stats {arg}"); return
        if arg == "hz" and len(toks) >= 3:
            try:
                hz = max(1, min(10, int(toks[2]))); st["stats_hz"] = hz; _ack(src_addr, True, f"stats hz={hz}")
            except Exception:
                _ack(src_addr, False, "usage: /stats hz <1..10>")
            return
        if arg == "once": send_stats_now(src_addr, st); return
        _ack(src_addr, False, "usage: /stats on|off | /stats hz <n> | /stats once"); return

    if cmd.lower() in ("/ping",):
        _dm(src_addr, {"event":"hello","from": state.get("client_address"), "uuid": DEVICE_UUID}); _ack(src_addr, True, "pong"); send_stream_info(src_addr); return

    if cmd.lower() in ("/home","home"):
        ok = _serial_send("HOME"); _ack(src_addr, ok, "HOME sent" if ok else "no serial"); return

    if cmd.lower() in ("/center","center"):
        ok = _serial_send("HOME"); _ack(src_addr, ok, "HOME sent" if ok else "no serial"); return

    if cmd and _validate_axes_cmd(cmd):
        full = _merge_axes_cmd(cmd); ok = _serial_send(full); _ack(src_addr, ok, f"serial: {full}" if ok else "no serial"); return

    if cmd: _ack(src_addr, False, "unknown command")

# ──────────────────────────────────────────────────────────────────────
# 10) main: QR + stream both channels with symmetric pacing + live depth window
# ──────────────────────────────────────────────────────────────────────
def run():
    global HAS_DEPTH
    # wait NKN ready
    t0 = time.time()
    while not state.get("client_address"):
        if time.time() - t0 > 20:
            print("NKN not ready, exiting."); _shutdown()
        time.sleep(0.02)

    # restore peers
    if addresses:
        for addr, meta in addresses.items():
            _ensure_peer(addr, default_scale=float(meta.get("scale",1.0)))
        print(f"[peers] restored {len(addresses)} from addresses.json")

    # Capture
    print(f"[video] opening: {COLOR_URL}")
    color_grabber = AutoGrabber(COLOR_URL)
    print(f"[video] Using {color_grabber.kind.upper()} stream")

    depth_grabber = None
    HAS_DEPTH = False
    if DEPTH_URL:
        print(f"[depth] opening: {DEPTH_URL}")
        try:
            depth_grabber = AutoGrabber(DEPTH_URL)
            HAS_DEPTH = True
            print(f"[depth] Using {depth_grabber.kind.upper()} stream")
            for addr in list(addresses.keys()):
                send_stream_info(addr, force=True)
        except Exception as e:
            print("[depth] failed to open depth stream:", e)
            depth_grabber = None
            HAS_DEPTH = False

    # QR from color feed
    detector = cv2.QRCodeDetector()
    win_color = "Color / QR"
    win_depth = "Depth Preview"
    try:
        cv2.namedWindow(win_color, cv2.WINDOW_NORMAL)
        if HAS_DEPTH: cv2.namedWindow(win_depth, cv2.WINDOW_NORMAL)
    except Exception:
        pass

    frames = 0
    last_ts = time.perf_counter()
    fps = 0.0

    d_frames = 0
    d_last_ts = time.perf_counter()
    d_fps = 0.0

    color_period = 1.0 / float(COLOR_HZ)
    depth_period = 1.0 / float(DEPTH_HZ if HAS_DEPTH else 1)
    PER_PEER_MIN_INTERVAL_COLOR = max(0.5 * color_period, 1.0/60.0)
    PER_PEER_MIN_INTERVAL_DEPTH = max(0.5 * depth_period, 1.0/60.0)
    last_color_batch = 0.0
    last_depth_batch = 0.0
    seq_color = 0
    seq_depth = 0
    seen_invites = set()

    try:
        while True:
            frame = color_grabber.read()
            if frame is None:
                time.sleep(0.005)

            # ---- QR + color preview window ----
            if frame is not None:
                work, scale = resize_keep_aspect(frame, SCAN_MAX_WIDTH) if SCAN_MAX_WIDTH > 0 else (frame, 1.0)
                gray_for_qr = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)

                decoded_strings: List[str] = []
                polys_full: List[np.ndarray] = []

                try:
                    retval, decoded_info, pts, _ = detector.detectAndDecodeMulti(gray_for_qr)
                    if pts is not None and len(pts):
                        for poly in pts:
                            p = (np.asarray(poly).reshape(-1, 2) * scale).astype(np.float32)
                            polys_full.append(p)
                    if retval and decoded_info:
                        decoded_strings.extend([s for s in decoded_info if s])
                except Exception:
                    pass

                if not decoded_strings:
                    try:
                        txt, pts, _ = detector.detectAndDecode(gray_for_qr)
                        if pts is not None and len(pts):
                            p = (np.asarray(pts).reshape(-1, 2) * scale).astype(np.float32)
                            polys_full.append(p)
                        if txt:
                            decoded_strings.append(txt)
                    except Exception:
                        pass

                for s in decoded_strings:
                    if s in seen_invites: continue
                    ok, msg, dest_addr, scopes, exp = process_qr_payload(s)
                    if ok and dest_addr: print(msg)
                    seen_invites.add(s)

                try:
                    vis = frame.copy()
                    draw_polys(vis, polys_full, color=(0, 255, 0))
                    frames += 1
                    nowp = time.perf_counter()
                    if nowp - last_ts >= 1.0:
                        fps = frames / (nowp - last_ts); frames = 0; last_ts = nowp
                    cv2.putText(vis, f"{fps:.1f} fps", (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,0), 2, cv2.LINE_AA)
                    cv2.putText(vis, f"{DEVICE_LABEL}  uuid={DEVICE_UUID}", (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,0), 2, cv2.LINE_AA)
                    cv2.imshow(win_color, vis)
                    key = cv2.waitKey(1) & 0xFF
                    if key == 27 or key == ord('q'):
                        break
                except Exception:
                    pass

            # ---- Presence expiry ----
            if peers:
                tnow = time.time()
                for addr, st in peers.items():
                    if st["online"] and (tnow - st["last_online"] > PRESENCE_TTL):
                        st["online"] = False

                # ---- COLOR send ----
                if frame is not None and (tnow - last_color_batch >= color_period):
                    last_color_batch = tnow
                    color_base = frame
                    scale_groups: Dict[float, List[str]] = {}
                    for addr, st in peers.items():
                        if not st["online"]: continue
                        if tnow - st.get("last_color_ts", 0.0) < PER_PEER_MIN_INTERVAL_COLOR:
                            continue
                        sc = float(st.get("scale", 1.0))
                        scale_groups.setdefault(sc, []).append(addr)

                    for sc, addrs in scale_groups.items():
                        if abs(sc - 1.0) > 1e-3:
                            w = max(2, int(color_base.shape[1] * sc))
                            h = max(2, int(color_base.shape[0] * sc))
                            color_scaled = cv2.resize(color_base, (w, h), interpolation=cv2.INTER_AREA)
                        else:
                            color_scaled = color_base
                        b64, raw_bytes, w, h = _fit_b64_jpeg_by_res(color_scaled, JPEG_QUALITY, MAX_B64)
                        if not b64: continue
                        seq_color += 1
                        payload = {"event":"frame-color","uuid":DEVICE_UUID,"data":b64,"w":w,"h":h,"seq":seq_color,"ts":int(tnow*1000)}
                        for addr in addrs:
                            _dm(addr, payload, tries=1)
                            st = _ensure_peer(addr)
                            st["last_color_ts"] = tnow
                            st["sent_since_tick"] = st.get("sent_since_tick", 0) + 1
                            if st.get("stats_on", False):
                                per = 1.0 / max(1, int(st.get("stats_hz", 1)))
                                if tnow - st.get("last_stats_ts", 0.0) >= per:
                                    send_stats_now(addr, st, last_bytes=raw_bytes, w=w, h=h)
                                    st["last_stats_ts"] = tnow
                        _pub(COLOR_TOPIC, payload)

                # ---- DEPTH send + depth preview window ----
                if HAS_DEPTH and depth_grabber and (tnow - last_depth_batch >= depth_period):
                    last_depth_batch = tnow
                    dframe = depth_grabber.read()
                    if dframe is not None:
                        if dframe.ndim == 2 and dframe.dtype == np.uint16:
                            g8 = _u16_to_u8_auto(dframe)
                        else:
                            g8 = _to_gray8(dframe)
                        if g8 is not None:
                            if DEPTH_SCALE and abs(DEPTH_SCALE - 1.0) > 1e-3:
                                w = max(2, int(g8.shape[1] * DEPTH_SCALE))
                                h = max(2, int(g8.shape[0] * DEPTH_SCALE))
                                g8s = cv2.resize(g8, (w, h), interpolation=cv2.INTER_AREA)
                            else:
                                g8s = g8
                                w, h = g8s.shape[1], g8s.shape[0]

                            # show live depth (grayscale)
                            try:
                                visd = cv2.cvtColor(g8s, cv2.COLOR_GRAY2BGR)
                                d_frames += 1
                                nowd = time.perf_counter()
                                if nowd - d_last_ts >= 1.0:
                                    d_fps = d_frames / (nowd - d_last_ts); d_frames = 0; d_last_ts = nowd
                                cv2.putText(visd, f"{d_fps:.1f} fps  {w}x{h}", (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,255), 2, cv2.LINE_AA)
                                cv2.imshow(win_depth, visd)
                                # no extra waitKey; the earlier call services events
                            except Exception:
                                pass

                            b64p, raw_bytes, w, h = _fit_b64_png_by_res(g8s, MAX_B64)
                            if b64p:
                                seq_depth += 1
                                payload_d = {"event":"frame-depth","uuid":DEVICE_UUID,"data":b64p,"w":w,"h":h,"seq":seq_depth,"ts":int(tnow*1000)}
                                for addr, st in peers.items():
                                    if not st["online"]: continue
                                    if tnow - st.get("last_depth_ts", 0.0) < PER_PEER_MIN_INTERVAL_DEPTH:
                                        continue
                                    _dm(addr, payload_d, tries=1)
                                    st["last_depth_ts"] = tnow
                                _pub(DEPTH_TOPIC, payload_d)

    finally:
        try: color_grabber.stop()
        except Exception: pass
        try:
            if depth_grabber: depth_grabber.stop()
        except Exception: pass
        try: cv2.destroyAllWindows()
        except Exception: pass

# ──────────────────────────────────────────────────────────────────────
# 11) main
# ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    print("→ Launching NKN device bridge …")
    run()