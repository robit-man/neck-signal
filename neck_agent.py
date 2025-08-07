#!/usr/bin/env python3
"""
neck_agent.py — bridge between signaling server and neck over serial, with JWT auth.
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
    print("[neck_agent] created .env with random secrets")

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
p = argparse.ArgumentParser(description="neck_agent")
p.add_argument("-s","--server-url", help="Signaling server URL (e.g. http://host:3000)")
p.add_argument("-u","--uuid",       required=True, help="This agent's UUID")
args = p.parse_args()

if args.server_url:
    server = args.server_url.rstrip("/")
    cfg["server_url"] = server
elif "server_url" in cfg:
    server = cfg["server_url"]
else:
    server = input("Enter signaling server URL (e.g. http://host:3000): ").strip().rstrip("/")
    cfg["server_url"] = server

# persist updated config
with open(config_path,"w") as f:
    json.dump(cfg, f, indent=4)

# ── D) VENV BOOTSTRAP & RE-EXEC ───────────────────────────────────────────────
venv_dir = os.path.join(script_dir, "venv")
venv_py  = os.path.join(venv_dir, "bin", "python")
def in_venv(): return os.path.abspath(sys.executable)==os.path.abspath(venv_py)

DEPS = ["pyserial","python-socketio[client]","requests"]
if not in_venv():
    if not os.path.isdir(venv_dir):
        print("[neck_agent] creating venv…")
        import subprocess; subprocess.check_call([sys.executable,"-m","venv",venv_dir])
        print("[neck_agent] installing deps…")
        subprocess.check_call([venv_py,"-m","pip","install","--upgrade","pip"])
        subprocess.check_call([venv_py,"-m","pip","install"]+DEPS)
    os.execv(venv_py, [venv_py]+sys.argv)

# ── E) ACTUAL LOGIC ───────────────────────────────────────────────────────────
import time, serial, socketio, requests
from urllib.parse import urljoin

# 1) JWT login
login_url = urljoin(server, "/login")
resp = requests.post(login_url, json={
    "uuid": args.uuid,
    "password": os.environ["PEER_SHARED_SECRET"]
})
if resp.status_code != 200:
    print("[neck_agent] login failed:", resp.text, file=sys.stderr)
    sys.exit(1)
token = resp.json()["token"]
print("[neck_agent] JWT acquired")

# 2) open serial on /dev/tty0 or /dev/tty1
ser = None
for port in ("/dev/tty0","/dev/tty1"):
    try:
        ser = serial.Serial(port,115200,timeout=1)
        print(f"[neck_agent] opened {port}")
        break
    except:
        pass
if not ser:
    print("[neck_agent] no serial port found", file=sys.stderr)
    sys.exit(1)

# 3) connect Socket.IO
sio = socketio.Client()
@sio.event
def connect():
    print("[neck_agent] WS connected")
@sio.event
def disconnect():
    print("[neck_agent] WS disconnected")
@sio.on("neck_command")
def on_cmd(data):
    cmd = data.get("command","").strip()
    if cmd:
        ser.write((cmd+"\n").encode("utf-8"))
        print(f"[neck_agent] → {cmd}")
        sio.emit("neck_ack", {"command":cmd})

sio.connect(server, auth={"token":token})
print("[neck_agent] ready")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    ser.close()
    sio.disconnect()
