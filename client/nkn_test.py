#!/usr/bin/env python3
"""
nkn_test.py — NAT-friendly client for signaling_server.py (DM-only to sig)

What it does:
- Auto-discovers the server's NKN address (sig) and topicPrefix from http://127.0.0.1:<PORT>/ (default 3000).
- Falls back to prompt if HTTP discover fails.
- Sends ALL events as DIRECT MESSAGES to the server's NKN address:
    announce, peer-message, broadcast-message, frame-color, frame-depth
- Retries DM send on "InvalidDestinationError: no destinations" with a small backoff.

Notes:
- On-chain subscriptions are disabled by default (wallet funding required).
- Requires Node.js 18+ and npm (no Python third-party deps).
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
    os.execv(str(PY_VENV), [str(PY_VENV), *sys.argv])

# ──────────────────────────────────────────────────────────────────────
# II. imports (now inside venv Python)
# ──────────────────────────────────────────────────────────────────────
import json, threading, secrets, time, signal, shutil, re, argparse
from datetime import datetime, timezone
from subprocess import Popen, PIPE
from typing import Optional, Dict, Any
import urllib.request, urllib.error

# ──────────────────────────────────────────────────────────────────────
# III. args & .env persistence
# ──────────────────────────────────────────────────────────────────────
cli = argparse.ArgumentParser()
cli.add_argument("--server-host", default=os.environ.get("SIG_HOST", "127.0.0.1"),
                 help="Host of local signaling server for bootstrap (default 127.0.0.1)")
cli.add_argument("--server-port", type=int, default=int(os.environ.get("SIG_PORT", "3000")),
                 help="Port of local signaling server for bootstrap (default 3000)")
args = cli.parse_args()

ENV_PATH = BASE_DIR / ".env"  # stores client-side seed + last inputs

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

if not SERVER_SIG:
    sys.exit("No server NKN address provided. Exiting.")

dotenv["SERVER_NKN_ADDRESS"] = SERVER_SIG
dotenv["NKN_TOPIC_PREFIX"]   = TOPIC_PREFIX
_save_env(ENV_PATH, dotenv)

# ──────────────────────────────────────────────────────────────────────
# V. Prepare Node bridge (nkn-sdk)
# ──────────────────────────────────────────────────────────────────────
NODE_DIR = BASE_DIR / "client-bridge"
NODE_BIN = shutil.which("node")
NPM_BIN  = shutil.which("npm")
if not NODE_BIN or not NPM_BIN:
    sys.exit("‼️  Node.js and npm are required for this client. Please install Node 18+.")

PKG_JSON = NODE_DIR / "package.json"
BRIDGE_JS = NODE_DIR / "nkn_client_bridge.js"

if not NODE_DIR.exists():
    NODE_DIR.mkdir(parents=True)

if not PKG_JSON.exists():
    print("→ Initializing client-bridge …")
    subprocess.check_call([NPM_BIN, "init", "-y"], cwd=NODE_DIR)
    subprocess.check_call([NPM_BIN, "install", "nkn-sdk@^1.3.0"], cwd=NODE_DIR)

BRIDGE_SRC = r"""
/* nkn_client_bridge.js — NAT-friendly DM-only bridge.
   - Persistent client address via CLIENT_NKN_SEED_HEX
   - DM-only sending to a server 'sig' address to punch through NAT
   - JSON line IPC with Python
*/
const nkn = require('nkn-sdk');

const SEED_HEX = process.env.CLIENT_NKN_SEED_HEX;
const TOPIC_NS = process.env.NKN_TOPIC_PREFIX || 'roko-signaling';
const ENABLE_SUBSCRIBE = process.env.ENABLE_SUBSCRIBE === '1'; // off by default

function log(...args){ console.error('[client-bridge]', ...args); }
function sendToPy(obj){ process.stdout.write(JSON.stringify(obj) + '\n'); }
function isValidAddr(s){
  return typeof s === 'string'
    && /^[A-Za-z0-9_-]+\.[0-9a-f]{64}$/.test(s.trim());
}
function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }

async function sendDMWithRetry(client, dest, data, tries=5){
  if (!isValidAddr(dest)) {
    log('dm aborted: invalid destination', dest);
    return false;
  }
  let lastErr = null;
  for (let i=0; i<tries; i++){
    try {
      await client.send(dest, JSON.stringify(data));
      return true;
    } catch (e) {
      lastErr = e;
      const msg = (e && e.message) ? e.message : String(e);
      // NKN SDK sometimes throws "InvalidDestinationError: no destinations" until paths are ready.
      if (i < tries-1) {
        const backoff = Math.min(1500, 150 * Math.pow(2, i));
        log(`dm retry ${i+1}/${tries} -> ${dest}: ${msg}; backing off ${backoff}ms`);
        await sleep(backoff);
        continue;
      } else {
        log('dm failed (giving up):', msg);
      }
    }
  }
  return false;
}

(async () => {
  const client = new nkn.MultiClient({ seed: SEED_HEX, identifier: 'client' });

  client.on('connect', () => {
    sendToPy({ type: 'ready', address: client.addr, topicPrefix: TOPIC_NS });
    log('ready at', client.addr);
  });

  client.on('message', (src, payload) => {
    try {
      const txt = payload.toString();
      let msg = null;
      try { msg = JSON.parse(txt); } catch { msg = { raw: txt }; }
      sendToPy({ type: 'nkn-message', src, msg });
    } catch (e) {
      log('bad inbound payload', e);
    }
  });

  // (Optional) Subscriptions kept OFF by default — unfunded wallet would error.
  if (ENABLE_SUBSCRIBE) {
    const topics = ['announce','peer-message','broadcast-message','frame-color','frame-depth']
      .map(t => TOPIC_NS + '.' + t);
    const subKeep = async () => {
      try {
        await Promise.all(topics.map(t => client.subscribe(t, { duration: 3600 })));
      } catch (e) { log('subscribe error', e); }
      setTimeout(subKeep, 30 * 60 * 1000);
    };
    subKeep();
  }

  const readline = require('readline').createInterface({ input: process.stdin });

  readline.on('line', async (line) => {
    if (!line) return;
    let cmd;
    try { cmd = JSON.parse(line); }
    catch (e) { return log('stdin parse error', e); }

    try {
      // DM-only path: the Python side will always send {type:"dm", to, data}
      if (cmd.type === 'dm') {
        const dest = (cmd.to || '').trim();
        await sendDMWithRetry(client, dest, cmd.data, 6);
      } else if (cmd.type === 'pub') {
        // allowed but not used by default in NAT mode
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
# OFF by default to avoid unfunded wallet errors.
bridge_env.setdefault("ENABLE_SUBSCRIBE", "0")

bridge = Popen(
    [str(shutil.which("node")), str(BRIDGE_JS)],
    cwd=NODE_DIR, env=bridge_env,
    stdin=PIPE, stdout=PIPE, stderr=PIPE,
    text=True, bufsize=1
)

state: Dict[str, Any] = {"client_address": None, "topic_prefix": dotenv["NKN_TOPIC_PREFIX"]}

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
        elif msg.get("type") == "nkn-message":
            src = msg.get("src")
            payload = msg.get("msg")
            try:
                printable = json.dumps(payload, ensure_ascii=False)
            except Exception:
                printable = str(payload)
            print(f"[inbound] from {src}: {printable}")

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

# ──────────────────────────────────────────────────────────────────────
# VII. DM everything to the server (NAT-friendly)
# ──────────────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _gen_uuid() -> str:
    return "-".join(secrets.token_hex(2) for _ in range(4))

def _looks_like_nkn_addr(s: str) -> bool:
    s = (s or "").strip()
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+\.[0-9a-f]{64}", s))

def dm(event: str, data: dict, dest: str):
    if not _looks_like_nkn_addr(dest):
        print(f"⚠ skipped DM '{event}': invalid NKN destination '{dest}'")
        return
    _bridge_send({"type": "dm", "to": dest.strip(), "data": {"event": event, **data}})

def run_handshake(sig_addr: str):
    # tiny handshake to warm up paths; server will treat as 'announce'
    dm("announce", {"uuid": _gen_uuid(), "data": {"hello": True, "ts": _now_iso()}}, sig_addr)

def send_all(sig_addr: str):
    uid = _gen_uuid()
    ts  = _now_iso()

    dm("announce", {"uuid": uid, "data": {"from": state.get("client_address"), "ts": ts}}, sig_addr)
    print("✓ DM: announce")

    dm("peer-message", {"data": f"hello from {state.get('client_address')} @ {ts}"}, sig_addr)
    print("✓ DM: peer-message")

    dm("broadcast-message", {"data": f"broadcast via DM from {state.get('client_address')} @ {ts}"}, sig_addr)
    print("✓ DM: broadcast-message")

    dm("frame-color", {"uuid": uid, "data": {"kind": "ping", "color": [255,0,0], "ts": ts}}, sig_addr)
    print("✓ DM: frame-color")

    dm("frame-depth", {"uuid": uid, "data": {"kind": "ping", "depth": 1.0, "ts": ts}}, sig_addr)
    print("✓ DM: frame-depth")

def _shutdown(*_):
    try:
        if bridge and bridge.poll() is None:
            bridge.terminate()
    except Exception:
        pass
    sys.exit(0)

signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

# ──────────────────────────────────────────────────────────────────────
# VIII. Main
# ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("→ Launching NKN client bridge …")

    # Wait briefly for NKN MultiClient to be fully ready
    for _ in range(200):
        if state["client_address"]:
            break
        time.sleep(0.05)

    if not state["client_address"]:
        print("Bridge did not become ready. Exiting.")
        _shutdown()

    # Always DM to the server's sig (NAT-friendly)
    if not _looks_like_nkn_addr(dotenv["SERVER_NKN_ADDRESS"]):
        print(f"‼ Invalid server NKN address: {dotenv['SERVER_NKN_ADDRESS']}")
        _shutdown()

    # Warm up route, then send the standard events
    run_handshake(dotenv["SERVER_NKN_ADDRESS"])
    time.sleep(0.2)  # tiny delay to let path establish
    send_all(dotenv["SERVER_NKN_ADDRESS"])

    print("→ Client is running (DM-only). Press Ctrl+C to exit.")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        _shutdown()
