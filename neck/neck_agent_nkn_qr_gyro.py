#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NKN device — unified multi-stream pipeline (QR from /camera/rs_color, streams default to /video)

WHAT'S NEW (frontend-compat + shorter QR):
- Accepts SHORT INVITES: just the client's public key (base64url 43 chars), also accepts 64-hex or "identifier.hex".
- Still accepts legacy "nkn+invite:" v1/v2.
- On short invite, grants default scopes (configurable) and default expiry (configurable), DMs grant to "client.<pubhex>".
- Emits frames with events your frontend expects:
    * Color  → {"event":"frame-color", "data":"<jpeg b64>", ...}
    * Depth  → {"event":"frame-depth", "data":"<png  b64>", ...}
- The periodic "streams" message now also includes a summary:
    {"event":"streams", "color":{"topic":...}, "depth":{"topic":...}, ...}

Other behavior preserved:
- Baseline camera/rs_color runs for QR scanning; green overlay boxes for detected invites also rendered in color frames.
- Commands: get/stop streams, /res N%, /stats on|off|hz, /home & serial passthrough with validation.
"""

# ──────────────────────────────────────────────────────────────────────
# 0) venv bootstrap (stdlib)
# ──────────────────────────────────────────────────────────────────────
import os, sys, subprocess, json, time, threading, base64, re, signal, shutil
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
from typing import Optional, Dict, Any, Tuple, List, Set
import urllib.parse
import requests
import numpy as np
import cv2
import secrets
import uuid as _uuid
from subprocess import Popen, PIPE
from nacl.signing import SigningKey, VerifyKey
import serial  # pyserial

def _log(*a):
    try: print(*a, flush=True)
    except Exception: pass

# ──────────────────────────────────────────────────────────────────────
# 2) args & .env
# ──────────────────────────────────────────────────────────────────────
cli = argparse.ArgumentParser()
cli.add_argument("--video-url", default=os.environ.get("VIDEO_URL", "http://127.0.0.1:8080/video/rs_color"))
cli.add_argument("--color-url", default=os.environ.get("COLOR_URL", None))
cli.add_argument("--depth-url", default=os.environ.get("DEPTH_URL", ""))  # legacy hint; discovery will also find it
cli.add_argument("--stream-hz", type=int, default=int(os.environ.get("STREAM_HZ","24")))
cli.add_argument("--camera-hz", type=int, default=int(os.environ.get("CAMERA_HZ","10")))
cli.add_argument("--scan-max-width", type=int, default=int(os.environ.get("SCAN_MAX_WIDTH", "960")))
cli.add_argument("--label", default=os.environ.get("DEVICE_LABEL","neck-agent"))
cli.add_argument("--uuid", default=os.environ.get("DEVICE_UUID",""))
cli.add_argument("--subclients", type=int, default=int(os.environ.get("SUBCLIENTS","6")))
cli.add_argument("--presence-ttl", type=float, default=float(os.environ.get("PRESENCE_TTL","20")))
cli.add_argument("--enable-topics", type=int, default=int(os.environ.get("ENABLE_TOPICS","1")))
cli.add_argument("--cmd-topic-strategy", default=os.environ.get("CMD_TOPIC_STRATEGY","uuid"), choices=["uuid","pubhex"])
cli.add_argument("--cmd-topic-duration", type=int, default=int(os.environ.get("CMD_TOPIC_DURATION","4320")))
cli.add_argument("--jpeg-quality", type=int, default=int(os.environ.get("JPEG_QUALITY","65")))
cli.add_argument("--max-b64", type=int, default=int(os.environ.get("MAX_B64","900000")))
# include IR candidates and depth by default
cli.add_argument("--stream-candidates", default=os.environ.get("STREAM_CANDIDATES","rs_color,rs_depth,rs_ir_left,rs_ir_right"))
cli.add_argument("--camera-base", default=os.environ.get("CAMERA_BASE",""))
cli.add_argument("--video-base",  default=os.environ.get("VIDEO_BASE",""))
# baseline for QR lives on camera
cli.add_argument("--always-on",   default=os.environ.get("ALWAYS_ON","camera:rs_color"))

# NEW: Defaults for short-invite grants
cli.add_argument("--default-scopes-short", default=os.environ.get("DEFAULT_SCOPES_SHORT","rd"))   # r=color rgb, d=depth
cli.add_argument("--default-invite-min", type=int, default=int(os.environ.get("DEFAULT_INV_MIN","60")))
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
if "DEVICE_SEED_HEX" not in dotenv: dotenv["DEVICE_SEED_HEX"] = secrets.token_hex(32)
if "DEVICE_TOPIC_PREFIX" not in dotenv: dotenv["DEVICE_TOPIC_PREFIX"] = "roko-signaling"
if "REV_COUNTER" not in dotenv: dotenv["REV_COUNTER"] = "0"

# Normalize legacy hints
color_url = args.color_url or args.video_url or dotenv.get("COLOR_URL") or dotenv.get("VIDEO_URL") or "http://127.0.0.1:8080/video/rs_color"

dotenv["VIDEO_URL"]      = color_url
dotenv["COLOR_URL"]      = color_url
dotenv["DEPTH_URL"]      = args.depth_url or dotenv.get("DEPTH_URL","")
dotenv["STREAM_HZ"]      = str(max(1, min(60, int(args.stream_hz))))
dotenv["CAMERA_HZ"]      = str(max(1, min(60, int(args.camera_hz))))
dotenv["SCAN_MAX_WIDTH"] = str(max(0, min(2560, int(args.scan_max_width))))
dotenv["DEVICE_UUID"]    = args.uuid or dotenv.get("DEVICE_UUID") or str(_uuid.uuid4())
dotenv["SUBCLIENTS"]     = str(max(1, min(16, int(args.subclients))))
dotenv["PRESENCE_TTL"]   = str(max(5, int(args.presence_ttl)))
dotenv["ENABLE_TOPICS"]  = "1" if int(args.enable_topics) else "0"
dotenv["CMD_TOPIC_STRATEGY"] = args.cmd_topic_strategy
dotenv["CMD_TOPIC_DURATION"] = str(max(60, int(args.cmd_topic_duration)))
dotenv["JPEG_QUALITY"]   = str(max(30, min(95, int(args.jpeg_quality))))
dotenv["MAX_B64"]        = str(max(200000, int(args.max_b64)))
dotenv["STREAM_CANDIDATES"] = args.stream_candidates
dotenv["CAMERA_BASE"]    = args.camera_base or dotenv.get("CAMERA_BASE","")
dotenv["VIDEO_BASE"]     = args.video_base  or dotenv.get("VIDEO_BASE","")
# 👇 baseline lives on /camera for QR
dotenv["ALWAYS_ON"]      = args.always_on

# NEW defaults for short-invite
dotenv["DEFAULT_SCOPES_SHORT"] = args.default_scopes_short
dotenv["DEFAULT_INV_MIN"]      = str(max(1, int(args.default_invite_min)))

_save_env(ENV_PATH, dotenv)

DEVICE_SEED_HEX = dotenv["DEVICE_SEED_HEX"].lower().replace("0x","")
TOPIC_PREFIX    = dotenv["DEVICE_TOPIC_PREFIX"]
REV_COUNTER     = int(dotenv["REV_COUNTER"])
STREAM_HZ       = max(1, min(60, int(dotenv["STREAM_HZ"])))
CAMERA_HZ       = max(1, min(60, int(dotenv["CAMERA_HZ"])))
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
MAX_B64         = max(200000, int(dotenv.get("MAX_B64","900000")))
STREAM_CANDIDATES = [s.strip() for s in dotenv["STREAM_CANDIDATES"].split(",") if s.strip()]
CAMERA_BASE     = dotenv["CAMERA_BASE"] or "http://127.0.0.1:8080"
VIDEO_BASE      = dotenv["VIDEO_BASE"]  or "http://127.0.0.1:8080"
ALWAYS_ON_CONF  = [s.strip() for s in (dotenv.get("ALWAYS_ON","camera:rs_color") or "").split(",") if s.strip()]

DEFAULT_SCOPES_SHORT = (dotenv.get("DEFAULT_SCOPES_SHORT","rd") or "rd")
DEFAULT_INV_MIN      = max(1, int(dotenv.get("DEFAULT_INV_MIN","60")))

def _derive_base(url: str) -> str:
    try:
        from urllib.parse import urlparse
        u = urlparse(url)
        if not u.scheme or not u.netloc: return ""
        return f"{u.scheme}://{u.netloc}"
    except Exception:
        return ""

if not dotenv["CAMERA_BASE"] and dotenv.get("DEPTH_URL"):
    CAMERA_BASE = _derive_base(dotenv["DEPTH_URL"]) or CAMERA_BASE
if not dotenv["VIDEO_BASE"] and dotenv.get("VIDEO_URL"):
    VIDEO_BASE = _derive_base(dotenv["VIDEO_URL"]) or VIDEO_BASE

# ──────────────────────────────────────────────────────────────────────
# 3) NKN bridge (Node sidecar, packet-friendly)
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
    _log("→ Initializing device-bridge …")
    subprocess.check_call([NPM_BIN, "init", "-y"], cwd=NODE_DIR, stdout=subprocess.DEVNULL)
    subprocess.check_call([NPM_BIN, "install", "nkn-sdk@^1.3.6"], cwd=NODE_DIR, stdout=subprocess.DEVNULL)

BRIDGE_SRC = r"""
/* nkn_device_bridge.js — MultiClient DM + optional topic subscribe/publish.
   Fire-and-forget path: if outbound has {no_ack:1}, we don't block on ACKs (we still use client.send()).
   Inbound: if {no_ack:1}, return false to suppress SDK ACK.
*/
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

function sendToPy(obj){ try{ process.stdout.write(JSON.stringify(obj) + '\n'); }catch(_){} }
function isFullAddr(s){ return typeof s === 'string' && /^[A-Za-z0-9_-]+\.[0-9a-f]{64}$/i.test((s||'').trim()); }
function isHex64(s){ return typeof s === 'string' && /^[0-9a-f]{64}$/i.test((s||'').trim()); }
function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }

async function sendDMReliable(client, dest, data, tries=2){
  if (!isFullAddr(dest) && !isHex64(dest)) return false;
  const body = JSON.stringify(data);
  const t = Math.max(1, Math.min(3, parseInt(tries || 2, 10)));
  for (let i=0;i<t;i++){
    try { await client.send(dest, body); return true; }
    catch(e){ await sleep(20); }
  }
  return false;
}

function sendDMFire(client, dest, data){
  if (!isFullAddr(dest) && !isHex64(dest)) return false;
  try { client.send(dest, JSON.stringify(data)).catch(()=>{}); } catch(_) {}
  return true;
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
      wallet = new nkn.Wallet({ seed: WALLET_SEED, rpcServerAddr: RPC || undefined });
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
    if (msg && (msg.no_ack === 1 || msg.no_ack === true)) return false;
  });

  const rl = readline.createInterface({ input: process.stdin });
  rl.on('line', async (line) => {
    if (!line) return;
    let cmd; try { cmd = JSON.parse(line); } catch { return; }
    if (cmd.type === 'dm') {
      if (cmd.fire) {
        sendDMFire(client, String(cmd.to||'').trim(), cmd.data);
      } else {
        await sendDMReliable(client, String(cmd.to||'').trim(), cmd.data, cmd.tries || 2);
      }
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
import base64 as _b64
def b64url_encode(b: bytes) -> str: return _b64.urlsafe_b64encode(b).decode("ascii").rstrip("=")
def b64url_decode(s: str) -> bytes:
    s = (s or "").strip().replace(" ", "+")
    pad = '=' * ((4 - len(s) % 4) % 4)
    return _b64.urlsafe_b64decode(s + pad)

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
        _log("bridge send error:", e)

def _dm(dest: str, data: dict, tries: int = 2, *, fire: bool = False):
    _bridge_send({"type":"dm","to":dest,"data":data,"tries":tries,"fire": bool(fire)})

def _pub(topic: str, data: dict):
    if ENABLE_TOPICS:
        _bridge_send({"type":"pub","topic":topic,"data":data})

def _shutdown(*_):
    try:
        if bridge and bridge.poll() is None:
            bridge.terminate()
    except Exception: pass
    try: cv2.destroyAllWindows()
    except Exception: pass
    try:
        if ser is not None and ser.is_open: ser.close()
    except Exception: pass
    sys.exit(0)

def _bridge_reader():
    for line in bridge.stdout:
        line = (line or "").strip()
        if not line: continue
        try: msg = json.loads(line)
        except Exception: continue
        t = msg.get("type")
        if t == "ready":
            state["client_address"] = msg.get("address")
            state["topic_prefix"]   = msg.get("topicPrefix") or TOPIC_PREFIX
            state["subs"]           = int(msg.get("subs") or SUBCLIENTS)
            _log(f"→ NKN ready: {state['client_address']}  subs={state['subs']}")
            for addr in list(addresses.keys()):
                _dm(addr, {"event":"hello","from": state["client_address"], "uuid": DEVICE_UUID}, fire=False)
                send_stream_info(addr, force=True)
        elif t == "topics-ready":
            state["topics_ready"] = True
            state["cmd_topic"]    = msg.get("cmdTopic") or CMD_TOPIC
            _log(f"→ topics: ready, cmd_topic={state['cmd_topic']}, duration={msg.get('duration')}")
        elif t == "topics-error":
            state["topics_ready"] = False
            _log("→ topics: disabled/fallback:", msg.get("error",""))
        elif t == "message":
            src = (msg.get("src") or "").strip()
            payload = msg.get("msg")
            topic = msg.get("topic")
            try:
                handle_inbound(src, payload, topic=topic)
            except Exception as e:
                _log("inbound handler error:", e)

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
            "last_info_ts": 0.0,
            "subs": set(),  # {(type,name)}
        }
        peers[addr] = st
    if "subs" not in st: st["subs"] = set()
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
    if ser is not None and ser.is_open: return
    for p in SER_POSSIBLE:
        try:
            ser = serial.Serial(p, 115200, timeout=1)
            _log("[serial] opened:", p)
            break
        except Exception:
            ser = None
    if ser is None:
        _log("[serial] no device found (commands will still be ACKed)")
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
        _log("[serial] error:", e)
    return False

# ──────────────────────────────────────────────────────────────────────
# 7) capture helpers
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
            return None if self._latest is None else self._latest.copy()

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
        self._latest: Optional[np.ndarray] = None
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
            return None if self._latest is None else self._latest.copy()

    def stop(self):
        self._stopped.set()
        try: self._t.join(timeout=0.5)
        except Exception: pass

class SingleImagePoller:
    """Polls a single-image HTTP endpoint (e.g., /camera/rs_depth) at a fixed rate."""
    def __init__(self, url: str, hz: int = 10, timeout=6.0):
        self.url = url
        self.period = 1.0 / max(1, hz)
        self.timeout = timeout
        self._lock = threading.Lock()
        self._latest: Optional[np.ndarray] = None
        self._stopped = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self):
        sess = requests.Session()
        headers = {"Cache-Control": "no-cache", "Pragma": "no-cache", "Accept": "image/*"}
        while not self._stopped.is_set():
            t0 = time.time()
            try:
                resp = sess.get(self.url, headers=headers, params={"_ts": int(time.time()*1000)}, timeout=self.timeout)
                if resp.status_code == 200 and resp.content:
                    arr = np.frombuffer(resp.content, dtype=np.uint8)
                    img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)  # preserve 16-bit if present
                    if img is not None:
                        with self._lock:
                            self._latest = img
            except Exception:
                pass
            dt = time.time() - t0
            sleep_left = self.period - dt
            if sleep_left > 0: time.sleep(sleep_left)

    def read(self) -> Optional[np.ndarray]:
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    def stop(self):
        self._stopped.set()
        try: self._t.join(timeout=0.5)
        except Exception: pass

def make_grabber(url: str, hz_for_camera: int, hz_for_video: int):
    u = url.lower()
    try:
        pr = requests.head(url, timeout=3.0)
        ctype = (pr.headers.get("Content-Type","") or "").lower()
    except Exception:
        ctype = ""

    if "/camera/" in u or (ctype.startswith("image/") and "multipart" not in ctype):
        g = SingleImagePoller(url, hz=hz_for_camera); g.kind = "single-image"; return g, "camera"
    if "/video/" in u or "multipart" in ctype:
        try:
            g = MJPEGGrabber(url); g.kind = "mjpeg"; return g, "video"
        except Exception:
            pass
    if "/video/" in u:
        try:
            g = LatestFrameGrabber(url); g.kind = "opencv"; return g, "video"
        except Exception:
            pass
    g = SingleImagePoller(url, hz=hz_for_camera); g.kind = "single-image"; return g, "camera"

# ──────────────────────────────────────────────────────────────────────
# 8) encoding + utils + overlay helpers
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
    if img.ndim == 3 and img.shape[2] == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if img.ndim == 3 and img.shape[2] == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    return None

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

# Overlay state for QR invites (normalized polys; TTL-based)
_qr_overlay_lock = threading.Lock()
_qr_overlay_state = {"ts": 0.0, "polys_norm": []}  # list of [[xN,yN],...] with coords in [0..1]

def _qr_set_overlay(polys_norm: List[List[Tuple[float,float]]]):
    with _qr_overlay_lock:
        _qr_overlay_state["ts"] = time.time()
        _qr_overlay_state["polys_norm"] = polys_norm

def _qr_get_overlay(ttl: float = 2.0) -> List[List[Tuple[float,float]]]:
    with _qr_overlay_lock:
        if time.time() - _qr_overlay_state["ts"] <= ttl:
            return [list(map(tuple, poly)) for poly in _qr_overlay_state["polys_norm"]]
        return []

def _draw_polys_on_bgr(img: np.ndarray, polys_norm: List[List[Tuple[float,float]]], color=(0,255,0)):
    if img is None or not polys_norm: return
    h, w = img.shape[:2]
    for poly in polys_norm:
        if not poly: continue
        pts = np.array([[int(x*w), int(y*h)] for (x,y) in poly], dtype=np.int32)
        cv2.polylines(img, [pts], isClosed=True, color=color, thickness=2, lineType=cv2.LINE_AA)
        p0 = pts[0]
        cv2.putText(img, "INVITE", (p0[0]+4, p0[1]-6), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2, cv2.LINE_AA)

# ──────────────────────────────────────────────────────────────────────
# 9) Stream Registry (+ baseline protection + per-stream dedicated events)
# ──────────────────────────────────────────────────────────────────────
def _simple_event_for(stype: str, name: str) -> str:
    n = (name or "").lower()
    if stype == "camera" and (("depth" in n) or ("ir" in n)):
        return "frame-depth"
    return "frame-color"

def _is_depthlike(stype: str, name: str) -> bool:
    n = (name or "").lower()
    return stype == "camera" and (("depth" in n) or ("ir" in n))

class StreamSource:
    def __init__(self, stype: str, name: str, url: str, hz: int):
        self.stype = stype
        self.name  = name
        self.url   = url
        self.hz    = max(1, min(60, hz))
        self.period= 1.0 / self.hz
        self.grabber, detected_type = make_grabber(url, CAMERA_HZ, STREAM_HZ)
        self.topic = f"{TOPIC_PREFIX}.{self.stype}.{self.name}.{DEVICE_UUID}"
        self.event_full = f"frame-{self.stype}-{self.name}".replace("__","_")
        self.seq   = 0
        self.running = False
        self._t = None
        self.subscribers: Set[str] = set()
        self.kind = getattr(self.grabber, "kind", "unknown")
        self.depthlike = _is_depthlike(self.stype, self.name)

    def start(self):
        if self.running: return
        self.running = True
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()
        _log(f"[stream] started {self.stype}/{self.name} ({self.kind}) -> topic {self.topic}")

    def stop(self):
        self.running = False
        try:
            if self._t and self._t.is_alive():
                self._t.join(timeout=0.5)
        except Exception:
            pass
        try: self.grabber.stop()
        except Exception: pass
        _log(f"[stream] stopped {self.stype}/{self.name}")

    def _loop(self):
        win_name = f"{self.stype}/{self.name}"
        try: cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
        except Exception: pass

        baseline = (self.stype, self.name) in ALWAYS_ON_KEYS and (self.stype, self.name) == BASELINE_COLOR

        # NEW: classify by NAME (not by image shape) so MJPEG "depth" or "ir" doesn’t get treated as RGB
        name_lc = (self.name or "").lower()
        treat_as_depth = ("depth" in name_lc) or ("ir" in name_lc)

        while self.running:
            t0 = time.time()
            frame = self.grabber.read()
            if frame is None:
                time.sleep(0.01)
                continue

            polys = _qr_get_overlay() if baseline else []
            show_preview = baseline or bool(self.subscribers)

            if treat_as_depth:
                # ---------- DEPTH/IR PATH (always encode PNG gray8) ----------
                # Normalize to 8-bit gray regardless of source format
                if frame.ndim == 3 and frame.shape[2] == 3:
                    g8 = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                elif frame.dtype == np.uint16:
                    g8 = _u16_to_u8_auto(frame)
                else:
                    g8 = _to_gray8(frame)

                if g8 is not None and show_preview:
                    try:
                        vis = cv2.cvtColor(g8, cv2.COLOR_GRAY2BGR)
                        if polys:
                            _draw_polys_on_bgr(vis, polys, color=(0,255,0))
                        cv2.putText(vis, f"{self.name} {g8.shape[1]}x{g8.shape[0]} {self.hz}Hz",
                                    (10,24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,255), 2, cv2.LINE_AA)
                        cv2.imshow(win_name, vis); cv2.waitKey(1)
                    except Exception:
                        pass

                if g8 is not None and self.subscribers:
                    b64p, raw_bytes, w, h = _fit_b64_png_by_res(g8, MAX_B64)
                    if b64p:
                        self.seq += 1
                        base = {"uuid": DEVICE_UUID, "data": b64p,
                                "w": w, "h": h, "seq": self.seq, "ts": int(t0*1000),
                                "topic": self.topic, "no_ack": 1}
                        payload_full   = dict(base, event=self.event_full, kind="frame-depth")
                        payload_compat = dict(base, event="frame-depth",  kind="frame-depth")

                        groups: Dict[float, List[str]] = {}
                        for addr in list(self.subscribers):
                            sc = float(_ensure_peer(addr).get("scale", 1.0))
                            groups.setdefault(sc, []).append(addr)

                        for sc, addrs in groups.items():
                            if abs(sc - 1.0) > 1e-3:
                                sw = max(2, int(w * sc)); sh = max(2, int(h * sc))
                                g8s = cv2.resize(g8, (sw, sh), interpolation=cv2.INTER_AREA)
                                b64s, _, sw2, sh2 = _fit_b64_png_by_res(g8s, MAX_B64)
                                if b64s:
                                    base_s = dict(base, data=b64s, w=sw2, h=sh2)
                                    p_full   = dict(base_s, event=self.event_full, kind="frame-depth")
                                    p_compat = dict(base_s, event="frame-depth",  kind="frame-depth")
                                    for addr in addrs:
                                        _dm(addr, p_full,   tries=1, fire=True)
                                        _dm(addr, p_compat, tries=1, fire=True)
                            else:
                                for addr in addrs:
                                    _dm(addr, payload_full,   tries=1, fire=True)
                                    _dm(addr, payload_compat, tries=1, fire=True)

                        if ENABLE_TOPICS: _pub(self.topic, payload_full)

            else:
                # ---------- COLOR PATH (JPEG) ----------
                out = frame if frame.ndim == 3 else cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
                if polys:
                    _draw_polys_on_bgr(out, polys, color=(0,255,0))

                if show_preview:
                    try:
                        vis = out.copy()
                        cv2.putText(vis, f"{self.name} {out.shape[1]}x{out.shape[0]} {self.hz}Hz",
                                    (10,24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,0), 2, cv2.LINE_AA)
                        cv2.imshow(win_name, vis); cv2.waitKey(1)
                    except Exception:
                        pass

                if self.subscribers:
                    b64, raw_bytes, w, h = _fit_b64_jpeg_by_res(out, JPEG_QUALITY, MAX_B64)
                    if b64:
                        self.seq += 1
                        base = {"uuid": DEVICE_UUID, "data": b64,
                                "w": w, "h": h, "seq": self.seq, "ts": int(t0*1000),
                                "topic": self.topic, "no_ack": 1}
                        payload_full   = dict(base, event=self.event_full, kind="frame-color")
                        payload_compat = dict(base, event="frame-color",  kind="frame-color")

                        groups: Dict[float, List[str]] = {}
                        for addr in list(self.subscribers):
                            sc = float(_ensure_peer(addr).get("scale", 1.0))
                            groups.setdefault(sc, []).append(addr)

                        for sc, addrs in groups.items():
                            if abs(sc - 1.0) > 1e-3:
                                sw = max(2, int(w * sc)); sh = max(2, int(h * sc))
                                color_scaled = cv2.resize(out, (sw, sh), interpolation=cv2.INTER_AREA)
                                b64s, _, sw2, sh2 = _fit_b64_jpeg_by_res(color_scaled, JPEG_QUALITY, MAX_B64)
                                if b64s:
                                    base_s = dict(base, data=b64s, w=sw2, h=sh2)
                                    p_full   = dict(base_s, event=self.event_full, kind="frame-color")
                                    p_compat = dict(base_s, event="frame-color",  kind="frame-color")
                                    for addr in addrs:
                                        _dm(addr, p_full,   tries=1, fire=True)
                                        _dm(addr, p_compat, tries=1, fire=True)
                            else:
                                for addr in addrs:
                                    _dm(addr, payload_full,   tries=1, fire=True)
                                    _dm(addr, payload_compat, tries=1, fire=True)

                        if ENABLE_TOPICS: _pub(self.topic, payload_full)


class StreamRegistry:
    def __init__(self):
        self.sources: Dict[Tuple[str,str], StreamSource] = {}
        self.lock = threading.Lock()

    def get_or_create(self, stype: str, name: str, url: str, hz: int) -> StreamSource:
        key = (stype, name)
        with self.lock:
            s = self.sources.get(key)
            if s is None:
                s = StreamSource(stype, name, url, hz)
                self.sources[key] = s
        return s

    def stop_if_idle(self, stype: str, name: str):
        key = (stype, name)
        with self.lock:
            s = self.sources.get(key)
            if s and not s.subscribers and key not in ALWAYS_ON_KEYS:
                s.stop()
                del self.sources[key]

    def list_active(self) -> List[Dict[str,Any]]:
        with self.lock:
            return [{
                "type": s.stype,
                "name": s.name,
                "url": s.url,
                "hz": s.hz,
                "topic": s.topic,
                "event_full": s.event_full,                     # dedicated per-stream event
                "compat_event": _simple_event_for(s.stype, s.name),  # generic (frame-color/depth)
                "subs": len(s.subscribers),
                "running": s.running,
                "kind": s.kind
            } for s in self.sources.values()]

streams = StreamRegistry()
ALWAYS_ON_KEYS: Set[Tuple[str,str]] = set()
BASELINE_COLOR: Optional[Tuple[str,str]] = None  # (stype,name) for QR preview

# ──────────────────────────────────────────────────────────────────────
# 10) discovery helpers
# ──────────────────────────────────────────────────────────────────────
def norm_name(s: str) -> str: return s.strip()
def build_url(base: str, stype: str, name: str) -> str: return f"{base.rstrip('/')}/{stype.strip('/')}/{name}"

def probe_endpoint(url: str, *, quick=False) -> Tuple[bool, str]:
    try:
        if quick:
            r = requests.head(url, timeout=2.0)
            if r.status_code != 200: return False, "unknown"
            ct = (r.headers.get("Content-Type","") or "").lower()
            if "multipart" in ct: return True, "mjpeg"
            if ct.startswith("image/"): return True, "single-image"
            return True, "unknown"
        r = requests.get(url, stream=True, timeout=3.0)
        if r.status_code != 200: return False, "unknown"
        ct = (r.headers.get("Content-Type","") or "").lower()
        if "multipart" in ct: return True, "mjpeg"
        if ct.startswith("image/"): return True, "single-image"
        return True, "unknown"
    except Exception:
        return False, "unknown"

def discover_streams() -> Dict[str, List[Dict[str,Any]]]:
    found = {"camera": [], "video": []}
    for stype in ("camera","video"):
        base = CAMERA_BASE if stype=="camera" else VIDEO_BASE
        for cand in STREAM_CANDIDATES:
            name = cand.strip()
            tried = set()
            for nm in (name, name.replace("_","-") if "_" in name else name.replace("-","_")):
                if nm in tried: continue
                tried.add(nm)
                url = build_url(base, stype, nm)
                ok, kind = probe_endpoint(url, quick=True)
                if ok:
                    hz = CAMERA_HZ if stype=="camera" else STREAM_HZ
                    found[stype].append({"type": stype, "name": nm, "url": url, "kind": kind, "hz": hz})
                    break
    return found

# ──────────────────────────────────────────────────────────────────────
# 11) presence + control + commands (+ QR scan baseline on camera/rs_color)
# ──────────────────────────────────────────────────────────────────────
def _ack(addr: str, ok: bool, note: str):
    _dm(addr, {"event": "ack", "ok": bool(ok), "note": note}, fire=False)

def _mark_online(addr: str):
    st = _ensure_peer(addr)
    st["online"] = True
    st["last_online"] = time.time()

def _streams_summary() -> Dict[str, Any]:
    """Top-level color/depth summary for UI convenience."""
    color = None
    depth = None
    for s in list(streams.sources.values()):
        if not s.running: continue
        evt = _simple_event_for(s.stype, s.name)
        if evt == "frame-color" and color is None:
            color = {"type": s.stype, "name": s.name, "topic": s.topic}
        if evt == "frame-depth" and depth is None:
            depth = {"type": s.stype, "name": s.name, "topic": s.topic}
    return {"color": color, "depth": depth}

def send_stream_info(addr: str, *, force: bool = False):
    st = _ensure_peer(addr)
    now = time.time()
    if not force and (now - st.get("last_info_ts", 0.0) < 5.0):
        return
    info = {
        "event": "streams",
        "uuid": DEVICE_UUID,
        "label": DEVICE_LABEL,
        "active": streams.list_active(),
        "topic_cmd": state.get("cmd_topic"),
        "bases": {"camera": CAMERA_BASE, "video": VIDEO_BASE},
        "candidates": STREAM_CANDIDATES,
        "always_on": list(ALWAYS_ON_KEYS),
        **_streams_summary(),
    }
    _dm(addr, info, fire=False)
    st["last_info_ts"] = now

def parse_get_command(cmd: str) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Supports:
      get streams
      get <name>                -> defaults to video
      get <type> <name>        -> explicit type camera|video
      stop <name>              -> defaults to video
      stop <type> <name>       -> explicit type
    """
    raw = cmd.strip()
    parts = raw.split()
    if len(parts) >= 2 and parts[0].lower() == "get" and parts[1].lower() == "streams":
        return "list", None, None
    if parts and parts[0].lower() == "get":
        if len(parts) == 2:
            return "start", "video", norm_name(parts[1])
        if len(parts) >= 3:
            return "start", parts[1].lower(), norm_name(parts[2])
    if parts and parts[0].lower() == "stop":
        if len(parts) == 2:
            return "stop", "video", norm_name(parts[1])
        if len(parts) >= 3:
            return "stop", parts[1].lower(), norm_name(parts[2])
    return "", None, None

def _try_find_stream(stype: str, name: str) -> Tuple[bool, str, str, str]:
    """Try (stype,name) and underscore/dash variant; return (ok, stype, name, url)."""
    def _alt(n: str) -> str:
        return n.replace("_","-") if "_" in n else n.replace("-","_")
    bases = {"camera": CAMERA_BASE, "video": VIDEO_BASE}
    order = [stype]
    other = "camera" if stype == "video" else "video"
    if other not in order: order.append(other)
    tried = []
    for st in order:
        base = bases[st]
        for nm in [name, _alt(name)]:
            url = build_url(base, st, nm)
            tried.append(f"{st}:{url}")
            ok, _kind = probe_endpoint(url, quick=True)
            if ok:
                return True, st, nm, url
    _log(f"[probe] not found ({', '.join(tried)})")
    return False, stype, name, build_url(bases[stype], stype, name)

# ——— Baseline /camera/rs_color for QR + preview
def _find_url_with_alt(base: str, stype: str, name: str) -> Tuple[Optional[str], Optional[str]]:
    tried = []
    url = build_url(base, stype, name); tried.append(url)
    ok, _ = probe_endpoint(url, quick=True)
    if ok: return url, name
    alt = name.replace("_","-") if "_" in name else name.replace("-","_")
    if alt != name:
        url2 = build_url(base, stype, alt); tried.append(url2)
        ok2, _ = probe_endpoint(url2, quick=True)
        if ok2: return url2, alt
    _log(f"[probe] {stype}/{name} not found (tried: {', '.join(tried)})")
    return None, None

def ensure_baseline_streams():
    """Ensure camera/rs_color is always-on for QR; also honor ANY additional ALWAYS_ON items."""
    need: List[Tuple[str,str,str,int]] = []

    # 1) Mandatory QR baseline: camera/rs_color
    url_qr, nm_qr = _find_url_with_alt(CAMERA_BASE, "camera", "rs_color")
    if url_qr:
        need.append(("camera", nm_qr, url_qr, CAMERA_HZ))
    else:
        need.append(("camera", "rs_color", build_url(CAMERA_BASE, "camera", "rs_color"), CAMERA_HZ))

    # 2) Additional ALWAYS_ON
    for item in ALWAYS_ON_CONF:
        if ":" not in item: continue
        stype, name = item.split(":",1)
        stype = stype.strip().lower(); name = norm_name(name)
        if stype == "camera" and name in ("rs_color","auto"):
            continue
        base = CAMERA_BASE if stype=="camera" else VIDEO_BASE
        url, newname = _find_url_with_alt(base, stype, name)
        if url:
            hz = CAMERA_HZ if stype=="camera" else STREAM_HZ
            need.append((stype, newname or name, url, hz))

    for stype,name,url,hz in need:
        key = (stype,name)
        if key in ALWAYS_ON_KEYS:
            continue
        s = streams.get_or_create(stype, name, url, hz)
        ALWAYS_ON_KEYS.add(key)
        if stype == "camera" and ("color" in name.lower() or name.lower()=="rs_color"):
            global BASELINE_COLOR
            BASELINE_COLOR = key
        if not s.running: s.start()
        _log(f"[baseline] {stype}/{name} running @ {url} (hz={hz})")

# ——— QR scanner on baseline camera/rs_color (overlay + grant)
class QRScanner:
    def __init__(self, read_fn, max_width: int = 960):
        self.read_fn = read_fn
        self.max_width = max_width
        self.stop_evt = threading.Event()
        self.det = cv2.QRCodeDetector()
        self.seen: Dict[str, float] = {}  # dedup_key -> ts

    def start(self):
        if self.max_width <= 0:
            _log("[qr] disabled (SCAN_MAX_WIDTH=0)")
            return
        threading.Thread(target=self._loop, daemon=True).start()
        _log(f"[qr] scanner started (max_width={self.max_width})")

    def _normalize_polys(self, polys: List[np.ndarray], w: int, h: int) -> List[List[Tuple[float,float]]]:
        out: List[List[Tuple[float,float]]] = []
        for p in polys:
            try:
                pts = p.reshape(-1,2)
                norm = [(float(x)/float(w), float(y)/float(h)) for (x,y) in pts]
                out.append(norm)
            except Exception:
                continue
        return out

    def _emit_overlay_event(self, polys_norm: List[List[Tuple[float,float]]]):
        if not polys_norm: return
        payload = {"event":"qr-overlay","polys":polys_norm, "ts": int(time.time()*1000), "no_ack": 1}
        for addr in list(addresses.keys()):
            _dm(addr, payload, tries=1, fire=True)

    def _looks_pubkey_text(self, t: str) -> Tuple[bool, Optional[str]]:
        """Return (True, pubhex) if t is a recognizable pubkey-only string."""
        s = (t or "").strip()
        # base64url of 32 bytes (Ed25519 pubkey) → ~43 chars (no padding)
        try:
            b = b64url_decode(s)
            if len(b) == 32:
                return True, b.hex()
        except Exception:
            pass
        # raw 64-hex
        if re.fullmatch(r"[0-9a-fA-F]{64}", s):
            return True, s.lower()
        # identifier.pubhex
        m = re.fullmatch(r"[A-Za-z0-9_-]+\.([0-9a-fA-F]{64})", s)
        if m:
            return True, m.group(1).lower()
        return False, None

    def _dedup_key_for_text(self, t: str) -> str:
        if t.startswith("nkn+invite:"):
            return t
        ok, hx = self._looks_pubkey_text(t)
        if ok and hx:
            return "pk:"+hx
        return "other:"+t  # conservative

    def _loop(self):
        while not self.stop_evt.is_set():
            frame = self.read_fn()
            if frame is None:
                time.sleep(0.05); continue
            try:
                img = frame
                h, w = img.shape[:2]
                if self.max_width and w > self.max_width:
                    scale = self.max_width / float(w)
                    img = cv2.resize(img, (int(w*scale), int(h*scale)), interpolation=cv2.INTER_AREA)
                    h, w = img.shape[:2]

                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

                texts: List[str] = []
                polys: List[np.ndarray] = []

                try:
                    retval, decoded_info, pts, _ = self.det.detectAndDecodeMulti(gray)
                    if pts is not None and len(pts):
                        for poly in pts:
                            polys.append(np.asarray(poly, dtype=np.float32))
                    if retval and decoded_info:
                        texts.extend([s for s in decoded_info if s])
                except Exception:
                    pass

                if not texts:
                    try:
                        t, pts, _ = self.det.detectAndDecode(gray)
                        if t:
                            texts.append(t)
                        if pts is not None and len(pts):
                            polys.append(np.asarray(pts, dtype=np.float32))
                    except Exception:
                        pass

                invites = []
                overlay_polys: List[np.ndarray] = []
                now = time.time()

                for t, p in zip(texts, polys):
                    if not isinstance(t, str): continue
                    is_old = t.startswith("nkn+invite:")
                    is_short, _hx = self._looks_pubkey_text(t)
                    if not (is_old or is_short):
                        continue
                    dedup_key = self._dedup_key_for_text(t)
                    if dedup_key in self.seen and now - self.seen[dedup_key] < 10:
                        overlay_polys.append(p)
                        continue
                    self.seen[dedup_key] = now
                    invites.append((t, p, dedup_key))

                all_polys = overlay_polys + [p for (_t,p,_k) in invites]
                if all_polys:
                    polys_norm = self._normalize_polys(all_polys, w, h)
                    _qr_set_overlay(polys_norm)
                    self._emit_overlay_event(polys_norm)

                for txt, _p, _k in invites:
                    ok, note, dest, scopes, exp = process_any_invite(txt)
                    _log(f"[qr] parsed: ok={ok} note={note}")
            except Exception:
                pass
            time.sleep(0.06)

    def stop(self): self.stop_evt.set()

qr_scanner: Optional[QRScanner] = None

def _maybe_start_qr_scanner():
    global qr_scanner
    if BASELINE_COLOR and tuple(BASELINE_COLOR) in streams.sources:
        s = streams.sources[tuple(BASELINE_COLOR)]
        qr_scanner = QRScanner(read_fn=s.grabber.read, max_width=SCAN_MAX_WIDTH)
        qr_scanner.start()

def handle_inbound(src_addr: str, payload: Any, *, topic: Optional[str] = None):
    if not src_addr: return
    _mark_online(src_addr)

    body = payload if isinstance(payload, dict) else {}
    cmd = ""
    if isinstance(payload, str):
        cmd = payload.strip()
    elif isinstance(payload, dict):
        cmd = (payload.get("cmd") or payload.get("data") or payload.get("raw") or "").strip()

    if cmd: _log(f"[cmd] from {src_addr} topic={topic or '-'} → {cmd}")

    if isinstance(body, dict) and body.get("event") == "ping":
        _dm(src_addr, {"event":"hello","from": state.get("client_address"), "uuid": DEVICE_UUID}, fire=False)
        send_stream_info(src_addr); return
    if isinstance(body, dict) and body.get("event") == "hello":
        _dm(src_addr, {"event":"hello-ack","uuid": DEVICE_UUID}, fire=False)
        send_stream_info(src_addr); return

    if topic and topic != state.get("cmd_topic"): return

    action, stype, name = parse_get_command(cmd) if cmd else ("",None,None)

    if action == "list":
        _log(f"[streams] list requested by {src_addr}")
        available = discover_streams()
        reply = {"event":"streams-list","available": available,"active": streams.list_active()}
        _dm(src_addr, reply, fire=False)
        return

    elif action == "start" and stype and name:
        ok, stype_found, name_found, url = _try_find_stream(stype, name)
        if not ok:
            _ack(src_addr, False, f"{stype}/{name} not found (auto-fallback tried camera/video)")
            return

        hz = CAMERA_HZ if stype_found=="camera" else STREAM_HZ
        s = streams.get_or_create(stype_found, name_found, url, hz)
        s.subscribers.add(src_addr)
        if not s.running: s.start()
        _dm(src_addr, {"event":"subscribed","type":stype_found,"name":name_found,"topic":s.topic,"url":url,"hz":hz,"event_name":s.event_full}, fire=False)
        send_stream_info(src_addr, force=True)
        _log(f"[streams] subscribed {src_addr} to {stype_found}/{name_found} @ {url} (hz={hz})")
        return

    elif action == "stop" and stype and name:
        # Also honor auto-found stype/name pair if user used a mismatched type earlier
        for key in list(streams.sources.keys()):
            if key[1] == name and (key[0] == stype or True):
                s = streams.sources.get(key)
                if s and src_addr in s.subscribers:
                    s.subscribers.discard(src_addr)
                    _dm(src_addr, {"event":"unsubscribed","type":key[0],"name":key[1]}, fire=False)
                    if not s.subscribers:
                        streams.stop_if_idle(key[0], key[1])
                    send_stream_info(src_addr, force=True)
                    _log(f"[streams] unsubscribed {src_addr} from {key[0]}/{key[1]}")
                    return
        _ack(src_addr, False, f"not subscribed to {stype}/{name}")
        return

    if cmd.lower().startswith("/res"):
        parts = cmd.split()
        if len(parts) >= 2:
            p = parts[1].rstrip("%")
            try:
                pct = float(p)
                st = _ensure_peer(src_addr)
                st["scale"] = float(max(0.1, min(1.0, pct/100.0)))
                pcti = int(round(st["scale"]*100))
                _ack(src_addr, True, f"resolution set to {pcti}%")
                _log(f"[res] {src_addr} → {pcti}%"); return
            except Exception: pass
        _ack(src_addr, False, "usage: /res <percent>"); return

    if cmd.lower().startswith("/stats"):
        st = _ensure_peer(src_addr)
        toks = cmd.split()
        if len(toks) == 1:
            _ack(src_addr, True, f"stats={'on' if st.get('stats_on') else 'off'} hz={st.get('stats_hz',1)}"); return
        arg = toks[1].lower()
        if arg in ("on","off"):
            st["stats_on"] = (arg == "on"); _ack(src_addr, True, f"stats {arg}")
            _log(f"[stats] {src_addr} → {arg}"); return
        if arg == "hz" and len(toks) >= 3:
            try:
                hz = max(1, min(10, int(toks[2]))); st["stats_hz"] = hz; _ack(src_addr, True, f"stats hz={hz}")
                _log(f"[stats] {src_addr} → hz {hz}")
            except Exception:
                _ack(src_addr, False, "usage: /stats hz <n>")
            return
        if arg == "once":
            _ack(src_addr, True, "stats once (not implemented)"); return
        _ack(src_addr, False, "usage: /stats on|off | /stats hz <n> | /stats once"); return

    if cmd.lower() in ("/home","home","/center","center"):
        ok = _serial_send("HOME"); _ack(src_addr, ok, "HOME sent" if ok else "no serial")
        _log(f"[serial] HOME by {src_addr} ok={ok}"); return

    if cmd and _validate_axes_cmd(cmd):
        full = _merge_axes_cmd(cmd); ok = _serial_send(full)
        _ack(src_addr, ok, f"serial: {full}" if ok else "no serial")
        _log(f"[serial] {src_addr} → {full} ok={ok}"); return

    if cmd:
        _ack(src_addr, False, "unknown command")
        _log(f"[cmd] unknown from {src_addr}: {cmd}")

# ──────────────────────────────────────────────────────────────────────
# 12) QR grant flow (legacy + short pubkey-only)
# ──────────────────────────────────────────────────────────────────────
def grant_for(client_pub_hex: str, scopes_list: List[str], exp_unix: int) -> dict:
    token_body = {"v":1,"sub":client_pub_hex,"scopes":scopes_list,"exp":int(exp_unix),
                  "device": state.get("client_address") or f"device.{DEVICE_PUBHEX}", "rc": REV_COUNTER}
    token = sign_token(token_body)
    return {"token": token, "exp": token_body["exp"], "scopes": token_body["scopes"], "device": token_body["device"]}

def verify_invite_sig_v1(client_addr: str, v: str, scopes: str, exp: str, nonce_b64url: str, sig_b64url: str) -> Tuple[bool, Optional[str]]:
    s = (client_addr or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", s): pubhex = s.lower()
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
    """Legacy nkn+invite parser (v1/v2)."""
    try:
        if not txt.startswith("nkn+invite:"):
            return False, "QR not an NKN invite", None, [], 0
        qs = txt[len("nkn+invite:"):]
        params = urllib.parse.parse_qs(qs, keep_blank_values=True)
        get = lambda k: (params.get(k,[""])[0] or "").strip()
        v = get("v") or "1"

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
            _dm(dest_addr, {"v":2,"type":"grant","grant":grant}, fire=False)
            _persist_peer(dest_addr, ident, grant["scopes"], grant["exp"], grant["device"], scale=1.0)
            _dm(dest_addr, {"event":"hello","from": state.get("client_address"), "uuid": DEVICE_UUID}, fire=False)
            send_stream_info(dest_addr, force=True)
            return True, f"GRANTED (v2) to {dest_addr}", dest_addr, scopes_list, exp_unix

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
        _dm(dest_addr, {"v":1,"type":"grant","grant":grant}, fire=False)
        _persist_peer(dest_addr, label or ident, grant["scopes"], grant["exp"], grant["device"], scale=1.0)
        _dm(dest_addr, {"event":"hello","from": state.get("client_address"), "uuid": DEVICE_UUID}, fire=False)
        send_stream_info(dest_addr, force=True)
        return True, f"GRANTED (v1) to {dest_addr}", dest_addr, scopes_list, exp_unix

    except Exception as e:
        return False, f"QR error: {e}", None, [], 0

def process_any_invite(txt: str) -> Tuple[bool, str, Optional[str], List[str], int]:
    """Accepts legacy nkn+invite:* AND short pubkey-only codes (base64url/hex/identifier.hex)."""
    try:
        if isinstance(txt, str) and txt.startswith("nkn+invite:"):
            return process_qr_payload(txt)

        s = (txt or "").strip()
        pubhex = None
        # base64url (32 bytes)
        try:
            b = b64url_decode(s)
            if len(b) == 32:
                pubhex = b.hex()
        except Exception:
            pass
        # 64-hex
        if pubhex is None and re.fullmatch(r"[0-9a-fA-F]{64}", s):
            pubhex = s.lower()
        # identifier.pubhex
        if pubhex is None:
            m = re.fullmatch(r"[A-Za-z0-9_-]+\.([0-9a-fA-F]{64})", s)
            if m: pubhex = m.group(1).lower()

        if not pubhex:
            return False, "Not an invite/pubkey", None, [], 0

        default_sc = os.environ.get("DEFAULT_SCOPES_SHORT", "rd")
        default_min = int(os.environ.get("DEFAULT_INV_MIN", "60"))
        scopes_list = scopes_from_short(default_sc)
        exp_unix = int(time.time()) + default_min*60

        dest_addr = f"client.{pubhex}"
        grant = grant_for(pubhex, scopes_list, exp_unix)
        _dm(dest_addr, {"type":"grant","grant":grant}, fire=False)
        _persist_peer(dest_addr, "client", grant["scopes"], grant["exp"], grant["device"], scale=1.0)
        _dm(dest_addr, {"event":"hello","from": state.get("client_address"), "uuid": DEVICE_UUID}, fire=False)
        send_stream_info(dest_addr, force=True)
        return True, f"GRANTED (pk) to {dest_addr}", dest_addr, scopes_list, exp_unix
    except Exception as e:
        return False, f"QR error: {e}", None, [], 0

# ──────────────────────────────────────────────────────────────────────
# 13) main
# ──────────────────────────────────────────────────────────────────────
def run():
    # wait NKN ready
    t0 = time.time()
    while not state.get("client_address"):
        if time.time() - t0 > 30:
            _log("NKN not ready, exiting."); _shutdown()
        time.sleep(0.02)

    # restore peers
    if addresses:
        for addr, meta in addresses.items():
            _ensure_peer(addr, default_scale=float(meta.get("scale",1.0)))
        _log(f"[peers] restored {len(addresses)} from addresses.json")

    _log(f"[bases] camera={CAMERA_BASE}  video={VIDEO_BASE}")
    _log(f"[candidates] {', '.join(STREAM_CANDIDATES) or '-'}")

    # HighGUI helper for multiple windows
    try:
        cv2.startWindowThread()
    except Exception:
        pass

    # Ensure baseline QR stream and any extra always-on
    ensure_baseline_streams()
    # Start QR scanner on baseline camera color (if available)
    _maybe_start_qr_scanner()

    _log("→ Ready. Commands: 'get streams', 'get rs_color', 'get camera rs_color', 'get rs_depth', 'get camera ir_left', 'stop rs_depth', '/res 50' etc.")

    try:
        while True:
            time.sleep(0.5)
            # mark offline by TTL
            now = time.time()
            for addr, st in peers.items():
                if st["online"] and (now - st["last_online"] > PRESENCE_TTL):
                    st["online"] = False
            # stop idle streams if no subscribers (baseline protected)
            for key, s in list(streams.sources.items()):
                if not s.subscribers and s.running:
                    streams.stop_if_idle(s.stype, s.name)
    except KeyboardInterrupt:
        pass
    finally:
        for key, s in list(streams.sources.items()):
            try: s.stop()
            except Exception: pass

if __name__ == "__main__":
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    _log("→ Launching NKN device bridge …")
    run()
