#!/usr/bin/env python3
"""
user_interface.py — REPL client for the neck, with JWT auth.
Remembers your server endpoint in config.json.
"""

import os, sys, secrets, json

# ── A) LOCATION SETUP ─────────────────────────────────────────────────────────
script_dir  = os.path.dirname(os.path.abspath(__file__))
env_path    = os.path.join(script_dir, ".env")
config_path = os.path.join(script_dir, "config.json")

# ── B) .env GENERATION & LOADING ─────────────────────────────────────────────
if not os.path.exists(env_path):
    with open(env_path, "w") as f:
        f.write(f"JWT_SECRET={secrets.token_urlsafe(32)}\n")
        f.write(f"PEER_SHARED_SECRET={secrets.token_urlsafe(32)}\n")
    print("[ui] created .env with random secrets")

for line in open(env_path):
    if "=" in line and not line.strip().startswith("#"):
        k,v = line.strip().split("=",1)
        os.environ.setdefault(k, v)

# ── C) config.json LOAD & SERVER PROMPT ───────────────────────────────────────
try:
    cfg = json.load(open(config_path))
except FileNotFoundError:
    cfg = {}

import argparse
p = argparse.ArgumentParser(description="user_interface")
p.add_argument("-s","--server-url", help="Signaling server URL (e.g. http://host:3000)")
p.add_argument("-u","--uuid",       required=True, help="Your UUID")
args = p.parse_args()

if args.server_url:
    server = args.server_url.rstrip("/")
    cfg["server_url"] = server
elif "server_url" in cfg:
    server = cfg["server_url"]
else:
    server = input("Enter signaling server URL (e.g. http://host:3000): ").strip().rstrip("/")
    cfg["server_url"] = server

with open(config_path,"w") as f:
    json.dump(cfg, f, indent=4)

# ── D) VENV BOOTSTRAP & RE-EXEC ───────────────────────────────────────────────
venv_dir = os.path.join(script_dir, "venv")
venv_py  = os.path.join(venv_dir, "bin", "python")
def in_venv(): return os.path.abspath(sys.executable)==os.path.abspath(venv_py)

DEPS = ["python-socketio[client]","requests"]
if not in_venv():
    if not os.path.isdir(venv_dir):
        print("[ui] creating venv…")
        import subprocess; subprocess.check_call([sys.executable,"-m","venv",venv_dir])
        print("[ui] installing deps…")
        subprocess.check_call([venv_py,"-m","pip","install","--upgrade","pip"])
        subprocess.check_call([venv_py,"-m","pip","install"]+DEPS)
    os.execv(venv_py, [venv_py]+sys.argv)

# ── E) ACTUAL LOGIC ───────────────────────────────────────────────────────────
import socketio, requests
from urllib.parse import urljoin

login_url = urljoin(server,"/login")
resp = requests.post(login_url, json={
    "uuid": args.uuid,
    "password": os.environ["PEER_SHARED_SECRET"]
})
if resp.status_code!=200:
    print("[ui] login failed:", resp.text, file=sys.stderr)
    sys.exit(1)
token = resp.json()["token"]
print("[ui] JWT acquired")

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

sio.connect(server, auth={"token":token})
print("[ui] ready. Type commands, Ctrl-C to quit.")
try:
    while True:
        cmd = input("command> ").strip()
        if cmd:
            sio.emit("neck_command", {"command":cmd})
            print(f"[ui] → emitted {cmd}")
except KeyboardInterrupt:
    pass
finally:
    sio.disconnect()
