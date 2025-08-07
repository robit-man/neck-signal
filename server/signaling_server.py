#!/usr/bin/env python3
"""
signaling_server.py
───────────────────
Self-bootstrapping Flask-SocketIO + LocalTunnel signaling server with JWT auth.

• Creates a `venv/` beside this file (once) and relaunches itself from there.
• Prompts *only the first time* for
      – allowed CORS origins      (comma-sep, blank = “*”)
      – desired LocalTunnel subdomain prefix
      – shared password that peers (agent / UI) will send to /login
• Saves all answers in config.json for later runs.
• Generates a .env holding JWT_SECRET & PEER_SHARED_SECRET on first run.
• Restarts itself if the public tunnel is lost for 5 consecutive minutes.
"""

# ──────────────────────────────────────────────────────────────────────
# I.  tiny bootstrap: create + re-exec into venv if necessary
# ──────────────────────────────────────────────────────────────────────
import os, sys, subprocess, secrets, re, select, fcntl, json, random, shutil
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urlparse
import time, threading, urllib.request, argparse, textwrap

BASE_DIR   = Path(__file__).resolve().parent
VENV_DIR   = BASE_DIR / "venv"
PY_IN_VENV = VENV_DIR / ("Scripts" if os.name == "nt" else "bin") / "python"
REQS       = ["flask", "flask-cors", "flask-socketio", "eventlet", "PyJWT"]

def ensure_venv():
    if Path(sys.executable).resolve() == PY_IN_VENV.resolve():
        return                               # already inside
    if not VENV_DIR.exists():
        print("→ creating virtual-env …")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
        print("→ installing dependencies (one-time) …")
        subprocess.check_call([str(PY_IN_VENV), "-m", "pip", "install",
                               "--upgrade", "pip", *REQS])
    os.execv(str(PY_IN_VENV), [str(PY_IN_VENV), *sys.argv])

ensure_venv()

# ──────────────────────────────────────────────────────────────────────
# II.  *now* we can import heavy stuff & monkey-patch
# ──────────────────────────────────────────────────────────────────────
import eventlet; eventlet.monkey_patch()

import jwt
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, emit

# ──────────────────────────────────────────────────────────────────────
# III.  first-run .env creation + loading
# ──────────────────────────────────────────────────────────────────────
ENV_PATH = BASE_DIR / ".env"

if not ENV_PATH.exists():
    print("First start – generating .env …")
    shared_pw  = input("Shared password for peers (blank = random):\n> ").strip() \
                 or secrets.token_urlsafe(16)
    jwt_secret = secrets.token_urlsafe(32)
    ENV_PATH.write_text(f"PEER_SHARED_SECRET={shared_pw}\nJWT_SECRET={jwt_secret}\n")
    print("→ wrote .env")

for line in ENV_PATH.read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

# ──────────────────────────────────────────────────────────────────────
# IV.  config.json load / first-run questions
# ──────────────────────────────────────────────────────────────────────
CFG_PATH = BASE_DIR / "config.json"
cfg: dict = {}
if CFG_PATH.exists():
    cfg = json.loads(CFG_PATH.read_text())
    print("→ loaded config:", cfg)

def ask_once(key: str, prompt: str, transform=lambda x: x):
    if key in cfg and cfg[key]:
        return cfg[key]
    val = transform(input(prompt).strip())
    cfg[key] = val
    return val

cors_raw = ask_once(
    "cors_origins",
    "Enter CORS origins (comma separated, blank = *):\n> ",
    lambda s: [urlparse(u if u.startswith(("http://", "https://"))
                        else f"http://{u}").netloc
               for u in s.split(",") if u.strip()] or ["*"]
)
subdomain = ask_once("subdomain", "Desired LocalTunnel sub-domain:\n> ")

# CLI overrides
clp = argparse.ArgumentParser(description="JWT-secured signaling server")
clp.add_argument("-p", "--port", type=int, help="listen port")
clp.add_argument("-s", "--subdomain", help="tunnel sub-domain prefix")
args = clp.parse_args()

PORT      = args.port      or cfg.get("port", 3000)
SUBDOMAIN = args.subdomain or subdomain

# ──────────────────────────────────────────────────────────────────────
# V.  launch LocalTunnel (retry w/ back-off)
# ──────────────────────────────────────────────────────────────────────
LT = shutil.which("lt") or shutil.which("npx")
if not LT:
    print("‼️  install localtunnel:  npm i -g localtunnel"); sys.exit(1)
LAUNCHER = [LT] if LT.endswith("lt") else [LT, "--yes", "localtunnel"]

url_re = re.compile(r"https?://\S+")
def start_tunnel(port:int, sub:str):
    cmd = LAUNCHER + ["--port", str(port), "--subdomain", sub]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    fcntl.fcntl(proc.stderr, fcntl.F_SETFL,
                fcntl.fcntl(proc.stderr, fcntl.F_GETFL)|os.O_NONBLOCK)
    while True:
        if proc.poll() is not None:
            return None, (proc.stderr.read() or "").strip()
        ready,_,_ = select.select([proc.stdout],[],[],0.2)
        if ready:
            line = proc.stdout.readline()
            m = url_re.search(line)
            if m: return proc, m.group(0)

delay = 5
while True:
    print(f"→ opening tunnel ‘{SUBDOMAIN}’ …")
    lt_proc, PUBLIC_URL = start_tunnel(PORT, SUBDOMAIN)
    if lt_proc: break
    print(f"‼️  tunnel error – retrying in {delay}s"); time.sleep(delay)
    delay = min(delay*2, 300)

print("→ public URL:", PUBLIC_URL)

# persist config
cfg.update(port=PORT, subdomain=SUBDOMAIN,
           cors_origins=cors_raw,
           localtunnel_domain=urlparse(PUBLIC_URL).netloc)
CFG_PATH.write_text(json.dumps(cfg, indent=4))

ALLOWED = "*" if cors_raw == ["*"] else \
          [f"https://{d}" for d in cors_raw] + [f"https://{cfg['localtunnel_domain']}"]

print("→ allowed CORS:", ALLOWED)

# ──────────────────────────────────────────────────────────────────────
# VI.  Flask + Socket.IO with JWT guard
# ──────────────────────────────────────────────────────────────────────
JWT_SECRET  = os.environ["JWT_SECRET"]
JWT_EXP     = timedelta(minutes=15)
PEER_PW     = os.environ["PEER_SHARED_SECRET"]

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": ALLOWED}})
socketio = SocketIO(app, cors_allowed_origins=ALLOWED, async_mode="eventlet")

clients: dict[str, dict] = {}   # sid -> {uuid, roles}
def roles_ok(sid, role): return role in clients.get(sid, {}).get("roles", [])

@app.route("/")
def root(): return jsonify(ok=True, public=PUBLIC_URL)

@app.route("/login", methods=["POST"])
def login():
    body = request.get_json(force=True) or {}
    if body.get("password") != PEER_PW:
        return jsonify(error="bad credentials"), 401
    uuid = body.get("uuid") or "-".join(secrets.token_hex(2) for _ in range(4))
    token = jwt.encode(
        {"sub": uuid, "roles": ["peer"], "exp": datetime.utcnow() + JWT_EXP},
        JWT_SECRET, algorithm="HS256"
    )
    return jsonify(token=token, uuid=uuid)

@socketio.on("connect")
def sio_connect(auth):
    token = isinstance(auth, dict) and auth.get("token")
    if not token: return False
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception as e:
        print("JWT err:", e); return False
    sid = request.sid
    clients[sid] = {"uuid": claims["sub"], "roles": claims.get("roles", [])}
    print(f"[+] {sid} ({clients[sid]['uuid']})")
    emit("new-peer", {"id": sid, **clients[sid]}, broadcast=True, include_self=False)
    emit("existing-peers",
         [{"id": pid, **d} for pid, d in clients.items() if pid != sid])

@socketio.on("disconnect")
def sio_disconnect():
    sid = request.sid
    print(f"[-] {sid}")
    clients.pop(sid, None)
    emit("peer-disconnect", sid, broadcast=True)

@socketio.on("peer-message")
def sio_peer_msg(data):
    sid = request.sid
    if not roles_ok(sid, "peer"): return emit("error", {"msg":"unauthorized"})
    tgt = data.get("target")
    if tgt not in clients:
        return emit("error", {"msg":"unknown target"})
    socketio.emit("peer-message", {"peerId": sid, "message": data.get("message")}, room=tgt)

@socketio.on("broadcast-message")
def sio_broadcast(data):
    sid = request.sid
    if not roles_ok(sid, "peer"): return
    socketio.emit("peer-message", {"peerId": sid, "message": data.get("message")},
                  broadcast=True, include_self=False)

# ──────────────────────────────────────────────────────────────────────
# VII.  heartbeat (restart if tunnel lost)
# ──────────────────────────────────────────────────────────────────────
def heartbeat():
    fails = 0
    while True:
        try:
            with urllib.request.urlopen(PUBLIC_URL, timeout=5) as r:
                if (getattr(r, "status", None) or r.getcode()) == 200:
                    print("✅ tunnel OK"); fails = 0; time.sleep(60); continue
        except Exception as e:
            fails += 1
            print(f"⚠️  heartbeat {fails}/5:", e)
        if fails >= 5:
            print("‼️ tunnel dead – restarting")
            os.execv(sys.executable, [sys.executable, *sys.argv])
        time.sleep(60)

# ──────────────────────────────────────────────────────────────────────
# VIII.  run
# ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=heartbeat, daemon=True).start()
    socketio.run(app, host="0.0.0.0", port=PORT)
