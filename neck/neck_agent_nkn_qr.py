#!/usr/bin/env python3
"""
app.py — QR handshake + multi-peer streaming (DM) with ultra-low backoff.

What’s new in this build
------------------------
- Node bridge DM retries are minimal: default tries=1 for frames, with backoff clamped to ≤20ms.
- Python sender passes `backoffMs` to the bridge; frames use tries=1, backoffMs=10 (fire-and-forget-ish).
- MultiClient subclients bumped to 6 for better route availability.
- Keeps: multi-peer grants, per-peer /res % command, per-scale JPEG cache, addresses.json persistence.

NOTE: This still uses NKN **DM** for frames. If you want true fire-and-forget broadcast,
      switch to publish-on-secret-topic (I can wire that next).
"""

# ──────────────────────────────────────────────────────────────────────
# 0) venv bootstrap (stdlib only)
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
                           "numpy", "opencv-python", "pynacl", "requests"])
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
import shutil
from subprocess import Popen, PIPE
from nacl.signing import SigningKey, VerifyKey
from nacl.exceptions import BadSignatureError

# ──────────────────────────────────────────────────────────────────────
# 2) args & .env
# ──────────────────────────────────────────────────────────────────────
cli = argparse.ArgumentParser()
cli.add_argument("--video-url", default=os.environ.get("VIDEO_URL", "http://127.0.0.1:8080/video/rs_color"))
cli.add_argument("--stream-hz", type=int, default=int(os.environ.get("STREAM_HZ", "20")))
cli.add_argument("--scan-max-width", type=int, default=int(os.environ.get("SCAN_MAX_WIDTH", "640")))
cli.add_argument("--label", default=os.environ.get("DEVICE_LABEL","neck-agent"))
cli.add_argument("--uuid", default=os.environ.get("DEVICE_UUID",""))
cli.add_argument("--jpeg-quality", type=int, default=int(os.environ.get("JPEG_QUALITY","55")))
cli.add_argument("--max-jpeg-bytes", type=int, default=int(os.environ.get("MAX_JPEG_BYTES","60000")))
cli.add_argument("--default-scale", type=int, default=int(os.environ.get("DEFAULT_SCALE_PCT","50")))
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

dotenv["VIDEO_URL"]       = args.video_url or dotenv.get("VIDEO_URL","http://127.0.0.1:8080/video/rs_color")
dotenv["STREAM_HZ"]       = str(max(1, min(60, int(args.stream_hz))))
dotenv["SCAN_MAX_WIDTH"]  = str(max(0, min(2560, int(args.scan_max_width))))
dotenv["DEVICE_UUID"]     = args.uuid or dotenv.get("DEVICE_UUID") or str(_uuid.uuid4())
dotenv["JPEG_QUALITY"]    = str(max(10, min(95, int(args.jpeg_quality))))
dotenv["MAX_JPEG_BYTES"]  = str(max(20_000, min(200_000, int(args.max_jpeg_bytes))))
dotenv["DEFAULT_SCALE_PCT"]= str(max(10, min(100, int(args.default_scale))))
_save_env(ENV_PATH, dotenv)

DEVICE_SEED_HEX = dotenv["DEVICE_SEED_HEX"].lower().replace("0x","")
TOPIC_PREFIX    = dotenv["DEVICE_TOPIC_PREFIX"]
REV_COUNTER     = int(dotenv["REV_COUNTER"])
VIDEO_URL       = dotenv["VIDEO_URL"]
STREAM_HZ       = max(1, min(60, int(dotenv["STREAM_HZ"])))
SCAN_MAX_WIDTH  = max(0, min(2560, int(dotenv["SCAN_MAX_WIDTH"])))
DEVICE_UUID     = dotenv["DEVICE_UUID"]
DEVICE_LABEL    = args.label
JPEG_QUALITY    = max(10, min(95, int(dotenv["JPEG_QUALITY"])))
MAX_JPEG_BYTES  = max(20_000, min(200_000, int(dotenv["MAX_JPEG_BYTES"])))
DEFAULT_SCALE_PCT = max(10, min(100, int(dotenv["DEFAULT_SCALE_PCT"])))

# ──────────────────────────────────────────────────────────────────────
# 3) NKN bridge (Node.js sidecar) — backoff clamped to ≤20ms
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
/* nkn_device_bridge.js — DM with ultra-low backoff; inbound passthrough */
const nkn = require('nkn-sdk');
const readline = require('readline');

const SEED_HEX = (process.env.DEVICE_SEED_HEX || '').toLowerCase().replace(/^0x/,'');
const IDENT    = process.env.DEVICE_IDENT || 'device';
const TOPIC_NS = process.env.DEVICE_TOPIC_PREFIX || 'roko-signaling';

function log(...args){ console.error('[device-bridge]', ...args); }
function sendToPy(obj){ try { process.stdout.write(JSON.stringify(obj) + '\n'); } catch {} }
function isFullAddr(s){ return typeof s === 'string' && /^[A-Za-z0-9_-]+\.[0-9a-f]{64}$/i.test((s||'').trim()); }
function isHex64(s){ return typeof s === 'string' && /^[0-9a-f]{64}$/i.test((s||'').trim()); }
function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }

async function sendDMWithRetry(client, dest, data, tries=1, backoffMs=10){
  const d = String(dest||'').trim();
  if (!isFullAddr(d) && !isHex64(d)) { log('dm aborted: invalid dest', d); return false; }
  const bms = Math.max(0, Math.min(20, (backoffMs|0))); // clamp to ≤20ms
  for (let i=0; i<Math.max(1, tries|0); i++){
    try {
      await client.send(d, JSON.stringify(data));
      return true;
    } catch(e){
      if (i === (tries-1)) break;
      if (bms > 0) await sleep(bms);
    }
  }
  return false;
}

(async () => {
  if (!/^[0-9a-f]{64}$/i.test(SEED_HEX)) throw new RangeError('invalid hex seed');
  const client = new nkn.MultiClient({ seed: SEED_HEX, identifier: IDENT, numSubClients: 6 });

  client.on('connect', () => {
    sendToPy({ type: 'ready', address: client.addr, topicPrefix: TOPIC_NS });
    log('ready at', client.addr);
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
      await sendDMWithRetry(client, String(cmd.to||'').trim(), cmd.data, cmd.tries ?? 1, cmd.backoffMs ?? 10);
    } else if (cmd.type === 'pub') {
      try { await client.publish(TOPIC_NS + '.' + cmd.topic, JSON.stringify(cmd.data)); } catch(e) { log('pub err', e?.message||e); }
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

bridge = Popen(
    [str(shutil.which("node")), str(BRIDGE_JS)],
    cwd=NODE_DIR, env=bridge_env,
    stdin=PIPE, stdout=PIPE, stderr=PIPE, text=True, bufsize=1
)

state: Dict[str, Any] = {"client_address": None, "topic_prefix": TOPIC_PREFIX}

def _bridge_send(obj: dict):
    try:
        bridge.stdin.write(json.dumps(obj) + "\n"); bridge.stdin.flush()
    except Exception as e:
        print("bridge send error:", e)

def _dm(dest: str, data: dict, tries: int = 1, backoff_ms: int = 10):
    _bridge_send({"type":"dm","to":dest,"data":data,"tries":tries,"backoffMs":backoff_ms})

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
    sys.exit(0)

# inbound from bridge
def _handle_inbound(src_addr: str, body: Any):
    if not isinstance(body, dict): return
    # Commands from client UIs
    cmd = body.get("cmd") or (body.get("event") == "cmd" and body.get("data"))
    if isinstance(cmd, str):
        m = re.match(r"^\s*/res\s+(\d{1,3})\s*%?\s*$", cmd)
        if m:
            pct = max(10, min(100, int(m.group(1))))
            with peers_lock:
                peer = peers.get(src_addr)
                if peer:
                    peer["scale_pct"] = pct
                    peer["last_cmd"] = time.time()
            _dm(src_addr, {"event":"ack","cmd":cmd,"ok":True,"scalePct":pct}, tries=2, backoff_ms=20)

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
            print(f"→ NKN ready: {state['client_address']}")
        elif msg.get("type") == "nkn-message":
            _handle_inbound(msg.get("src") or "", msg.get("msg"))

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

BOOK_PATH = BASE_DIR / "addresses.json"  # persistent peers
def _load_book() -> dict:
    if BOOK_PATH.exists():
        try: return json.loads(BOOK_PATH.read_text())
        except: return {}
    return {}
def _save_book(b: dict):
    BOOK_PATH.write_text(json.dumps(b, indent=2))
book = _load_book()

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

# ──────────────────────────────────────────────────────────────────────
# 5) Pairing & peers
# ──────────────────────────────────────────────────────────────────────
peers_lock = threading.Lock()
# peers[dest_addr] = {pubhex, scopes, scale_pct, jpeg_q, last_cmd}
peers: Dict[str, Dict[str, Any]] = {}

# preload from addresses.json
for dest_addr, meta in _load_book().items():
    if isinstance(meta, dict):
        peers[dest_addr] = {
            "pubhex": meta.get("pubhex",""),
            "scopes": meta.get("scopes",["video:rgb"]),
            "scale_pct": int(meta.get("scale_pct", DEFAULT_SCALE_PCT)),
            "jpeg_q": int(meta.get("jpeg_q", JPEG_QUALITY)),
            "last_cmd": 0.0,
        }

def _remember_peer(dest_addr: str, pubhex: str, scopes: List[str]):
    b = _load_book()
    entry = b.get(dest_addr, {})
    entry.update({
        "pubhex": pubhex,
        "scopes": scopes,
        "scale_pct": entry.get("scale_pct", DEFAULT_SCALE_PCT),
        "jpeg_q": entry.get("jpeg_q", JPEG_QUALITY),
        "updated": int(time.time()),
    })
    b[dest_addr] = entry
    _save_book(b)
    with peers_lock:
        peers[dest_addr] = {
            "pubhex": pubhex,
            "scopes": scopes,
            "scale_pct": int(entry["scale_pct"]),
            "jpeg_q": int(entry["jpeg_q"]),
            "last_cmd": 0.0,
        }

# ──────────────────────────────────────────────────────────────────────
# 6) EXACT grabber/resize/draw from qr_scan.py (low-latency)
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
    if not polys: return
    for p in polys:
        pts = np.asarray(p, dtype=np.int32).reshape(-1, 2)
        cv2.polylines(img, [pts], True, color, 2, cv2.LINE_AA)

# ──────────────────────────────────────────────────────────────────────
# 7) QR parse → grant → remember peer
# ──────────────────────────────────────────────────────────────────────
def process_qr_payload(txt: str) -> Tuple[bool, str, Optional[str], List[str]]:
    """
    Returns (ok, message, full_dest_addr, scopes_list). Sends GRANT if verified.
    """
    try:
        if not txt.startswith("nkn+invite:"):
            return False, "QR not an NKN invite", None, []
        qs = txt[len("nkn+invite:"):]
        params = urllib.parse.parse_qs(qs, keep_blank_values=True)
        get = lambda k: (params.get(k,[""])[0] or "").strip()
        v = get("v") or "1"

        # v2 compact (preferred)
        if v == "2" or get("a"):
            a  = get("a"); s = get("s"); e = get("e"); n = get("n"); g = get("g")
            ident = get("i") or "client"
            if not (a and s and e and n and g):
                return False, "Invite v2 missing fields", None, []
            ok, client_pub_hex = verify_invite_sig_v2(a, s, e, n, g)
            if not ok or not client_pub_hex:
                return False, "Bad v2 invite signature", None, []
            try: exp_unix = int(e, 36)
            except Exception: return False, "Invalid v2 expiry", None, []
            if exp_unix < int(time.time()) - 2:
                return False, "Invite expired", None, []
            scopes_list = scopes_from_short(s)
            dest_addr = f"{ident}.{client_pub_hex}"
            grant = grant_for(client_pub_hex, scopes_list, exp_unix)
            _dm(dest_addr, {"v":2,"type":"grant","grant":grant}, tries=2, backoff_ms=20)
            _remember_peer(dest_addr, client_pub_hex, scopes_list)
            return True, f"GRANTED (v2) to {dest_addr}", dest_addr, scopes_list

        # v1 legacy
        caddr  = get("clientAddr")
        label  = urllib.parse.unquote(get("label"))
        scopes_csv = get("scopes") or ""
        exp    = get("exp") or "0"
        nonce  = get("nonce")
        sig    = get("sig")
        ident  = get("i") or "client"
        if not (caddr and scopes_csv and exp and nonce and sig):
            return False, "Invite v1 missing fields", None, []
        ok_v1, client_pub_hex = verify_invite_sig_v1(caddr, v, scopes_csv, exp, nonce, sig)
        if not ok_v1 or not client_pub_hex:
            return False, "Bad v1 invite signature", None, []
        scopes_list = [s.strip() for s in scopes_csv.split(",") if s.strip()]
        dest_addr = caddr.strip() if re.fullmatch(r"[A-Za-z0-9_-]+\.([0-9a-fA-F]{64})", caddr.strip()) \
                   else f"{ident}.{client_pub_hex}"
        exp_unix = int(exp)
        grant = grant_for(client_pub_hex, scopes_list, exp_unix)
        _dm(dest_addr, {"v":1,"type":"grant","grant":grant}, tries=2, backoff_ms=20)
        _remember_peer(dest_addr, client_pub_hex, scopes_list)
        return True, f"GRANTED (v1) to {label or dest_addr}", dest_addr, scopes_list

    except Exception as e:
        return False, f"QR error: {e}", None, []

# ──────────────────────────────────────────────────────────────────────
# 8) QR scanner (exact method as qr_scan.py) + streaming loop
# ──────────────────────────────────────────────────────────────────────
def _encode_jpeg_b64_gray(gray: np.ndarray, quality: int) -> Optional[str]:
    try:
        ok, buf = cv2.imencode(".jpg", gray, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok: return None
        return base64.b64encode(buf.tobytes()).decode("ascii")
    except Exception:
        return None

def run():
    # wait NKN ready
    t0 = time.time()
    while not state.get("client_address"):
        if time.time() - t0 > 15:
            print("NKN not ready, exiting."); _shutdown()
        time.sleep(0.02)

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
    seen_invites: Set[str] = set()

    # per-scale encode cache (encode once per scale per frame)
    cache_b64: Dict[int, str] = {}
    cache_key_frame_id = 0

    try:
        while True:
            frame = grabber.read()
            if frame is None:
                time.sleep(0.001)
                continue

            # Optional working resize for scanning
            work, scale = resize_keep_aspect(frame, SCAN_MAX_WIDTH) if SCAN_MAX_WIDTH > 0 else (frame, 1.0)
            gray_scan = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)

            decoded_strings: List[str] = []
            polys_full: List[np.ndarray] = []

            # Multi
            try:
                retval, decoded_info, pts, _ = detector.detectAndDecodeMulti(gray_scan)
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
                    txt, pts, _ = detector.detectAndDecode(gray_scan)
                    if pts is not None and len(pts):
                        p = (np.asarray(pts).reshape(-1, 2) * scale).astype(np.float32)
                        polys_full.append(p)
                    if txt:
                        decoded_strings.append(txt)
                except Exception:
                    pass

            # Print ALL decoded strings (debugging)
            for s in decoded_strings:
                print(s)

            # Pair newly-seen invites
            for s in decoded_strings:
                if s in seen_invites: continue
                ok, msg, dest_addr, scopes = process_qr_payload(s)
                if ok and dest_addr:
                    print(msg)
                seen_invites.add(s)

            # Draw detections on original frame
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

            # STREAM to all remembered peers at STREAM_HZ
            tnow = time.time()
            if tnow - last_stream >= stream_period and peers:
                last_stream = tnow
                # bump cache frame id
                cache_key_frame_id += 1
                cache_b64.clear()

                # base grayscale once
                gray_full = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

                # prepare unique scales used by peers
                with peers_lock:
                    peer_items = list(peers.items())

                scales_needed = sorted({ max(10, min(100, int(meta["scale_pct"]))) for _, meta in peer_items })
                for pct in scales_needed:
                    if pct not in cache_b64:
                        if pct == 100:
                            img = gray_full
                        else:
                            h, w = gray_full.shape[:2]
                            nw = max(1, int(w * pct / 100.0))
                            nh = max(1, int(h * pct / 100.0))
                            img = cv2.resize(gray_full, (nw, nh), interpolation=cv2.INTER_AREA)
                        # encode with clamp if oversized
                        q = JPEG_QUALITY
                        b64 = _encode_jpeg_b64_gray(img, q)
                        if b64 and len(b64) * 3 // 4 > MAX_JPEG_BYTES:
                            # drop quality in small steps until under cap or hit floor
                            for q2 in (50, 45, 40, 35, 30, 28, 26, 24, 22, 20):
                                b64 = _encode_jpeg_b64_gray(img, q2)
                                if b64 and (len(b64) * 3 // 4) <= MAX_JPEG_BYTES:
                                    break
                        if b64:
                            cache_b64[pct] = b64

                # send to each peer using its scale; frames use tries=1, backoffMs=10
                for dest_addr, meta in peer_items:
                    if "video:rgb" not in (meta.get("scopes") or ["video:rgb"]): 
                        continue
                    pct = max(10, min(100, int(meta.get("scale_pct", DEFAULT_SCALE_PCT))))
                    b64 = cache_b64.get(pct)
                    if not b64:
                        continue
                    _dm(dest_addr, {"event":"frame-color","uuid":DEVICE_UUID,"data":b64}, tries=1, backoff_ms=10)

    finally:
        grabber.stop()
        try: cv2.destroyAllWindows()
        except Exception: pass

# ──────────────────────────────────────────────────────────────────────
# 9) main
# ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    print("→ Launching NKN device bridge …")
    run()
