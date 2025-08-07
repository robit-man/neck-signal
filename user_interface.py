#!/usr/bin/env python3
"""
user_interface.py — simple REPL → signaling server → neck_agent, with JWT auth.
"""

import os, sys, secrets

# ── A) .env GENERATION & LOADING ─────────────────────────────────────────────
script_dir = os.path.dirname(os.path.abspath(__file__))
env_path   = os.path.join(script_dir, ".env")
if not os.path.exists(env_path):
    # generate the same keys (so UI & agent share them)
    with open(env_path, "w") as f:
        f.write(f"JWT_SECRET={secrets.token_urlsafe(32)}\n")
        f.write(f"PEER_SHARED_SECRET={secrets.token_urlsafe(32)}\n")
    print("[ui] created .env with random secrets")

for line in open(env_path):
    if "=" not in line or line.strip().startswith("#"):
        continue
    k,v = line.strip().split("=",1)
    os.environ.setdefault(k, v)

# ── B) VENV BOOTSTRAP & RE-EXEC ───────────────────────────────────────────────
venv_dir = os.path.join(script_dir, "venv")
venv_py  = os.path.join(venv_dir, "bin", "python")
def in_venv():
    return os.path.abspath(sys.executable) == os.path.abspath(venv_py)
DEPS = ["python-socketio[client]", "requests"]
if not in_venv():
    if not os.path.isdir(venv_dir):
        print("[ui] creating venv…")
        import subprocess; subprocess.check_call([sys.executable, "-m", "venv", venv_dir])
        print("[ui] installing deps…")
        subprocess.check_call([venv_py, "-m", "pip", "install", "--upgrade", "pip"])
        subprocess.check_call([venv_py, "-m", "pip", "install"] + DEPS)
    os.execv(venv_py, [venv_py] + sys.argv)

# ── C) ACTUAL LOGIC ───────────────────────────────────────────────────────────
import argparse, socketio, requests

p = argparse.ArgumentParser(description="user_interface")
p.add_argument("-s","--server-url", required=True, help="e.g. http://host:port")
p.add_argument("-u","--uuid",       required=True)
args = p.parse_args()
SERVER = args.server_url.rstrip("/")

# 1) login → get JWT
resp = requests.post(f"{SERVER}/login",
                     json={"uuid": args.uuid,
                           "password": os.environ["PEER_SHARED_SECRET"]})
if resp.status_code != 200:
    print("[ui] login failed:", resp.text, file=sys.stderr); sys.exit(1)
token = resp.json()["token"]
print("[ui] JWT acquired")

# 2) connect socket.io
sio = socketio.Client()
@sio.event
def connect():
    print("[ui] WS connected")
@sio.event
def disconnect():
    print("[ui] WS disconnected")
@sio.on("neck_ack")
def on_ack(data):
    print(f"[ui] ACK ← {data.get('command')}")

sio.connect(SERVER, auth={"token": token})
print("[ui] ready. Type commands, Ctrl-C to quit.")

try:
    while True:
        cmd = input("command> ").strip()
        if cmd:
            sio.emit("neck_command", {"command": cmd})
            print(f"[ui] → emitted {cmd}")
except KeyboardInterrupt:
    pass
finally:
    sio.disconnect()
