#!/usr/bin/env python3
"""
signaling_server.py  –  JWT-secured Flask-SocketIO + LocalTunnel broker
Adds per-peer dynamic frame endpoints:
  • Agent emits to:   "frame-color" / "frame-depth"  (unchanged)
  • Server re-emits:  "/<uuid>_color" and "/<uuid>_depth" (binary)
Clients that want a specific robot’s stream should listen to those dynamic events.
"""
# ──────────────────────────────────────────────────────────────────────
# I.  bootstrap into a v-env (std-lib only)
# ──────────────────────────────────────────────────────────────────────
import os, sys, subprocess, importlib
from pathlib import Path

BASE_DIR   = Path(__file__).resolve().parent
VENV_DIR   = BASE_DIR / "venv"
BIN_DIR    = VENV_DIR / ("Scripts" if os.name == "nt" else "bin")
PY_VENV    = BIN_DIR / "python"
REQS       = ["flask", "flask-cors", "flask-socketio", "eventlet", "PyJWT"]

def running_inside_venv() -> bool:
    return Path(sys.executable).resolve() == PY_VENV.resolve()

def create_venv_once():
    if VENV_DIR.exists():
        return
    print("→ Creating virtual-env …")
    import venv; venv.EnvBuilder(with_pip=True).create(VENV_DIR)
    print("→ Upgrading pip …")
    subprocess.check_call([str(PY_VENV), "-m", "pip", "install", "--upgrade", "pip"])
    print("→ Installing first-time dependencies …")
    subprocess.check_call([str(PY_VENV), "-m", "pip", "install", *REQS])

def ensure_deps_inside_venv():
    missing = []
    for pkg, modulename in [("flask", "flask"),
                            ("flask-cors", "flask_cors"),
                            ("flask-socketio", "flask_socketio"),
                            ("eventlet", "eventlet"),
                            ("PyJWT", "jwt")]:
        try:
            importlib.import_module(modulename)
        except ModuleNotFoundError:
            missing.append(pkg)
    if missing:
        print("→ Installing missing deps inside venv:", ", ".join(missing))
        subprocess.check_call([str(PY_VENV), "-m", "pip", "install", *missing])
        os.execv(str(PY_VENV), [str(PY_VENV), *sys.argv])

if not running_inside_venv():
    create_venv_once()
    os.execv(str(PY_VENV), [str(PY_VENV), *sys.argv])

ensure_deps_inside_venv()

# ──────────────────────────────────────────────────────────────────────
# II. heavy imports (after v-env) & monkey-patch
# ──────────────────────────────────────────────────────────────────────
import eventlet; eventlet.monkey_patch()

import json, re, select, fcntl, shutil, time, threading, secrets, argparse
from datetime import datetime, timedelta
from urllib.parse import urlparse
import urllib.request, subprocess

import jwt
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO

# ──────────────────────────────────────────────────────────────────────
# III. .env generation & load
# ──────────────────────────────────────────────────────────────────────
ENV_PATH = BASE_DIR / ".env"
if not ENV_PATH.exists():
    pw  = input("Shared password for peers (blank = random):\n> ").strip() or secrets.token_urlsafe(16)
    ENV_PATH.write_text(f"PEER_SHARED_SECRET={pw}\nJWT_SECRET={secrets.token_urlsafe(32)}\n")
    print("→ wrote .env")
for line in ENV_PATH.read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

PEER_PW     = os.environ["PEER_SHARED_SECRET"]
JWT_SECRET  = os.environ["JWT_SECRET"]
JWT_EXP     = timedelta(minutes=15)

# ──────────────────────────────────────────────────────────────────────
# IV. config.json  (first-run prompts)
# ──────────────────────────────────────────────────────────────────────
CFG_PATH = BASE_DIR / "config.json"
cfg: dict = json.loads(CFG_PATH.read_text()) if CFG_PATH.exists() else {}

def ask_once(key: str, prompt: str, transform=lambda x: x):
    if key in cfg and cfg[key]:
        return cfg[key]
    val = transform(input(prompt).strip())
    cfg[key] = val
    return val

cors_raw = ask_once(
    "cors_origins",
    "Enter CORS origins (comma-sep, blank=*):\n> ",
    lambda s: [urlparse(u if u.startswith(("http", "https")) else f"http://{u}").netloc
               for u in s.split(",") if u.strip()] or ["*"]
)
subdomain = ask_once("subdomain", "Desired LocalTunnel sub-domain prefix:\n> ")

cli = argparse.ArgumentParser()
cli.add_argument("-p", "--port", type=int, help="listen port")
cli.add_argument("-s", "--subdomain", help="LocalTunnel sub-domain prefix")
args = cli.parse_args()
PORT      = args.port      or cfg.get("port", 3000)
SUBDOMAIN = args.subdomain or subdomain

# ──────────────────────────────────────────────────────────────────────
# V. LocalTunnel (robust start with back-off)
# ──────────────────────────────────────────────────────────────────────
LT_BIN = shutil.which("lt") or shutil.which("npx")
if not LT_BIN:
    sys.exit("‼️  install `npm i -g localtunnel` or have npx available")

LAUNCHER = [LT_BIN] if LT_BIN.endswith("lt") else [LT_BIN, "--yes", "localtunnel"]
URL_RE   = re.compile(r"https?://\S+")

def open_tunnel(port: int, sub: str):
    proc = subprocess.Popen(LAUNCHER + ["--port", str(port), "--subdomain", sub],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    fcntl.fcntl(proc.stderr, fcntl.F_SETFL,
                fcntl.fcntl(proc.stderr, fcntl.F_GETFL) | os.O_NONBLOCK)
    while True:
        if proc.poll() is not None:
            raise RuntimeError(proc.stderr.read() or f"lt exited {proc.returncode}")
        ready, *_ = select.select([proc.stdout], [], [], 0.2)
        if ready:
            m = URL_RE.search(proc.stdout.readline())
            if m: return proc, m.group(0).rstrip()

delay = 5
while True:
    try:
        lt_proc, PUBLIC_URL = open_tunnel(PORT, SUBDOMAIN)
        break
    except Exception as e:
        print("Tunnel error:", e, "— retry in", delay, "s")
        time.sleep(delay)
        delay = min(delay * 2, 300)

print("→ Public URL:", PUBLIC_URL)

cfg.update({"port": PORT, "subdomain": SUBDOMAIN,
            "cors_origins": cors_raw,
            "localtunnel_domain": urlparse(PUBLIC_URL).netloc})
CFG_PATH.write_text(json.dumps(cfg, indent=4))

ALLOWED = "*" if cors_raw == ["*"] else \
          [f"https://{d}" for d in cors_raw] + [f"https://{cfg['localtunnel_domain']}"]
print("→ Allowed CORS origins:", ALLOWED)

# ──────────────────────────────────────────────────────────────────────
# VI.  Flask + Socket.IO (JWT guard)  ──────────
# ──────────────────────────────────────────────────────────────────────
app      = Flask(__name__)
CORS(app, resources={r"/*": {"origins": ALLOWED}})
socketio = SocketIO(app, cors_allowed_origins=ALLOWED, async_mode="eventlet")

# sid → {uuid:str, roles:[str]}
clients: dict[str, dict] = {}
def roles_ok(sid: str, role: str) -> bool:
    return role in clients.get(sid, {}).get("roles", [])

@app.route("/")
def root(): return jsonify(ok=True, public=PUBLIC_URL)

@app.route("/login", methods=["POST"])
def login():
    body = request.get_json(force=True) or {}
    if body.get("password") != PEER_PW:
        return jsonify(error="bad credentials"), 401
    uuid = body.get("uuid") or "-".join(secrets.token_hex(2) for _ in range(4))
    token = jwt.encode({"sub": uuid, "roles": ["peer"],
                        "exp": datetime.utcnow() + JWT_EXP},
                       JWT_SECRET, algorithm="HS256")
    return jsonify(token=token, uuid=uuid)

# ——— WebSocket events ————————————————————————————
@socketio.on("connect")
def _connect(auth):
    token = isinstance(auth, dict) and auth.get("token")

    # WS-only password path (no POST) for browsers/phones
    if not token and isinstance(auth, dict) and auth.get("password"):
        if auth["password"] == PEER_PW:
            sid = request.sid
            uid = auth.get("uuid") or "-".join(secrets.token_hex(2) for _ in range(4))
            clients[sid] = {"uuid": uid, "roles": ["peer"]}
            print(f"[+] {sid} ({uid}) connected via ws+password")
            socketio.emit("new-peer", {"id": sid, **clients[sid]}, skip_sid=sid)
            socketio.emit("existing-peers",
                          [{"id": pid, **info} for pid, info in clients.items() if pid != sid],
                          to=sid)
            return
        else:
            print("WS password invalid"); return False

    # JWT path
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception as e:
        print("JWT decode error:", e); return False

    sid = request.sid
    clients[sid] = {"uuid": claims["sub"], "roles": claims.get("roles", [])}
    print(f"[+] {sid} ({claims['sub']}) connected")
    socketio.emit("new-peer", {"id": sid, **clients[sid]}, skip_sid=sid)
    socketio.emit("existing-peers",
                  [{"id": pid, **info} for pid, info in clients.items() if pid != sid],
                  to=sid)

@socketio.on("disconnect")
def _disconnect():
    sid = request.sid
    clients.pop(sid, None)
    socketio.emit("peer-disconnect", sid)
    print(f"[-] {sid} disconnected")

@socketio.on("peer-message")
def _peer_msg(data):
    sid = request.sid
    if not roles_ok(sid, "peer"): return
    target = data.get("target")
    if target in clients:
        socketio.emit("peer-message", {"peerId": sid, "message": data.get("message")}, to=target)

@socketio.on("broadcast-message")
def _broadcast(data):
    sid = request.sid
    if not roles_ok(sid, "peer"): return
    socketio.emit("peer-message", {"peerId": sid, "message": data.get("message")}, skip_sid=sid)

# ──────────────────────────────────────────────────────────────────────
# NEW: Per-peer dynamic frame endpoints
#   Agent → emits to "frame-color"/"frame-depth" (binary bytes)
#   Server → re-emits to *both* legacy and dynamic:
#            "frame-color"/"frame-depth" (legacy)
#            "/<uuid>_color" and "/<uuid>_depth" (dynamic, binary)
# Clients interested in a specific robot listen to those dynamic names.
# ──────────────────────────────────────────────────────────────────────
def _uuid_for_sid(sid: str) -> str | None:
    info = clients.get(sid)
    return info and info.get("uuid")

@socketio.on("frame-color")
def _relay_color(data):
    sid = request.sid
    uid = _uuid_for_sid(sid)
    if not uid: return
    # Legacy broadcast (all except sender)
    socketio.emit("frame-color", data, skip_sid=sid)
    # Dynamic, per-robot channel
    socketio.emit(f"/{uid}_color", data, skip_sid=sid)

@socketio.on("frame-depth")
def _relay_depth(data):
    sid = request.sid
    uid = _uuid_for_sid(sid)
    if not uid: return
    socketio.emit("frame-depth", data, skip_sid=sid)
    socketio.emit(f"/{uid}_depth", data, skip_sid=sid)

# ──────────────────────────────────────────────────────────────────────
# VII. heartbeat – restart if tunnel lost
# ──────────────────────────────────────────────────────────────────────
def heartbeat():
    misses = 0
    while True:
        try:
            with urllib.request.urlopen(PUBLIC_URL, timeout=5) as r:
                code = getattr(r, "status", None) or r.getcode()
                if code == 200:
                    misses = 0
                    time.sleep(60)
                    continue
        except Exception as e:
            misses += 1
            print(f"Heartbeat miss {misses}/5:", e)
        if misses >= 5:
            print("‼️ tunnel lost – restarting …")
            os.execv(sys.executable, [sys.executable, *sys.argv])
        time.sleep(60)

# ──────────────────────────────────────────────────────────────────────
# VIII. run
# ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=heartbeat, daemon=True).start()
    socketio.run(app, host="0.0.0.0", port=PORT)
