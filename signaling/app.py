#!/usr/bin/env python3
"""
signaling_server.py — NKN-only signaling + JWT handshake over DMs

What this provides:
• A persistent decentralized endpoint (NKN MultiClient) with address derived from NKN_SEED_HEX.
• Auth handshake via NKN Direct Messages:
    Agent → DM: {"event":"login","uuid":"<uuid>","password":"<shared>"}
    Server → DM: {"event":"login-ok","uuid":"<uuid>","token":"<jwt>"}  or  {"event":"login-error","error":"..."}
• Logs EVERY inbound DM (event, src, uuid) so you can see attempts in the console.
• Minimal HTTP metadata endpoint (local only):
    GET /  -> { ok: true, nknAddress, topicPrefix }

Notes:
• No Socket.IO, no LocalTunnel. Fully NKN-native.
• We don't subscribe to on-chain topics by default (wallet funding would be required). We live on DMs.
"""

# ──────────────────────────────────────────────────────────────────────
# I. bootstrap venv & deps (std-lib only)
# ──────────────────────────────────────────────────────────────────────
import os, sys, subprocess
from pathlib import Path
import importlib.util as _ilu

BASE_DIR = Path(__file__).resolve().parent
VENV_DIR = BASE_DIR / "venv"
BIN_DIR  = VENV_DIR / ("Scripts" if os.name == "nt" else "bin")
PY_VENV  = BIN_DIR / "python"
REQS     = ["flask", "flask-cors", "eventlet", "PyJWT"]

def _in_venv() -> bool:
    try:
        return Path(sys.executable).resolve() == PY_VENV.resolve()
    except Exception:
        return False

def _module_missing(mod: str) -> bool:
    return _ilu.find_spec(mod) is None

def _create_venv():
    if VENV_DIR.exists(): return
    import venv; venv.EnvBuilder(with_pip=True).create(VENV_DIR)
    subprocess.check_call([str(PY_VENV), "-m", "pip", "install", "--upgrade", "pip"])

def _ensure_deps():
    missing = []
    checks = [("flask", "flask"),
              ("flask-cors", "flask_cors"),
              ("eventlet", "eventlet"),
              ("PyJWT", "jwt")]
    for pkg, mod in checks:
        if _module_missing(mod): missing.append(pkg)
    if missing:
        subprocess.check_call([str(PY_VENV), "-m", "pip", "install", *missing])
        os.execv(str(PY_VENV), [str(PY_VENV), *sys.argv])

if not _in_venv():
    _create_venv()
    os.execv(str(PY_VENV), [str(PY_VENV), *sys.argv])

_ensure_deps()

# ──────────────────────────────────────────────────────────────────────
# II. heavy imports & eventlet patch
# ──────────────────────────────────────────────────────────────────────
import eventlet; eventlet.monkey_patch()

import json, secrets, signal, threading, base64, time, re, shutil
from datetime import datetime, timedelta
from subprocess import Popen, PIPE
from typing import Dict, Any

import jwt
from flask import Flask, jsonify
from flask_cors import CORS

# ──────────────────────────────────────────────────────────────────────
# III. .env and config
# ──────────────────────────────────────────────────────────────────────
ENV_PATH = BASE_DIR / ".env"
if not ENV_PATH.exists():
    pw        = input("Shared password for agents (blank = random):\n> ").strip() or secrets.token_urlsafe(16)
    jwt_sec   = secrets.token_urlsafe(32)
    seed_hex  = secrets.token_hex(32)  # 64 hex chars
    topic_ns  = "roko-signaling"
    ENV_PATH.write_text(
        f"PEER_SHARED_SECRET={pw}\n"
        f"JWT_SECRET={jwt_sec}\n"
        f"NKN_SEED_HEX={seed_hex}\n"
        f"NKN_TOPIC_PREFIX={topic_ns}\n"
    )
    print("→ wrote .env")

dotenv: Dict[str,str] = {}
for line in ENV_PATH.read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        k,v = line.split("=",1); dotenv[k.strip()] = v.strip()

PEER_PW      = dotenv["PEER_SHARED_SECRET"]
JWT_SECRET   = dotenv["JWT_SECRET"]
NKN_SEED_HEX = dotenv["NKN_SEED_HEX"].lower().replace("0x","")
TOPIC_PREFIX = dotenv.get("NKN_TOPIC_PREFIX","roko-signaling")
JWT_EXP      = timedelta(minutes=30)

# ──────────────────────────────────────────────────────────────────────
# IV. Node NKN bridge (DM-first; tolerant of SDK variations)
# ──────────────────────────────────────────────────────────────────────
NODE_DIR = BASE_DIR / "bridge-node"
NODE_BIN = shutil.which("node")
NPM_BIN  = shutil.which("npm")
if not NODE_BIN or not NPM_BIN:
    sys.exit("‼️  Node.js and npm are required for the NKN bridge. Install Node 18+.")

BRIDGE_JS = NODE_DIR / "nkn_bridge.js"
PKG_JSON  = NODE_DIR / "package.json"

if not NODE_DIR.exists():
    NODE_DIR.mkdir(parents=True)
if not PKG_JSON.exists():
    print("→ Initializing bridge-node …")
    subprocess.check_call([NPM_BIN, "init", "-y"], cwd=NODE_DIR)
    subprocess.check_call([NPM_BIN, "install", "nkn-sdk@^1.3.1"], cwd=NODE_DIR)

# Handles both message signatures: (src, payload) and (msgObj)
BRIDGE_SRC = r"""
/* nkn_bridge.js — DM-only bridge; no topic subscriptions required */
const nkn = require('nkn-sdk');
const readline = require('readline');

const SEED_HEX = (process.env.NKN_SEED_HEX || '').toLowerCase().replace(/^0x/,'');
const TOPIC_NS = process.env.NKN_TOPIC_PREFIX || 'roko-signaling';

function sendToPy(obj){ process.stdout.write(JSON.stringify(obj) + '\n'); }
function log(...args){ console.error('[bridge]', ...args); }

(async () => {
  if (!/^[0-9a-f]{64}$/.test(SEED_HEX)) throw new RangeError('invalid hex seed');
  const client = new nkn.MultiClient({ seed: SEED_HEX, identifier: 'sig' });

  client.on('connect', () => {
    sendToPy({ type: 'ready', address: client.addr, topicPrefix: TOPIC_NS });
    log('ready at', client.addr);
  });

  const onMessage = (a, b) => {
    let src, payload;
    if (a && typeof a === 'object' && a.payload !== undefined) {
      // signature: (msgObj)
      src = a.src;
      payload = a.payload;
    } else {
      // signature: (src, payload)
      src = a;
      payload = b;
    }

    try {
      const asStr = Buffer.isBuffer(payload) ? payload.toString('utf8') : String(payload || '');
      // Only pass JSON up; ignore non-JSON quietly.
      try {
        const msg = JSON.parse(asStr);
        sendToPy({ type: 'nkn-dm', src, msg });
      } catch {
        // not JSON -> ignore
      }
    } catch (e) {
      // ignore
    }
  };

  client.on('message', onMessage);

  const rl = readline.createInterface({ input: process.stdin });
  rl.on('line', async line => {
    if (!line) return;
    let cmd; try { cmd = JSON.parse(line); } catch { return; }
    try {
      if (cmd.type === 'dm' && cmd.to && cmd.data) {
        await client.send(cmd.to, JSON.stringify(cmd.data));
      } else if (cmd.type === 'pub' && cmd.topic && cmd.data) {
        await client.publish(TOPIC_NS + '.' + cmd.topic, JSON.stringify(cmd.data));
      }
    } catch (e) {
      log('send error', e);
    }
  });
})();
"""
if not BRIDGE_JS.exists() or BRIDGE_JS.read_text() != BRIDGE_SRC:
    BRIDGE_JS.write_text(BRIDGE_SRC)

bridge_env = os.environ.copy()
bridge_env["NKN_SEED_HEX"]     = NKN_SEED_HEX
bridge_env["NKN_TOPIC_PREFIX"] = TOPIC_PREFIX

bridge = Popen([str(NODE_BIN), str(BRIDGE_JS)], cwd=NODE_DIR, env=bridge_env,
               stdin=PIPE, stdout=PIPE, stderr=PIPE, text=True, bufsize=1)

state: Dict[str, Any] = {"nkn_address": None, "topic_prefix": TOPIC_PREFIX}
agents_by_src: Dict[str, Dict[str, Any]] = {}    # src -> {uuid, lastSeen}
agents_by_uuid: Dict[str, Dict[str, Any]] = {}   # uuid -> {src, lastSeen}
viewers_by_uuid: Dict[str, set] = {}  # uuid -> set of viewer src addrs

def _dm(to: str, data: dict):
    _bridge_send({"type": "dm", "to": to, "data": data})


def _bridge_reader_stdout():
    for line in bridge.stdout:
        line = (line or "").strip()
        if not line: continue
        try:
            msg = json.loads(line)
        except Exception:
            continue

        if msg.get("type") == "ready":
            state["nkn_address"]  = msg.get("address")
            state["topic_prefix"] = msg.get("topicPrefix") or TOPIC_PREFIX
            print(f"→ NKN ready: {state['nkn_address']}  (topics prefix: {state['topic_prefix']})")

        elif msg.get("type") == "nkn-dm":
            src = msg.get("src") or ""
            body = msg.get("msg") or {}
            _handle_dm(src, body)

def _bridge_reader_stderr():
    for line in bridge.stderr:
        sys.stderr.write(line)

def _bridge_send(obj: dict):
    try:
        bridge.stdin.write(json.dumps(obj) + "\n")
        bridge.stdin.flush()
    except Exception:
        pass

threading.Thread(target=_bridge_reader_stdout, daemon=True).start()
threading.Thread(target=_bridge_reader_stderr, daemon=True).start()

def _shutdown(*_):
    try: bridge.terminate()
    except Exception: pass
    os._exit(0)

signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

# ──────────────────────────────────────────────────────────────────────
# V. DM handler (JWT over NKN) + logging
# ──────────────────────────────────────────────────────────────────────
def _mint_jwt(uuid: str) -> str:
    now = datetime.utcnow()
    claims = {
        "sub": uuid,
        "roles": ["peer"],
        "iat": int(now.timestamp()),
        "exp": int((now + JWT_EXP).timestamp()),
    }
    return jwt.encode(claims, JWT_SECRET, algorithm="HS256")

_uuid_rx = re.compile(r"^[0-9a-f]{4}(?:-[0-9a-f]{4}){3}$")

def _stamp() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"

def _log(evt: str, msg: str):
    print(f"[{_stamp()}] {evt}  {msg}")

def _handle_dm(src_addr: str, body: dict):
    ev = (body.get("event") or "").strip()
    if not ev:
        _log("dm", f"from {src_addr}  event=<missing>  body={body}")
        return

    # ── LOGIN ─────────────────────────────────────────────────────────
    if ev == "login":
        uuid = (body.get("uuid") or "").strip()
        pw   = (body.get("password") or "").strip()
        _log("login", f"from {src_addr}  uuid={uuid or '—'}")
        if not uuid or not pw:
            _dm(src_addr, {"event":"login-error","error":"missing fields"})
            _log("login-error", f"{src_addr} → missing fields")
            return
        if pw != PEER_PW:
            _dm(src_addr, {"event":"login-error","error":"bad credentials"})
            _log("login-error", f"{src_addr} (uuid={uuid}) bad credentials")
            return
        tok = _mint_jwt(uuid)
        agents_by_src[src_addr] = {"uuid": uuid, "lastSeen": _stamp()}
        agents_by_uuid[uuid]    = {"src": src_addr, "lastSeen": _stamp()}
        _dm(src_addr, {"event":"login-ok","uuid":uuid,"token":tok})
        _log("login-ok", f"{src_addr} (uuid={uuid})")
        return

    # ── VIEWER REGISTRATION ──────────────────────────────────────────
    if ev == "viewer-hello":
        uuid = (body.get("uuid") or "").strip()
        if uuid:
            viewers_by_uuid.setdefault(uuid, set()).add(src_addr)
            _log("viewer-hello", f"{src_addr}  uuid={uuid}")
            _dm(src_addr, {"event":"viewer-ack","uuid":uuid})
        else:
            _dm(src_addr, {"event":"viewer-error","error":"missing uuid"})
        return

    if ev == "viewer-bye":
        uuid = (body.get("uuid") or "").strip()
        if uuid and uuid in viewers_by_uuid and src_addr in viewers_by_uuid[uuid]:
            viewers_by_uuid[uuid].discard(src_addr)
            if not viewers_by_uuid[uuid]:
                viewers_by_uuid.pop(uuid, None)
            _log("viewer-bye", f"{src_addr}  uuid={uuid}")
        return

    # ── CONTROL RELAY (browser → server → agent) ─────────────────────
    if ev == "control":
        uuid = (body.get("uuid") or "").strip()
        cmd  = (body.get("command") or "").strip()
        _log("control", f"{src_addr} → uuid={uuid or '—'}  cmd={cmd!r}")
        agent = agents_by_uuid.get(uuid)
        if not agent or not agent.get("src"):
            _dm(src_addr, {"event":"control-error","error":"unknown uuid"})
            return
        agent_src = agent["src"]
        _dm(agent_src, {"event":"peer-message","data": cmd})
        _dm(src_addr, {"event":"control-ack","uuid":uuid})
        return

    # ── ANNOUNCE (benign) ────────────────────────────────────────────
    if ev == "announce":
        uuid = (body.get("uuid") or "").strip()
        if uuid:
            agents_by_src[src_addr] = {"uuid": uuid, "lastSeen": _stamp()}
            agents_by_uuid[uuid]    = {"src": src_addr, "lastSeen": _stamp()}
        _log("announce", f"from {src_addr}  uuid={uuid or '—'}")
        return

    # ── FRAMES (agent → server → viewers) ────────────────────────────
    if ev in ("frame-color", "frame-depth"):
        uuid = (body.get("uuid") or "").strip()
        data = body.get("data")
        size = 0
        if isinstance(data, str):
            try: size = len(base64.b64decode(data.encode("ascii"), validate=True))
            except Exception: size = len(data)
        _log(ev, f"from {src_addr}  uuid={uuid or '—'}  bytes≈{size}")

        if uuid and uuid in viewers_by_uuid:
            for v in list(viewers_by_uuid[uuid]):
                _dm(v, body)  # forward same JSON
        return

    # ── Peer/broadcast messages (optional) ───────────────────────────
    if ev in ("peer-message","broadcast-message"):
        txt = body.get("data")
        _log(ev, f"from {src_addr}  text={repr(txt)[:120]}")
        return

    _log("dm-unknown", f"from {src_addr}  event={ev}  body={body}")


# ──────────────────────────────────────────────────────────────────────
# VI. Minimal HTTP (metadata only)
# ──────────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

@app.route("/")
def root():
    return jsonify(ok=True, nknAddress=state["nkn_address"], topicPrefix=state["topic_prefix"])


# ──────────────────────────────────────────────────────────────────────
# VII. Run
# ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("→ NKN-only signaling. HTTP is metadata only (no Socket.IO).")
    # Keep a tiny HTTP listener alive for discovery
    from eventlet import wsgi
    listener = eventlet.listen(("0.0.0.0", 3000))
    wsgi.server(listener, app)
