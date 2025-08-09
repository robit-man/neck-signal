#!/usr/bin/env python3
"""
nkn_test.py — DM-only NKN viewer client with OpenCV display

- Discovers server sig/topic from http://127.0.0.1:<port>/ (default 3000), or prompts.
- Sends `viewer-hello` (and `viewer-bye` on exit) for the provided --uuid.
- Receives DM frames: {"event":"frame-color"|"frame-depth","uuid", "data":"<base64>"}
- Decodes and displays with OpenCV in live windows (color + optional depth).
- Prints basic stats (fps, throughput).

Requirements auto-installed into local venv on first run:
  numpy, opencv-python
"""

# ──────────────────────────────────────────────────────────────────────
# I. bootstrap venv (stdlib only)
# ──────────────────────────────────────────────────────────────────────
import os, sys, subprocess
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
    # Install viewer deps into the venv, then re-exec inside the venv.
    subprocess.check_call([str(PY_VENV), "-m", "pip", "install", "--quiet", "numpy", "opencv-python"])
    os.execv(str(PY_VENV), [str(PY_VENV), *sys.argv])

# ──────────────────────────────────────────────────────────────────────
# II. imports (now inside venv Python)
# ──────────────────────────────────────────────────────────────────────
import json, threading, secrets, time, signal, shutil, re, argparse, base64
from datetime import datetime, timezone
from subprocess import Popen, PIPE
from typing import Optional, Dict, Any, Tuple
import urllib.request, urllib.error

import numpy as np
import cv2

# ──────────────────────────────────────────────────────────────────────
# III. args & .env persistence
# ──────────────────────────────────────────────────────────────────────
cli = argparse.ArgumentParser()
cli.add_argument("--server-host", default=os.environ.get("SIG_HOST", "127.0.0.1"),
                 help="Host for HTTP bootstrap (default 127.0.0.1)")
cli.add_argument("--server-port", type=int, default=int(os.environ.get("SIG_PORT", "3000")),
                 help="Port for HTTP bootstrap (default 3000)")
cli.add_argument("--uuid", required=False, default=os.environ.get("VIEW_UUID", ""),
                 help="Agent UUID to view (the robot/agent to subscribe to)")
cli.add_argument("--show-depth", action="store_true",
                 help="Display depth window as well")
cli.add_argument("--fps", type=float, default=30.0,
                 help="UI refresh cap (OpenCV) (default 30)")
args = cli.parse_args()

ENV_PATH = BASE_DIR / ".env"  # stores client seed + last inputs

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
if "CLIENT_NKN_SEED_HEX" not in dotenv:
    dotenv["CLIENT_NKN_SEED_HEX"] = secrets.token_hex(32)  # 32 bytes -> 64 hex chars

CLIENT_SEED_HEX = dotenv["CLIENT_NKN_SEED_HEX"]

# ──────────────────────────────────────────────────────────────────────
# IV. bootstrap: discover sig/topic from server or prompt
# ──────────────────────────────────────────────────────────────────────
def _fetch_bootstrap(host: str, port: int) -> Optional[Dict[str, str]]:
    url = f"http://{host}:{port}/"
    try:
        with urllib.request.urlopen(url, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict) and data.get("ok"):
                nkn_addr = data.get("nknAddress")
                topic    = data.get("topicPrefix") or "roko-signaling"
                if nkn_addr and isinstance(nkn_addr, str):
                    return {"SERVER_SIG": nkn_addr, "TOPIC_PREFIX": topic}
    except Exception:
        return None
    return None

def _prompt(label: str, default: Optional[str] = None) -> str:
    prompt = f"{label}"
    if default:
        prompt += f" [{default}]"
    prompt += ":\n> "
    try:
        val = input(prompt).strip()
    except EOFError:
        val = ""
    return val or (default or "")

bootstrap = _fetch_bootstrap(args.server_host, args.server_port)

if bootstrap:
    SERVER_SIG   = bootstrap["SERVER_SIG"]
    TOPIC_PREFIX = bootstrap["TOPIC_PREFIX"]
    print(f"→ Discovered server via HTTP: sig={SERVER_SIG}  topicPrefix={TOPIC_PREFIX}")
else:
    print("Enter the server's NKN address (sig) and topic prefix.")
    SERVER_SIG   = _prompt("Server NKN address (sig)", dotenv.get("SERVER_NKN_ADDRESS", ""))
    TOPIC_PREFIX = _prompt("Topic prefix", dotenv.get("NKN_TOPIC_PREFIX", "roko-signaling"))

VIEW_UUID = (args.uuid or os.environ.get("VIEW_UUID") or _prompt("UUID of agent to view", dotenv.get("VIEW_UUID",""))).strip()

if not SERVER_SIG:
    sys.exit("No server NKN address provided. Exiting.")
if not VIEW_UUID:
    sys.exit("No agent UUID provided. Exiting.")

dotenv["SERVER_NKN_ADDRESS"] = SERVER_SIG
dotenv["NKN_TOPIC_PREFIX"]   = TOPIC_PREFIX
dotenv["VIEW_UUID"]          = VIEW_UUID
_save_env(ENV_PATH, dotenv)

# ──────────────────────────────────────────────────────────────────────
# V. Prepare Node bridge (nkn-sdk)
# ──────────────────────────────────────────────────────────────────────
NODE_DIR = BASE_DIR / "client-bridge"
NODE_BIN = shutil.which("node")
NPM_BIN  = shutil.which("npm")
if not NODE_BIN or not NPM_BIN:
    sys.exit("‼️  Node.js and npm are required. Install Node 18+.")

PKG_JSON  = NODE_DIR / "package.json"
BRIDGE_JS = NODE_DIR / "nkn_client_bridge.js"

if not NODE_DIR.exists():
    NODE_DIR.mkdir(parents=True)

if not PKG_JSON.exists():
    print("→ Initializing client-bridge …")
    subprocess.check_call([NPM_BIN, "init", "-y"], cwd=NODE_DIR)
    subprocess.check_call([NPM_BIN, "install", "nkn-sdk@^1.3.1"], cwd=NODE_DIR)

BRIDGE_SRC = r"""
/* nkn_client_bridge.js — NAT-friendly DM-only bridge for viewer.
   - Persistent client address via CLIENT_NKN_SEED_HEX
   - DM-only sending to server 'sig' address
   - JSON line IPC with Python
   - Robust inbound handling for multiple nkn-sdk signatures
*/
const nkn = require('nkn-sdk');
const readline = require('readline');

const SEED_HEX = (process.env.CLIENT_NKN_SEED_HEX || '').toLowerCase().replace(/^0x/,'');
const TOPIC_NS = process.env.NKN_TOPIC_PREFIX || 'roko-signaling';

function log(...args){ console.error('[client-bridge]', ...args); }
function sendToPy(obj){ process.stdout.write(JSON.stringify(obj) + '\n'); }
function isValidAddr(s){
  return typeof s === 'string' && /^[A-Za-z0-9_-]+\.[0-9a-f]{64}$/.test(s.trim());
}
function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }

async function sendDMWithRetry(client, dest, data, tries=8){
  if (!isValidAddr(dest)) { log('dm aborted: invalid dest', dest); return false; }
  let lastErr = null;
  for (let i=0;i<tries;i++){
    try { await client.send(dest, JSON.stringify(data)); return true; }
    catch(e){
      lastErr = e; const msg = (e && e.message) ? e.message : String(e);
      const backoff = Math.min(1500, 120*Math.pow(2,i));
      log(`dm retry ${i+1}/${tries} -> ${dest}: ${msg}; backoff ${backoff}ms`);
      await sleep(backoff);
    }
  }
  log('dm failed:', lastErr && lastErr.message);
  return false;
}

/** Normalize inbound message across SDK variants.
 * Possible shapes:
 *  1) on('message', (src, payload))                          // classic
 *  2) on('message', (obj)) where obj = {src, payload?}       // some builds
 *  3) on('message', (obj)) where obj = {src, data?}          // alt field
 *  4) payload can be Buffer|string|undefined
 */
function normalizeInbound(a, b) {
  // Case 2/3: a is an object with fields
  if (a && typeof a === 'object' && (a.payload !== undefined || a.data !== undefined || a.src !== undefined)) {
    const src = a.src || a.from || a.addr || '';
    const payload = (a.payload !== undefined) ? a.payload : a.data;
    return { src, payload };
  }
  // Case 1: (src, payload)
  return { src: a, payload: b };
}

function payloadToString(payload){
  if (payload == null) return '';
  try {
    if (Buffer.isBuffer(payload)) return payload.toString('utf8');
    if (typeof payload === 'string') return payload;
    // some SDKs hand an object already
    return typeof payload === 'object' ? JSON.stringify(payload) : String(payload);
  } catch {
    return '';
  }
}

(async () => {
  if (!/^[0-9a-f]{64}$/.test(SEED_HEX)) throw new RangeError('invalid hex seed');
  const client = new nkn.MultiClient({ seed: SEED_HEX, identifier: 'viewer' });

  client.on('connect', () => {
    sendToPy({ type: 'ready', address: client.addr, topicPrefix: TOPIC_NS });
    log('ready at', client.addr);
  });

  // Accept any signature shape the SDK throws at us.
  client.on('message', (a, b) => {
    try {
      const { src, payload } = normalizeInbound(a, b);
      const txt = payloadToString(payload);
      let msg = null;
      try { msg = JSON.parse(txt); } catch { msg = { raw: txt }; }
      sendToPy({ type: 'nkn-message', src, msg });
    } catch (e) {
      log('bad inbound payload', e);
    }
  });

  const rl = readline.createInterface({ input: process.stdin });
  rl.on('line', async (line) => {
    if (!line) return;
    let cmd; try { cmd = JSON.parse(line); } catch { return; }
    try {
      if (cmd.type === 'dm') {
        await sendDMWithRetry(client, String(cmd.to||'').trim(), cmd.data, 8);
      } else if (cmd.type === 'pub') {
        await client.publish(TOPIC_NS + '.' + cmd.topic, JSON.stringify(cmd.data));
      }
    } catch (e) {
      log('stdin handler error', e);
    }
  });
})();

"""
if not BRIDGE_JS.exists() or BRIDGE_JS.read_text() != BRIDGE_SRC:
    BRIDGE_JS.write_text(BRIDGE_SRC)

# ──────────────────────────────────────────────────────────────────────
# VI. Launch bridge & IPC helpers
# ──────────────────────────────────────────────────────────────────────
bridge_env = os.environ.copy()
bridge_env["CLIENT_NKN_SEED_HEX"] = CLIENT_SEED_HEX
bridge_env["NKN_TOPIC_PREFIX"]   = dotenv["NKN_TOPIC_PREFIX"]

bridge = Popen(
    [str(shutil.which("node")), str(BRIDGE_JS)],
    cwd=NODE_DIR, env=bridge_env,
    stdin=PIPE, stdout=PIPE, stderr=PIPE,
    text=True, bufsize=1
)

state: Dict[str, Any] = {"client_address": None, "topic_prefix": dotenv["NKN_TOPIC_PREFIX"]}

# latest decoded frames (numpy arrays)
latest_color = {"img": None, "ts": 0.0, "bytes": 0}
latest_depth = {"img": None, "ts": 0.0, "bytes": 0}
lock = threading.Lock()

stats = {
    "frames": 0,
    "bytes": 0,
    "t0": time.time(),
    "fps": 0.0,
    "bps": 0.0
}

def _update_stats(nbytes: int):
    with lock:
        stats["frames"] += 1
        stats["bytes"]  += nbytes
        now = time.time()
        dt  = now - stats["t0"]
        if dt >= 1.0:
            stats["fps"] = stats["frames"] / dt
            stats["bps"] = stats["bytes"] / dt
            stats["frames"] = 0
            stats["bytes"] = 0
            stats["t0"] = now

def _decode_image(b64: str) -> Optional[np.ndarray]:
    try:
        data = base64.b64decode(b64.encode("ascii"), validate=True)
    except Exception:
        try:
            data = base64.b64decode(b64.encode("ascii"))
        except Exception:
            return None
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)  # handles JPEG/PNG
    return img

def _decode_depth(b64: str) -> Optional[np.ndarray]:
    # Try to decode as image first; else try raw uint16 fallback.
    try:
        data = base64.b64decode(b64.encode("ascii"), validate=True)
    except Exception:
        try:
            data = base64.b64decode(b64.encode("ascii"))
        except Exception:
            return None
    # Try image decode
    arr = np.frombuffer(data, dtype=np.uint8)
    dimg = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
    if dimg is not None:
        if dimg.ndim == 3:
            dimg = cv2.cvtColor(dimg, cv2.COLOR_BGR2GRAY)
        return dimg
    # Fallback: try interpret as uint16 vector and square reshape guess (not ideal)
    if len(data) % 2 == 0:
        u16 = np.frombuffer(data, dtype="<u2")
        side = int(np.sqrt(u16.size))
        if side*side == u16.size:
            return u16.reshape(side, side).astype(np.uint16)
    return None

def _normalize_depth_for_display(d: np.ndarray) -> np.ndarray:
    # Normalize using robust percentiles to ignore outliers
    d = d.astype(np.float32)
    vmin = np.percentile(d, 5)
    vmax = np.percentile(d, 95)
    if vmax <= vmin: vmax = vmin + 1.0
    norm = np.clip((d - vmin) / (vmax - vmin), 0.0, 1.0)
    disp = (norm * 255.0).astype(np.uint8)
    disp = cv2.applyColorMap(disp, cv2.COLORMAP_TURBO)
    return disp

def _bridge_reader():
    for line in bridge.stdout:
        line = line.strip()
        if not line: continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("type") == "ready":
            state["client_address"] = msg.get("address")
            state["topic_prefix"]   = msg.get("topicPrefix") or dotenv["NKN_TOPIC_PREFIX"]
            print(f"→ NKN client ready: {state['client_address']} (topics prefix: {state['topic_prefix']})")
            # Register as a viewer for this UUID
            _dm("viewer-hello", {"uuid": VIEW_UUID})
        elif msg.get("type") == "nkn-message":
            src = msg.get("src")
            body = msg.get("msg") or {}
            ev = body.get("event")
            if ev == "viewer-ack":
                print(f"✓ viewer-ack from server for uuid={VIEW_UUID}")
            elif ev == "frame-color" and body.get("uuid") == VIEW_UUID:
                b64 = body.get("data")
                if isinstance(b64, str):
                    img = _decode_image(b64)
                    if img is not None:
                        with lock:
                            latest_color["img"] = img
                            latest_color["ts"]  = time.time()
                            latest_color["bytes"] = len(b64) * 3 // 4  # approx decoded bytes
                        _update_stats(len(b64) * 3 // 4)
            elif ev == "frame-depth" and body.get("uuid") == VIEW_UUID and args.show_depth:
                b64 = body.get("data")
                if isinstance(b64, str):
                    dimg = _decode_depth(b64)
                    if dimg is not None:
                        with lock:
                            latest_depth["img"] = dimg
                            latest_depth["ts"]  = time.time()
                            latest_depth["bytes"] = len(b64) * 3 // 4
            # Optional: print other DMs for debugging
            # else:
            #     try: print(f"[inbound] from {src}: {json.dumps(body)[:200]}")
            #     except: pass

def _bridge_err_reader():
    for line in bridge.stderr:
        sys.stderr.write(line)

threading.Thread(target=_bridge_reader, daemon=True).start()
threading.Thread(target=_bridge_err_reader, daemon=True).start()

def _bridge_send(obj: dict):
    try:
        bridge.stdin.write(json.dumps(obj) + "\n")
        bridge.stdin.flush()
    except Exception as e:
        print("bridge send error:", e)

def _looks_like_nkn_addr(s: str) -> bool:
    s = (s or "").strip()
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+\.[0-9a-f]{64}", s))

def _dm(event: str, data: dict):
    if not _looks_like_nkn_addr(SERVER_SIG):
        print(f"⚠ skipped DM '{event}': invalid NKN destination '{SERVER_SIG}'")
        return
    _bridge_send({"type":"dm", "to": SERVER_SIG, "data": {"event": event, **data}})

def _shutdown(*_):
    try:
        _dm("viewer-bye", {"uuid": VIEW_UUID})
    except Exception:
        pass
    try:
        if bridge and bridge.poll() is None:
            bridge.terminate()
    except Exception:
        pass
    cv2.destroyAllWindows()
    sys.exit(0)

signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

# ──────────────────────────────────────────────────────────────────────
# VII. UI loop (OpenCV)
# ──────────────────────────────────────────────────────────────────────
def _overlay_stats(img: np.ndarray, label: str) -> np.ndarray:
    h, w = img.shape[:2]
    pad = 6
    box = img.copy()
    cv2.rectangle(box, (0,0), (w, 42), (0,0,0), -1)
    alpha = 0.45
    cv2.addWeighted(box, alpha, img, 1-alpha, 0, img)
    txt = f"{label}  uuid={VIEW_UUID}  fps={stats['fps']:.1f}  rate={stats['bps']*8/1e6:.2f} Mbps"
    cv2.putText(img, txt, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 1, cv2.LINE_AA)
    return img

def ui_loop():
    wnd_color = f"color [{VIEW_UUID}]"
    wnd_depth = f"depth [{VIEW_UUID}]"
    cv2.namedWindow(wnd_color, cv2.WINDOW_NORMAL)
    if args.show_depth:
        cv2.namedWindow(wnd_depth, cv2.WINDOW_NORMAL)

    frame_interval = max(1.0/float(args.fps), 1.0/60.0)
    last_draw = 0.0

    while True:
        now = time.time()
        if now - last_draw >= frame_interval:
            last_draw = now
            cimg = None; dvis = None
            with lock:
                if latest_color["img"] is not None:
                    cimg = latest_color["img"].copy()
                if args.show_depth and latest_depth["img"] is not None:
                    dsrc = latest_depth["img"]
                    dvis = _normalize_depth_for_display(dsrc)

            if cimg is not None:
                cimg = _overlay_stats(cimg, "COLOR")
                cv2.imshow(wnd_color, cimg)
            if args.show_depth and dvis is not None:
                dvis = _overlay_stats(dvis, "DEPTH")
                cv2.imshow(wnd_depth, dvis)

        # process UI events
        k = cv2.waitKey(1) & 0xFF
        if k == 27 or k == ord('q'):
            _shutdown()

        time.sleep(0.001)

# ──────────────────────────────────────────────────────────────────────
# VIII. Main
# ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("→ Launching NKN client bridge …")

    # Wait briefly for NKN MultiClient to be fully ready
    for _ in range(400):
        if state["client_address"]:
            break
        time.sleep(0.025)

    if not state["client_address"]:
        print("Bridge did not become ready. Exiting.")
        _shutdown()

    if not _looks_like_nkn_addr(dotenv.get("SERVER_NKN_ADDRESS","")):
        dotenv["SERVER_NKN_ADDRESS"] = SERVER_SIG
        _save_env(ENV_PATH, dotenv)

    print("→ Viewer running. Press 'q' or ESC to quit.")
    ui_loop()
