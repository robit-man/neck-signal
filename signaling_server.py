#!/usr/bin/env python3
"""
app.py — self-bootstrapping venv + Flask-SocketIO signaling server + LocalTunnel + JWT auth.
"""

import os, sys

# ── I) ENSURE VENV ─────────────────────────────────────────────────────────────
script_dir = os.path.dirname(os.path.abspath(__file__))
venv_dir   = os.path.join(script_dir, "venv")
venv_py    = os.path.join(venv_dir, "bin", "python")

def in_venv():
    return os.path.abspath(sys.executable) == os.path.abspath(venv_py)

DEPS = ["flask", "flask-cors", "flask-socketio", "eventlet", "PyJWT"]

if not in_venv():
    if not os.path.isdir(venv_dir):
        print("→ Creating virtualenv…")
        subprocess.check_call([sys.executable, "-m", "venv", venv_dir])
    print("→ Installing dependencies…")
    subprocess.check_call([venv_py, "-m", "pip", "install", "--upgrade", "pip"])
    subprocess.check_call([venv_py, "-m", "pip", "install"] + DEPS)
    os.execv(venv_py, [venv_py] + sys.argv)

# ── II) MONKEY-PATCH & IMPORTS ─────────────────────────────────────────────────
import eventlet
eventlet.monkey_patch()

import json, random, argparse, re, select, fcntl, threading, time, subprocess, shutil, urllib.request
from urllib.parse import urlparse
from datetime import datetime, timedelta

import jwt
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, emit

# ── III) JWT CONFIG ────────────────────────────────────────────────────────────
SECRET_KEY = os.environ.get("JWT_SECRET", "replace_with_strong_random_secret")
JWT_EXP     = timedelta(minutes=15)

# ── IV) LOAD/INIT CONFIG ───────────────────────────────────────────────────────
cfg_path = os.path.join(script_dir, "config.json")
try:
    cfg = json.load(open(cfg_path))
    print("→ Loaded config:", cfg)
except FileNotFoundError:
    cfg = {}

# first-run CORS origins
if "cors_origins" not in cfg:
    raw = input("Enter CORS origins (comma separated, blank=wildcard):\n> ").strip()
    domains = [ urlparse(u if u.startswith("http") else "http://"+u).netloc
                for u in raw.split(",") if u.strip() ]
    cfg["cors_origins"] = domains or ["*"]

# first-run LocalTunnel subdomain
if not cfg.get("subdomain"):
    cfg["subdomain"] = input("Enter desired LocalTunnel subdomain:\n> ").strip()

# CLI overrides
p = argparse.ArgumentParser()
p.add_argument("-p","--port",    type=int,   default=None)
p.add_argument("-s","--subdomain",type=str,   default=None)
args = p.parse_args()
PORT      = args.port      or cfg.get("port", 3000)
SUBDOMAIN = args.subdomain or cfg["subdomain"]
print(f"→ Using port={PORT} subdomain={SUBDOMAIN}")

# ── V) LAUNCH LOCALTUNNEL ──────────────────────────────────────────────────────
LT = shutil.which("lt") or (shutil.which("npx") and ["npx","--yes","localtunnel"][0])
if not LT:
    print("‼️ Install `lt` or `npx` first."); sys.exit(1)
launcher = [LT] if isinstance(LT, str) else ["npx","--yes","localtunnel"]

def try_tunnel(port, sub):
    cmd = launcher + ["--port",str(port),"--subdomain",sub]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    fd   = proc.stderr.fileno()
    fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL)| os.O_NONBLOCK)
    while True:
        if proc.poll() is not None:
            err = proc.stderr.read().strip()
            return None, err or f"exit {proc.returncode}"
        ready,__,_ = select.select([proc.stdout],[],[],0.1)
        if ready:
            return proc, proc.stdout.readline().strip()

def launch_with_backoff():
    delay = 5
    while True:
        print(f"→ Trying tunnel '{SUBDOMAIN}'…")
        proc, res = try_tunnel(PORT, SUBDOMAIN)
        if proc: return proc, res
        print(f"‼️ Tunnel error: {res}. Retrying in {delay}s…")
        time.sleep(delay); delay = min(delay*2,300)

proc, public_url = launch_with_backoff()
print("→ Public URL:", public_url)

# persist updated config
cfg.update(port=PORT, subdomain=SUBDOMAIN,
           cors_origins=cfg["cors_origins"],
           localtunnel_domain=urlparse(public_url).netloc)
json.dump(cfg, open(cfg_path,"w"), indent=4)

# ── VI) CORS SETUP ────────────────────────────────────────────────────────────
if cfg["cors_origins"]==["*"]:
    allowed = "*"
else:
    allowed = [f"https://{d}" for d in cfg["cors_origins"]]
    allowed.append(f"https://{cfg['localtunnel_domain']}")

print("→ Allowed CORS origins:", allowed)

# ── VII) FLASK + SOCKET.IO ────────────────────────────────────────────────────
app      = Flask(__name__)
CORS(app, resources={r"/*":{"origins":allowed}})
socketio = SocketIO(app, cors_allowed_origins=allowed, async_mode="eventlet")

clients = {}   # sid → { uuid, roles }

def gen_uuid():
    return "-".join("".join(random.choice("0123456789abcdef") for _ in range(4)) for _ in range(4))

@app.after_request
def add_headers(resp):
    if allowed=="*":
        resp.headers["Access-Control-Allow-Origin"]="*"
    else:
        origin = request.headers.get("Origin")
        if origin in allowed:
            resp.headers["Access-Control-Allow-Origin"]=origin
            resp.headers["Access-Control-Allow-Methods"]="GET, POST"
            resp.headers["Access-Control-Allow-Headers"]="Content-Type"
    return resp

# ── VIII) AUTH ROUTE ─────────────────────────────────────────────────────────
@app.route("/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    uuid     = data.get("uuid","").strip()
    password = data.get("password","")
    if password != os.environ.get("PEER_SHARED_SECRET","neck-peer-password"):
        return jsonify({"error":"invalid credentials"}), 401

    token = jwt.encode({
        "sub": uuid,
        "roles": ["peer"],
        "exp": datetime.utcnow() + JWT_EXP
    }, SECRET_KEY, algorithm="HS256")
    return jsonify({"token": token})

# ── IX) SOCKET.IO EVENT HANDLERS ──────────────────────────────────────────────
@socketio.on("connect")
def ws_connect(auth):
    token = auth.get("token") if isinstance(auth, dict) else None
    if not token:
        return False  # reject

    try:
        claims = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except Exception as e:
        print("→ JWT error:", e)
        return False

    sid  = request.sid
    uuid = claims["sub"]
    roles= claims.get("roles",[])
    print(f"[+] AUTH connect: sid={sid} uuid={uuid} roles={roles}")

    clients[sid] = {"uuid":uuid,"roles":roles}
    # notify others
    emit("new-peer", {"id":sid,"uuid":uuid,"roles":roles},
         broadcast=True, include_self=False)
    # send existing
    peers = [{"id":pid,**d} for pid,d in clients.items() if pid!=sid]
    emit("existing-peers", peers)

@socketio.on("disconnect")
def ws_disconnect():
    sid = request.sid
    print(f"[-] Disconnect: sid={sid}")
    clients.pop(sid,None)
    emit("peer-disconnect", sid, broadcast=True)

def require_role(sid, role):
    return role in clients.get(sid,{}).get("roles",[])

@socketio.on("peer-message")
def ws_peer_message(data):
    sid = request.sid
    if not require_role(sid,"peer"):
        return emit("error-message", {"message":"not authorized"})
    target = data.get("target")
    msg    = data.get("message")
    if target in clients:
        socketio.emit("peer-message", {"peerId":sid,"message":msg}, room=target)
    else:
        emit("error-message", {"message":f"Peer {target} not found"})

@socketio.on("broadcast-message")
def ws_broadcast(data):
    sid = request.sid
    if not require_role(sid,"peer"):
        return emit("error-message", {"message":"not authorized"})
    socketio.emit("peer-message", {"peerId":sid,"message":data.get("message")}, broadcast=True, include_self=False)

# ── X) HEARTBEAT MONITOR ──────────────────────────────────────────────────────
def heartbeat():
    failures = 0
    while True:
        try:
            r = urllib.request.urlopen(public_url, timeout=5)
            code = getattr(r,"status",None) or r.getcode()
            if code==200:
                failures=0; print("✅ Heartbeat OK")
            else:
                failures+=1; print(f"⚠️ Heartbeat {code} ({failures}/5)")
        except Exception as e:
            failures+=1; print(f"⚠️ Heartbeat fail ({failures}/5): {e}")
        if failures>=5:
            print("‼️ Restarting…")
            os.execv(sys.executable,[sys.executable]+sys.argv)
        time.sleep(60)

# ── XI) RUN ───────────────────────────────────────────────────────────────────
if __name__=="__main__":
    threading.Thread(target=heartbeat,daemon=True).start()
    socketio.run(app, host="0.0.0.0", port=PORT)
