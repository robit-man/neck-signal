#!/usr/bin/env python3
"""
neck_agent.py — Serial bridge + camera streamer with NKN-only signaling (JWT over DM)

What this does:
• Starts a Node sidecar (agent-node/nkn_agent_bridge.js) using nkn-sdk.
• Uses CLIENT_NKN_SEED_HEX (from .env) for a persistent agent NKN address.
• On NKN ready → DM SERVER_NKN_ADDRESS with {event:"login", uuid, password}.
• Retries DM login until {event:"login-ok"} comes back (then announces).
• Sends EVERYTHING as DMs to the server (NAT-friendly):
    - announce
    - peer-message / broadcast-message (outbound if you ever need)
    - frame-color / frame-depth  (base64 payloads)
• Receives control via DM:
    - {event:"peer-message", data:"X..,Y..,Z..,H..,S..,A..,R..,P.."}  -> forwarded to serial
    - {event:"broadcast-message", data:"..."} -> same

No Socket.IO anywhere.
"""

from __future__ import annotations
import os, sys, subprocess, venv, json, secrets, argparse, time, platform, pathlib, io, threading, re, shutil, base64
from typing import Optional
import requests, serial
from PIL import Image

# ────────────────────────────────────────────────────────────────────
# 0) venv bootstrap (just like before)
# ────────────────────────────────────────────────────────────────────
ROOT      = pathlib.Path(__file__).resolve().parent
VENV_DIR  = ROOT / "ag_venv"
VENV_PY   = VENV_DIR / ("Scripts" if platform.system() == "Windows" else "bin") / "python"
DEPS      = ["pyserial", "requests", "pillow"]

def in_venv() -> bool:
    return sys.prefix != sys.base_prefix

if not in_venv():
    if not VENV_DIR.exists():
        print("[agent] creating ag_venv …")
        venv.EnvBuilder(with_pip=True).create(VENV_DIR)
        subprocess.check_call([str(VENV_PY), "-m", "pip", "install",
                               "--quiet", "--upgrade", "pip", *DEPS])
    os.execv(str(VENV_PY), [str(VENV_PY), *sys.argv])

# ensure deps if user nuked them later
missing: list[str] = []
for pkg, mod in [("pyserial", "serial"),
                 ("requests", "requests"),
                 ("pillow", "PIL")]:
    try: __import__(mod)
    except ModuleNotFoundError: missing.append(pkg)
if missing:
    print("[agent] installing extra deps:", ", ".join(missing))
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *missing])
    os.execv(sys.executable, [sys.executable, *sys.argv])

# ────────────────────────────────────────────────────────────────────
# 1) .env + config
# ────────────────────────────────────────────────────────────────────
ENV_PATH = ROOT / ".env"
CFG_PATH = ROOT / "config.json"

def _load_env(path: pathlib.Path) -> dict:
    if not path.exists(): return {}
    out = {}
    for ln in path.read_text().splitlines():
        if "=" in ln and not ln.lstrip().startswith("#"):
            k,v = ln.split("=",1)
            out[k.strip()] = v.strip()
    return out

def _save_env(d: dict):
    ENV_PATH.write_text("".join(f"{k}={v}\n" for k,v in d.items()))

if not ENV_PATH.exists():
    pw = input("Shared password (matches server) [blank=random]:\n> ").strip() or secrets.token_urlsafe(16)
    ENV_PATH.write_text(
        "PEER_SHARED_SECRET=" + pw + "\n" +
        "CLIENT_NKN_SEED_HEX=" + secrets.token_hex(32) + "\n" +
        "SERVER_NKN_ADDRESS=\n" +
        "NKN_TOPIC_PREFIX=roko-signaling\n"
    )
    print("[agent] wrote .env")

env_map = _load_env(ENV_PATH)
for k,v in env_map.items():
    os.environ.setdefault(k, v)

def _hex64(s: str) -> str:
    s = (s or "").strip().lower().replace("0x","")
    return s if re.fullmatch(r"[0-9a-f]{64}", s) else ""

PEER_SECRET   = os.environ.get("PEER_SHARED_SECRET","").strip()
CLIENT_SEED   = _hex64(os.environ.get("CLIENT_NKN_SEED_HEX",""))
SERVER_NKN    = (os.environ.get("SERVER_NKN_ADDRESS","") or "").strip()
TOPIC_PREFIX  = (os.environ.get("NKN_TOPIC_PREFIX","roko-signaling") or "roko-signaling").strip()

if not CLIENT_SEED:
    CLIENT_SEED = secrets.token_hex(32)
    env_map["CLIENT_NKN_SEED_HEX"] = CLIENT_SEED
    _save_env(env_map)
    print("[agent] CLIENT_NKN_SEED_HEX missing/invalid → generated new seed and saved")

# config.json defaults
default_cfg = {
    "uuid": "",
    "camera_host": "http://127.0.0.1:8080",
    "color_path": "/camera/rs_color",
    "depth_path": "/camera/rs_depth",
    "frame_hz": 12,
}
cfg: dict = json.loads(CFG_PATH.read_text()) if CFG_PATH.exists() else {}
for k,v in default_cfg.items():
    cfg.setdefault(k, v)

ap = argparse.ArgumentParser()
ap.add_argument("-u","--uuid", help="Persistent agent UUID")
ap.add_argument("--frame-hz", type=int, help="camera streaming fps (default 12)")
ap.add_argument("--camera-host", help="base URL for camera endpoints")
ap.add_argument("--color-path", help="path for color endpoint")
ap.add_argument("--depth-path", help="path for depth endpoint")
cli = ap.parse_args()

def ask(t:str)->str: return input(t).strip()

UUID   = (cli.uuid or cfg.get("uuid") or ask("Agent UUID [blank=random]:\n> ")
          or "-".join(secrets.token_hex(2) for _ in range(4)))

if cli.frame_hz:     cfg["frame_hz"] = max(1, min(60, cli.frame_hz))
if cli.camera_host:  cfg["camera_host"] = cli.camera_host.rstrip("/")
if cli.color_path:   cfg["color_path"] = cli.color_path
if cli.depth_path:   cfg["depth_path"] = cli.depth_path

cfg.update(uuid=UUID)
CFG_PATH.write_text(json.dumps(cfg, indent=4))

print(f"[agent]   uuid={UUID}")
print(f"[agent]   camera_host={cfg['camera_host']}  frame_hz={cfg['frame_hz']}")
print(f"[agent]   NKN: ENABLED  prefix={TOPIC_PREFIX}  seed={'present' if bool(CLIENT_SEED) else 'missing!'}")
print("[agent]   Mode: NKN-ONLY.")

# ────────────────────────────────────────────────────────────────────
# 2) NKN sidecar (Node) — DM-only
# ────────────────────────────────────────────────────────────────────
N_NODE_DIR   = ROOT / "agent-node"
N_BRIDGE_JS  = N_NODE_DIR / "nkn_agent_bridge.js"
N_PKG_JSON   = N_NODE_DIR / "package.json"
NODE_BIN     = shutil.which("node")
NPM_BIN      = shutil.which("npm")
nkn_proc     = None
nkn_ready    = threading.Event()
nkn_address  = None

if not NODE_BIN or not NPM_BIN:
    sys.exit("‼️  Node.js and npm are required for NKN. Install Node 18+.")

def ensure_node_bridge():
    if not N_NODE_DIR.exists(): N_NODE_DIR.mkdir(parents=True)
    if not N_PKG_JSON.exists():
        print("[agent] initializing agent-node …")
        subprocess.check_call([NPM_BIN, "init", "-y"], cwd=N_NODE_DIR, stdout=subprocess.DEVNULL)
        subprocess.check_call([NPM_BIN, "install", "nkn-sdk@^1.3.1"], cwd=N_NODE_DIR)

    BRIDGE_SRC = r"""
/* nkn_agent_bridge.js — DM-only bridge. Robust handler for both message signatures. */
const nkn = require('nkn-sdk');
const readline = require('readline');

const SEED_HEX = (process.env.CLIENT_NKN_SEED_HEX || '').toLowerCase().replace(/^0x/,'');
const TOPIC_NS = process.env.NKN_TOPIC_PREFIX || 'roko-signaling';

function sendToPy(obj){ process.stdout.write(JSON.stringify(obj) + '\n'); }
function log(...args){ console.error('[nkn-bridge]', ...args); }
function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }
function isValidAddr(s){ return typeof s === 'string' && /^[A-Za-z0-9_-]+\.[0-9a-f]{64}$/.test(s.trim()); }

async function sendDMWithRetry(client, dest, data, tries=6){
  if (!isValidAddr(dest)) { log('dm aborted: invalid dest', dest); return false; }
  let lastErr = null;
  for (let i=0;i<tries;i++){
    try { await client.send(dest, JSON.stringify(data)); return true; }
    catch(e){
      lastErr = e; const msg = (e && e.message) ? e.message : String(e);
      const backoff = Math.min(1500, 150*Math.pow(2,i));
      log(`dm retry ${i+1}/${tries} -> ${dest}: ${msg}; backoff ${backoff}ms`);
      await sleep(backoff);
    }
  }
  log('dm failed:', lastErr && lastErr.message);
  return false;
}

(async () => {
  if (!/^[0-9a-f]{64}$/.test(SEED_HEX)) throw new RangeError('invalid hex seed');
  const client = new nkn.MultiClient({ seed: SEED_HEX, identifier: 'agent' });

  client.on('connect', () => {
    sendToPy({ type: 'ready', address: client.addr, topicPrefix: TOPIC_NS });
    log('ready at', client.addr, 'ns=', TOPIC_NS);
  });

  // Handle both signatures: (src,payload) and (msgObj)
  const onMessage = (a,b) => {
    let src, payload;
    if (a && typeof a === 'object' && a.payload !== undefined) {
      src = a.src; payload = a.payload;
    } else { src = a; payload = b; }
    try {
      const txt = Buffer.isBuffer(payload) ? payload.toString('utf8') : String(payload ?? '');
      let msg; try { msg = JSON.parse(txt); } catch { msg = { raw: txt }; }
      sendToPy({ type: 'nkn-message', src, msg });
    } catch(e) { /* ignore */ }
  };
  client.on('message', onMessage);

  const rl = readline.createInterface({ input: process.stdin });
  rl.on('line', async line => {
    if (!line) return;
    let cmd; try { cmd = JSON.parse(line); } catch { return; }
    if (cmd.type === 'dm' && cmd.to && cmd.data) {
      await sendDMWithRetry(client, cmd.to, cmd.data, 6);
    }
  });
})();
"""
    if not N_BRIDGE_JS.exists() or N_BRIDGE_JS.read_text() != BRIDGE_SRC:
        N_BRIDGE_JS.write_text(BRIDGE_SRC)

def start_node_bridge():
    global nkn_proc, nkn_address
    ensure_node_bridge()
    env = os.environ.copy()
    env["CLIENT_NKN_SEED_HEX"] = CLIENT_SEED
    env["NKN_TOPIC_PREFIX"] = TOPIC_PREFIX
    nkn_proc = subprocess.Popen(
        [NODE_BIN, str(N_BRIDGE_JS)],
        cwd=N_NODE_DIR,
        env=env,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1
    )

    def _read_stdout():
        global nkn_address
        for line in nkn_proc.stdout:
            line = (line or "").strip()
            if not line: continue
            try: msg = json.loads(line)
            except: continue
            if msg.get("type") == "ready":
                nkn_address = msg.get("address")
                print(f"[agent] NKN ready: {nkn_address}  (topics prefix: {TOPIC_PREFIX})")
                nkn_ready.set()
            elif msg.get("type") == "nkn-message":
                _handle_nkn_inbound(msg.get("src"), msg.get("msg"))
    threading.Thread(target=_read_stdout, daemon=True).start()

    def _read_stderr():
        for line in nkn_proc.stderr:
            sys.stderr.write(line)
    threading.Thread(target=_read_stderr, daemon=True).start()

def stop_node_bridge():
    if nkn_proc:
        try: nkn_proc.terminate()
        except: pass

def nkn_dm(to_addr: str, data: dict):
    if not nkn_ready.is_set() or not nkn_proc or not nkn_proc.stdin: return
    try:
        nkn_proc.stdin.write(json.dumps({"type":"dm","to":to_addr,"data":data}) + "\n")
        nkn_proc.stdin.flush()
    except: pass

# ────────────────────────────────────────────────────────────────────
# 3) Serial + command handling
# ────────────────────────────────────────────────────────────────────
SER_POSSIBLE = ["/dev/ttyUSB0","/dev/ttyUSB1","/dev/tty0","/dev/tty1","COM3","COM4"]
ser: Optional[serial.Serial] = None
while ser is None:
    for p in SER_POSSIBLE:
        try:
            ser = serial.Serial(p, 115200, timeout=1)
            print("[agent] opened serial:", p)
            break
        except Exception:
            ser = None
    if ser is None:
        print("[agent] no serial – retry in 3 s"); time.sleep(3)

ALLOWED = {
    "X": (-700, 700, int), "Y": (-700, 700, int), "Z": (-700, 700, int),
    "H": (0, 70, int),     "S": (0, 10, float),   "A": (0, 10, float),
    "R": (-700, 700, int), "P": (-700, 700, int),
}
state = {k: (1.0 if k in ("S","A") else 0) for k in ALLOWED}

def validate(cmd:str)->bool:
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

def merge(cmd:str)->str:
    if cmd.lower()=="home":
        for k in state: state[k]=1.0 if k in ("S","A") else 0
        return "HOME"
    for tok in cmd.split(","):
        m = re.match(r"^([XYZHSARP])(-?\d+(?:\.\d+)?)$", tok.strip())
        if m:
            k,val=m.group(1),m.group(2); state[k]=ALLOWED[k][2](val)
    return ",".join(f"{k}{state[k]}" for k in ["X","Y","Z","H","S","A","R","P"])

def handle_cmd(raw:str):
    if not validate(raw):
        print("[agent] ✗ invalid:", raw); return
    full = merge(raw)
    try:
        ser.write((full+"\n").encode())
        print(f"[agent] → serial  {full}")
    except Exception as e:
        print("[agent] serial error:", e)

print("[agent] example full cmd:  X100,Y-50,Z0,H30,S1,A1,R0,P0")
ser.write(b"HOME\n"); print("[agent] → serial  HOME")

# ────────────────────────────────────────────────────────────────────
# 4) Camera streamer (DMs frames to server)
# ────────────────────────────────────────────────────────────────────
def crop_to_aspect(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    tw, th = target_w, target_h
    W, H = img.size
    target_ratio = tw / th
    src_ratio = W / H
    if abs(src_ratio - target_ratio) < 1e-3:
        return img
    if src_ratio > target_ratio:
        new_w = int(H * target_ratio)
        left = (W - new_w) // 2
        return img.crop((left, 0, left + new_w, H))
    else:
        new_h = int(W / target_ratio)
        top = (H - new_h) // 2
        return img.crop((0, top, W, top + new_h))

class CameraStreamer:
    def __init__(self, cfg: dict):
        self.host = cfg["camera_host"].rstrip("/")
        self.color_url = self.host + cfg["color_path"]
        self.depth_url = self.host + cfg["depth_path"]
        self.hz = max(1, min(60, int(cfg.get("frame_hz", 12))))
        self.period = 1.0 / self.hz
        self._stop = threading.Event()
        self._t = None

    def start(self):
        if self._t and self._t.is_alive(): return
        self._stop.clear()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()
        print(f"[agent] camera streaming loop @ {self.hz} FPS")

    def stop(self):
        self._stop.set()

    def _loop(self):
        session = requests.Session()
        headers = {"Connection": "keep-alive"}
        target_size = (360, 640)  # (W,H)
        while not self._stop.is_set():
            if not nkn_ready.is_set() or not SERVER_NKN:
                time.sleep(0.1); continue
            t0 = time.time()
            color_b64 = None
            depth_b64 = None

            try:
                r = session.get(self.color_url, timeout=1.5, headers=headers)
                if r.status_code == 200 and r.content:
                    with Image.open(io.BytesIO(r.content)) as im:
                        im = im.convert("RGB")
                        im = crop_to_aspect(im, *target_size)
                        im = im.resize(target_size, Image.BICUBIC)
                        buf = io.BytesIO()
                        im.save(buf, format="JPEG", quality=68, optimize=True)  # keep small for DM
                        color_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            except Exception:
                pass

            try:
                r = session.get(self.depth_url, timeout=1.5, headers=headers)
                if r.status_code == 200 and r.content:
                    payload = r.content
                    try:
                        with Image.open(io.BytesIO(payload)) as im:
                            W,H = im.size
                            if (W,H) != target_size:
                                im = im.convert("L").resize(target_size, Image.NEAREST)
                                buf = io.BytesIO()
                                # use PNG-8; still usually small
                                im.save(buf, format="PNG", optimize=True)
                                payload = buf.getvalue()
                    except Exception:
                        # raw payload
                        pass
                    depth_b64 = base64.b64encode(payload).decode("ascii")
            except Exception:
                pass

            # DM frames
            if color_b64:
                nkn_dm(SERVER_NKN, {"event":"frame-color","uuid":UUID,"data":color_b64})
            if depth_b64:
                nkn_dm(SERVER_NKN, {"event":"frame-depth","uuid":UUID,"data":depth_b64})

            dt = time.time() - t0
            if dt < self.period:
                time.sleep(self.period - dt)

camera_streamer = CameraStreamer(cfg)

# ────────────────────────────────────────────────────────────────────
# 5) NKN JWT login + announce (DM)
# ────────────────────────────────────────────────────────────────────
def nkn_login_and_announce():
    if not SERVER_NKN:
        print("‼️  SERVER_NKN_ADDRESS empty in .env — set it to sig.<64hex> from the server.")
        return
    if not PEER_SECRET:
        print("‼️  PEER_SHARED_SECRET empty in .env — set it to match the server.")
        return
    # wait for NKN
    while not nkn_ready.wait(timeout=0.25):
        pass

    backoff = 1.0
    while True:
        print(f"[agent] DM login → {SERVER_NKN} (uuid={UUID})")
        nkn_dm(SERVER_NKN, {"event":"login","uuid":UUID,"password":PEER_SECRET})
        # server DM reply will be handled in _handle_nkn_inbound
        for _ in range(20):  # ~2s
            if getattr(nkn_login_and_announce, "ok", False):
                break
            time.sleep(0.1)
        if getattr(nkn_login_and_announce, "ok", False):
            break
        print(f"[agent] DM login retry in {backoff:.1f}s …")
        time.sleep(backoff); backoff = min(backoff*1.6, 8.0)

    # announce presence (optional/diagnostic)
    nkn_dm(SERVER_NKN, {"event":"announce","uuid":UUID,"data":{"hello":True}})

def _handle_nkn_inbound(src_addr: str | None, msg: dict):
    if not isinstance(msg, dict): return
    ev = msg.get("event")

    if ev == "login-ok":
        setattr(nkn_login_and_announce, "ok", True)
        print("[agent] login-ok (JWT issued via NKN DM)")

    elif ev == "login-error":
        print(f"[agent] login-error: {msg.get('error','unknown')}")

    elif ev in ("peer-message","broadcast-message"):
        data = msg.get("data")
        if isinstance(data, str):
            handle_cmd(data.strip())

    # (You can add more DM events here if you need them later.)

# ────────────────────────────────────────────────────────────────────
# 6) Main
# ────────────────────────────────────────────────────────────────────
print("[agent] bridge live – Ctrl-C to quit")

start_node_bridge()
camera_streamer.start()

# Start the DM login worker
threading.Thread(target=nkn_login_and_announce, daemon=True).start()

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    try: camera_streamer.stop()
    except: ...
    try: ser.close()
    except: ...
    try: stop_node_bridge()
    except: ...
    print("[agent] bye")
