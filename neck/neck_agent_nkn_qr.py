#!/usr/bin/env python3
"""
app.py — NKN QR handshake, multi-peer grayscale streaming, and DM commands
- QR scan from VIDEO_URL using the same low-latency method as qr_scan.py
- Grants (v1/v2) → saves peer to addresses.json → streams frames to ALL peers
- Per-peer /res <percent> to change resolution scale
- /stats on|off | /stats hz <n> | /stats once
- Serial actuator commands: "X100,Y-50,Z0,H30,S1,A1,R0,P0" or "home"
- Aggressive DM: 2 tries, 20ms constant backoff (in Node sidecar)
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
from nacl.exceptions import BadSignatureError
import serial  # pyserial

# ──────────────────────────────────────────────────────────────────────
# 2) args & .env
# ──────────────────────────────────────────────────────────────────────
cli = argparse.ArgumentParser()
cli.add_argument("--video-url", default=os.environ.get("VIDEO_URL", "http://127.0.0.1:8080/video/rs_color"))
cli.add_argument("--stream-hz", type=int, default=int(os.environ.get("STREAM_HZ", "30")))
cli.add_argument("--scan-max-width", type=int, default=int(os.environ.get("SCAN_MAX_WIDTH", "960")))
cli.add_argument("--label", default=os.environ.get("DEVICE_LABEL","neck-agent"))
cli.add_argument("--uuid", default=os.environ.get("DEVICE_UUID",""))
cli.add_argument("--subclients", type=int, default=int(os.environ.get("SUBCLIENTS","8")))
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

dotenv["VIDEO_URL"]      = args.video_url or dotenv.get("VIDEO_URL","http://127.0.0.1:8080/video/rs_color")
dotenv["STREAM_HZ"]      = str(max(1, min(60, int(args.stream_hz))))
dotenv["SCAN_MAX_WIDTH"] = str(max(0, min(2560, int(args.scan_max_width))))
dotenv["DEVICE_UUID"]    = args.uuid or dotenv.get("DEVICE_UUID") or str(_uuid.uuid4())
dotenv["SUBCLIENTS"]     = str(max(1, min(16, int(args.subclients))))
_save_env(ENV_PATH, dotenv)

DEVICE_SEED_HEX = dotenv["DEVICE_SEED_HEX"].lower().replace("0x","")
TOPIC_PREFIX    = dotenv["DEVICE_TOPIC_PREFIX"]
REV_COUNTER     = int(dotenv["REV_COUNTER"])
VIDEO_URL       = dotenv["VIDEO_URL"]
STREAM_HZ       = max(1, min(60, int(dotenv["STREAM_HZ"])))
SCAN_MAX_WIDTH  = max(0, min(2560, int(dotenv["SCAN_MAX_WIDTH"])))
DEVICE_UUID     = dotenv["DEVICE_UUID"]
DEVICE_LABEL    = args.label
SUBCLIENTS      = max(1, min(16, int(dotenv["SUBCLIENTS"])))

# ──────────────────────────────────────────────────────────────────────
# 3) NKN bridge (Node.js sidecar) — aggressive low backoff
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
/* nkn_device_bridge.js — NKN DM bridge with aggressive low-latency send.
   - MultiClient (numSubClients from env)
   - JSON line IPC with Python
   - sendDMWithRetry: constant backoff 20ms, tries=2 (fire-and-forget-ish)
*/
const nkn = require('nkn-sdk');
const readline = require('readline');

const SEED_HEX = (process.env.DEVICE_SEED_HEX || '').toLowerCase().replace(/^0x/,'');
const IDENT    = process.env.DEVICE_IDENT || 'device';
const TOPIC_NS = process.env.DEVICE_TOPIC_PREFIX || 'roko-signaling';
const SUBS     = Math.max(1, Math.min(16, parseInt(process.env.SUBCLIENTS || '8', 10)));

function log(...args){ console.error('[device-bridge]', ...args); }
function sendToPy(obj){ process.stdout.write(JSON.stringify(obj) + '\n'); }
function isFullAddr(s){ return typeof s === 'string' && /^[A-Za-z0-9_-]+\.[0-9a-f]{64}$/i.test((s||'').trim()); }
function isHex64(s){ return typeof s === 'string' && /^[0-9a-f]{64}$/i.test((s||'').trim()); }
function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }

async function sendDMWithRetry(client, dest, data, tries=2){
  if (!isFullAddr(dest) && !isHex64(dest)) { return false; }
  const body = JSON.stringify(data);
  const t = Math.max(1, Math.min(3, parseInt(tries || 2, 10)));
  for (let i=0;i<t;i++){
    try { await client.send(dest, body); return true; }
    catch(e){ await sleep(20); } // 20ms constant backoff cap
  }
  return false;
}

(async () => {
  if (!/^[0-9a-f]{64}$/i.test(SEED_HEX)) throw new RangeError('invalid hex seed');
  const client = new nkn.MultiClient({ seed: SEED_HEX, identifier: IDENT, numSubClients: SUBS });

  client.on('connect', () => {
    sendToPy({ type: 'ready', address: client.addr, topicPrefix: TOPIC_NS, subs: SUBS });
    log('ready at', client.addr, 'subs=', SUBS);
  });

  client.on('message', (a,b) => {
    let src, payload;
    if (a && typeof a === 'object' && (a.payload !== undefined || a.data !== undefined || a.src !== undefined)) {
      src = a.src || a.from || a.addr || '';
      payload = (a.payload !== undefined) ? a.payload : a.data;
    } else { src = a; payload = b; }
    try {
      const txt = Buffer.isBuffer(payload) ? payload.toString('utf8')
                 : (typeof payload==='string' ? payload : JSON.stringify(payload));
      let msg; try { msg = JSON.parse(txt); } catch { msg = { raw: txt }; }
      sendToPy({ type:'nkn-message', src, msg });
    } catch {}
  });

  const rl = readline.createInterface({ input: process.stdin });
  rl.on('line', async (line) => {
    if (!line) return;
    let cmd; try { cmd = JSON.parse(line); } catch { return; }
    if (cmd.type === 'dm') {
      await sendDMWithRetry(client, String(cmd.to||'').trim(), cmd.data, cmd.tries || 2);
    } else if (cmd.type === 'pub') {
      try { await client.publish(TOPIC_NS + '.' + cmd.topic, JSON.stringify(cmd.data)); } catch {}
    }
  });
})();
"""
if not BRIDGE_JS.exists() or BRIDGE_JS.read_text() != BRIDGE_SRC:
    BRIDGE_JS.write_text(BRIDGE_SRC)

bridge_env = os.environ.copy()
bridge_env["DEVICE_SEED_HEX"]     = DEVICE_SEED_HEX
bridge_env["DEVICE_IDENT"]        = os.environ.get("DEVICE_IDENT","device")
bridge_env["DEVICE_TOPIC_PREFIX"] = TOPIC_PREFIX
bridge_env["SUBCLIENTS"]          = str(SUBCLIENTS)

bridge = Popen(
    [str(shutil.which("node")), str(BRIDGE_JS)],
    cwd=NODE_DIR, env=bridge_env,
    stdin=PIPE, stdout=PIPE, stderr=PIPE, text=True, bufsize=1
)

state: Dict[str, Any] = {"client_address": None, "topic_prefix": TOPIC_PREFIX, "subs": SUBCLIENTS}

def _bridge_send(obj: dict):
    try:
        bridge.stdin.write(json.dumps(obj) + "\n"); bridge.stdin.flush()
    except Exception as e:
        print("bridge send error:", e)

def _dm(dest: str, data: dict, tries: int = 2):
    _bridge_send({"type":"dm","to":dest,"data":data,"tries":tries})

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
        if msg.get("type") == "ready":
            state["client_address"] = msg.get("address")
            state["topic_prefix"]   = msg.get("topicPrefix") or TOPIC_PREFIX
            state["subs"]           = int(msg.get("subs") or SUBCLIENTS)
            print(f"→ NKN ready: {state['client_address']}  subs={state['subs']}")
        elif msg.get("type") == "nkn-message":
            src = (msg.get("src") or "").strip()
            payload = msg.get("msg")
            try:
                handle_inbound_command(src, payload)
            except Exception as e:
                print("inbound handler error:", e)

def _bridge_err():
    for line in bridge.stderr:
        sys.stderr.write(line)

threading.Thread(target=_bridge_reader, daemon=True).start()
threading.Thread(target=_bridge_err, daemon=True).start()

# ──────────────────────────────────────────────────────────────────────
# 4) crypto helpers + grant
# ──────────────────────────────────────────────────────────────────────
def b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")

def b64url_decode(s: str) -> bytes:
    s = (s or "").strip().replace(" ", "+")
    pad = '=' * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s + pad)

def canonical_invite_v1(v: str, client_addr: str, scopes_csv: str, exp: str, nonce_b64url: str) -> bytes:
    return f"{v}|{client_addr}|{scopes_csv}|{exp}|{nonce_b64url}".encode("utf-8")

def load_device_keys(seed_hex: str) -> Tuple[SigningKey, str]:
    seed = bytes.fromhex(seed_hex)
    sk = SigningKey(seed)
    pk = sk.verify_key.encode().hex()
    return sk, pk

DEVICE_SK, DEVICE_PUBHEX = load_device_keys(DEVICE_SEED_HEX)

def sign_token(body: dict) -> str:
    body_json = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = DEVICE_SK.sign(body_json).signature
    return f"{b64url_encode(body_json)}.{b64url_encode(sig)}"

# v1 verification (legacy)
def verify_invite_sig_v1(client_addr: str, v: str, scopes: str, exp: str, nonce_b64url: str, sig_b64url: str) -> Tuple[bool, Optional[str]]:
    s = (client_addr or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", s):
        pubhex = s.lower()
    else:
        m = re.fullmatch(r"[A-Za-z0-9_-]+\.([0-9a-fA-F]{64})", s)
        pubhex = m.group(1).lower() if m else None
    if not pubhex:
        return False, None
    try:
        vk = VerifyKey(bytes.fromhex(pubhex))
        canonical = canonical_invite_v1(v, client_addr, scopes, exp, nonce_b64url)
        sig = b64url_decode(sig_b64url)
        vk.verify(canonical, sig)
        return True, pubhex
    except Exception:
        return False, None

# v2 verification (compact)
def verify_invite_sig_v2(a_b64u: str, s_short: str, e36: str, n_b64u: str, g_b64u: str) -> Tuple[bool, Optional[str]]:
    try:
        pubkey = b64url_decode(a_b64u)
        if len(pubkey) != 32:
            return False, None
        sig = b64url_decode(g_b64u)
        if len(sig) != 64:
            return False, None
        canonical = f"2|{a_b64u}|{s_short}|{e36}|{n_b64u}".encode("utf-8")
        VerifyKey(pubkey).verify(canonical, sig)
        return True, pubkey.hex()
    except Exception:
        return False, None

# scope mapping (short -> full)
SCOPE_MAP = {
    'r': 'video:rgb',
    'd': 'video:depth',
    'm': 'audio:mic',
    's': 'audio:speaker',
    'p': 'control:ptz',
    'c': 'control:robot',
}
def scopes_from_short(s_short: str) -> List[str]:
    out: List[str] = []
    for ch in (s_short or ''):
        full = SCOPE_MAP.get(ch)
        if full and full not in out:
            out.append(full)
    if not out:
        out = ['video:rgb']
    return out

# ──────────────────────────────────────────────────────────────────────
# 5) address book (persist peer endpoints)
# ──────────────────────────────────────────────────────────────────────
ADDR_PATH = BASE_DIR / "addresses.json"
def _load_addresses() -> Dict[str, Dict[str, Any]]:
    if ADDR_PATH.exists():
        try: return json.loads(ADDR_PATH.read_text())
        except: return {}
    return {}
def _save_addresses(d: Dict[str, Dict[str, Any]]):
    ADDR_PATH.write_text(json.dumps(d, indent=2))

# peers runtime: addr -> state
# state: {"scale":float,"stats_on":bool,"stats_hz":int,"last_stats_ts":float,"sent_since_tick":int,"last_tick_ts":float}
peers: Dict[str, Dict[str, Any]] = {}
addresses = _load_addresses()  # persisted config (label, scopes, scale, exp, device)

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

ALLOWED = {
    "X": (-700, 700, int), "Y": (-700, 700, int), "Z": (-700, 700, int),
    "H": (0, 70, int),     "S": (0, 10, float),   "A": (0, 10, float),
    "R": (-700, 700, int), "P": (-700, 700, int),
}
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
# 7) EXACT grabber/resize/draw from qr_scan.py
# ──────────────────────────────────────────────────────────────────────
class LatestFrameGrabber:
    def __init__(self, url: str):
        try:
            self.cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        except Exception:
            self.cap = cv2.VideoCapture(url)
        for prop, val in [
            (cv2.CAP_PROP_BUFFERSIZE, 1),
            (cv2.CAP_PROP_FPS, 120),
            (cv2.CAP_PROP_CONVERT_RGB, 1),
        ]:
            try: self.cap.set(prop, val)
            except Exception: pass
        if not self.cap or not self.cap.isOpened():
            raise RuntimeError(f"Could not open video source: {url}")
        self._lock = threading.Lock()
        self._latest: Optional[np.ndarray] = None
        self._stopped = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self):
        while not self._stopped.is_set():
            ok, frame = self.cap.read()
            if not ok:
                time.sleep(0.002)
                continue
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

def resize_keep_aspect(img: np.ndarray, max_w: int) -> Tuple[np.ndarray, float]:
    if max_w <= 0:
        return img, 1.0
    h, w = img.shape[:2]
    if w <= max_w:
        return img, 1.0
    new_w = max_w
    new_h = int(round(h * (new_w / w)))
    out = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
    scale = w / float(new_w)
    return out, scale

def draw_polys(img: np.ndarray, polys: List[np.ndarray], color=(0, 255, 0)):
    if not polys:
        return
    for p in polys:
        pts = np.asarray(p, dtype=np.int32).reshape(-1, 2)
        cv2.polylines(img, [pts], True, color, 2, cv2.LINE_AA)

# ──────────────────────────────────────────────────────────────────────
# 8) QR → handshake → start streaming (multi-peer)
# ──────────────────────────────────────────────────────────────────────
def _encode_jpeg_gray(gray: np.ndarray, quality: int = 65) -> Tuple[Optional[str], int]:
    try:
        ok, buf = cv2.imencode(".jpg", gray, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok:
            return None, 0
        raw = buf.tobytes()
        return base64.b64encode(raw).decode("ascii"), len(raw)
    except Exception:
        return None, 0

def grant_for(client_pub_hex: str, scopes_list: List[str], exp_unix: int) -> dict:
    token_body = {
        "v": 1,
        "sub": client_pub_hex,
        "scopes": scopes_list,
        "exp": int(exp_unix),
        "device": state.get("client_address") or f"device.{DEVICE_PUBHEX}",
        "rc": REV_COUNTER
    }
    token = sign_token(token_body)
    return {"token": token, "exp": token_body["exp"], "scopes": token_body["scopes"], "device": token_body["device"]}

def process_qr_payload(txt: str) -> Tuple[bool, str, Optional[str], List[str], int]:
    """
    Returns (ok, message, dest_addr, scopes_list, exp_unix). Sends GRANT if verified.
    dest_addr is FULL "<identifier>.<pubhex>" (identifier defaults to "client" if not provided)
    """
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
        if re.fullmatch(r"[A-Za-z0-9_-]+\.([0-9a-fA-F]{64})", (caddr or "").strip()):
            dest_addr = caddr.strip()
        else:
            dest_addr = f"{ident}.{client_pub_hex}"
        exp_unix = int(exp)
        grant = grant_for(client_pub_hex, scopes_list, exp_unix)
        _dm(dest_addr, {"v":1,"type":"grant","grant":grant}, tries=2)
        _persist_peer(dest_addr, label or ident, grant["scopes"], grant["exp"], grant["device"], scale=1.0)
        return True, f"GRANTED (v1) to {dest_addr}", dest_addr, scopes_list, exp_unix

    except Exception as e:
        return False, f"QR error: {e}", None, [], 0

# ──────────────────────────────────────────────────────────────────────
# 9) inbound commands (DM) → per-peer scale, stats, serial control
# ──────────────────────────────────────────────────────────────────────
def _ack(addr: str, ok: bool, note: str):
    _dm(addr, {"event": "ack", "ok": bool(ok), "note": note})

def _update_scale(addr: str, scale: float):
    st = _ensure_peer(addr)
    st["scale"] = float(max(0.1, min(1.0, scale)))
    # persist into addresses.json
    if addr in addresses:
        addresses[addr]["scale"] = float(st["scale"])
        addresses[addr]["last_seen"] = int(time.time())
        _save_addresses(addresses)
    _ack(addr, True, f"resolution set to {int(st['scale']*100)}%")

def send_stats_now(addr: str, st: Dict[str, Any], *, last_bytes: int = 0, w: int = 0, h: int = 0):
    now = time.time()
    tick_dt = max(1e-6, now - st.get("last_tick_ts", now))
    fps_out = st.get("sent_since_tick", 0) / tick_dt
    msg = {
        "event": "stats",
        "fps_out": round(fps_out, 2),
        "jpeg_bytes": int(last_bytes),
        "scale": round(st.get("scale", 1.0), 3),
        "w": int(w),
        "h": int(h),
        "ts": int(now * 1000),
    }
    _dm(addr, msg)
    # reset tick counters
    st["sent_since_tick"] = 0
    st["last_tick_ts"] = now

def handle_inbound_command(src_addr: str, payload: Any):
    if not src_addr:
        return
    # Normalize to a command string or actuator string
    raw = None
    if isinstance(payload, dict) and payload.get("event") == "cmd":
        raw = payload.get("cmd") or payload.get("data") or ""
    elif isinstance(payload, dict) and "data" in payload and isinstance(payload["data"], str):
        # legacy: {"event":"peer-message","data":"X10,Y-5"}
        raw = payload.get("data","")
    elif isinstance(payload, str):
        raw = payload
    elif isinstance(payload, dict) and "raw" in payload and isinstance(payload["raw"], str):
        raw = payload["raw"]
    if not raw:
        return

    cmd = raw.strip()

    # /res <percent>
    if cmd.lower().startswith("/res"):
        parts = cmd.split()
        if len(parts) >= 2:
            p = parts[1].rstrip("%")
            try:
                pct = float(p)
                _update_scale(src_addr, pct/100.0)
                return
            except Exception:
                pass
        _ack(src_addr, False, "usage: /res <percent>  e.g. /res 50%")
        return

    # /stats ...
    if cmd.lower().startswith("/stats"):
        st = _ensure_peer(src_addr)
        toks = cmd.split()
        if len(toks) == 1:
            _ack(src_addr, True, f"stats={'on' if st['stats_on'] else 'off'} hz={st['stats_hz']}")
            return
        arg = toks[1].lower()
        if arg in ("on","off"):
            st["stats_on"] = (arg == "on")
            _ack(src_addr, True, f"stats {arg}")
            return
        if arg == "hz" and len(toks) >= 3:
            try:
                hz = max(1, min(10, int(toks[2])))
                st["stats_hz"] = hz
                _ack(src_addr, True, f"stats hz={hz}")
            except Exception:
                _ack(src_addr, False, "usage: /stats hz <1..10>")
            return
        if arg == "once":
            send_stats_now(src_addr, st)
            return
        _ack(src_addr, False, "usage: /stats on|off | /stats hz <n> | /stats once")
        return

    # /home shortcut
    if cmd.lower() in ("/home","home"):
        full = "HOME"
        ok = _serial_send(full)
        _ack(src_addr, ok, "HOME sent" if ok else "no serial")
        return

    # If it looks like actuator command:
    if _validate_axes_cmd(cmd):
        full = _merge_axes_cmd(cmd)
        ok = _serial_send(full)
        _ack(src_addr, ok, f"serial: {full}" if ok else "no serial")
        return

    # unknown
    _ack(src_addr, False, "unknown command")

# ──────────────────────────────────────────────────────────────────────
# 10) main run: scan, pair, broadcast frames to all peers
# ──────────────────────────────────────────────────────────────────────
def run():
    # wait NKN ready
    t0 = time.time()
    while not state.get("client_address"):
        if time.time() - t0 > 15:
            print("NKN not ready, exiting."); _shutdown()
        time.sleep(0.02)

    # Preload any persisted peers so they start receiving immediately
    if addresses:
        for addr, meta in addresses.items():
            _ensure_peer(addr, default_scale=float(meta.get("scale",1.0)))
        print(f"[peers] restored {len(addresses)} from addresses.json")

    print(f"[qr] opening: {VIDEO_URL}")
    grabber = LatestFrameGrabber(VIDEO_URL)
    detector = cv2.QRCodeDetector()
    window = "QR Scan"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)

    frames = 0
    last_ts = time.perf_counter()
    fps = 0.0
    stream_period = 1.0 / float(STREAM_HZ)
    last_stream = 0.0

    seen_invites = set()

    try:
        while True:
            frame = grabber.read()
            if frame is None:
                time.sleep(0.001)
                continue

            # Optional working resize for QR
            work, scale = resize_keep_aspect(frame, SCAN_MAX_WIDTH) if SCAN_MAX_WIDTH > 0 else (frame, 1.0)
            gray_for_qr = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)

            decoded_strings: List[str] = []
            polys_full: List[np.ndarray] = []

            # Multi
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

            # Single fallback
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

            # Print ALL decoded strings (every frame) like qr_scan.py
            for s in decoded_strings:
                print(s)

            # On first verified invite(s), add peers
            for s in decoded_strings:
                if s in seen_invites:
                    continue
                ok, msg, dest_addr, scopes, exp = process_qr_payload(s)
                if ok and dest_addr:
                    print(msg)
                seen_invites.add(s)

            # Draw detections on ORIGINAL frame
            vis = frame.copy()
            draw_polys(vis, polys_full, color=(0, 255, 0))

            # FPS overlay
            frames += 1
            nowp = time.perf_counter()
            if nowp - last_ts >= 1.0:
                fps = frames / (nowp - last_ts)
                frames = 0
                last_ts = nowp
            cv2.putText(vis, f"{fps:.1f} fps", (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,0), 2, cv2.LINE_AA)
            cv2.putText(vis, f"{DEVICE_LABEL}  uuid={DEVICE_UUID}", (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,0), 2, cv2.LINE_AA)

            # Show
            cv2.imshow(window, vis)
            key = cv2.waitKey(1) & 0xFF
            if key == 27 or key == ord('q'):
                break

            # Broadcast frames to ALL peers (per-peer scale)
            if peers:
                tnow = time.time()
                if tnow - last_stream >= stream_period:
                    last_stream = tnow
                    # Make base grayscale once
                    gray_base = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    # Group peers by unique scale to avoid repeated resizes
                    scale_groups: Dict[float, List[str]] = {}
                    for addr in list(peers.keys()):
                        st = _ensure_peer(addr)
                        sc = float(st.get("scale", 1.0))
                        scale_groups.setdefault(sc, []).append(addr)

                    scaled_cache: Dict[float, np.ndarray] = {}
                    for sc, addrs in scale_groups.items():
                        if abs(sc - 1.0) > 1e-3:
                            w = int(gray_base.shape[1] * sc)
                            h = int(gray_base.shape[0] * sc)
                            if w < 2 or h < 2:
                                w = max(2, w); h = max(2, h)
                            gray_scaled = cv2.resize(gray_base, (w, h), interpolation=cv2.INTER_AREA)
                        else:
                            gray_scaled = gray_base
                        scaled_cache[sc] = gray_scaled

                        # Encode once per scale, then DM to all addrs in that group
                        b64, raw_bytes = _encode_jpeg_gray(gray_scaled, quality=65)
                        if not b64:
                            continue
                        payload = {"event":"frame-color","uuid":DEVICE_UUID,"data":b64}
                        for addr in addrs:
                            _dm(addr, payload, tries=2)
                            st = _ensure_peer(addr)
                            st["sent_since_tick"] = st.get("sent_since_tick", 0) + 1
                            # periodic stats
                            if st.get("stats_on", False):
                                per = 1.0 / max(1, int(st.get("stats_hz", 1)))
                                if tnow - st.get("last_stats_ts", 0.0) >= per:
                                    send_stats_now(addr, st, last_bytes=raw_bytes, w=gray_scaled.shape[1], h=gray_scaled.shape[0])
                                    st["last_stats_ts"] = tnow

    finally:
        grabber.stop()
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
