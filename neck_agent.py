#!/usr/bin/env python3
"""
neck_agent.py — bridge between signaling server and neck over serial, with JWT auth.
"""

import os, sys, secrets

# ── A) .env GENERATION & LOADING ─────────────────────────────────────────────
script_dir = os.path.dirname(os.path.abspath(__file__))
env_path   = os.path.join(script_dir, ".env")
if not os.path.exists(env_path):
    # generate secrets
    with open(env_path, "w") as f:
        f.write(f"JWT_SECRET={secrets.token_urlsafe(32)}\n")
        f.write(f"PEER_SHARED_SECRET={secrets.token_urlsafe(32)}\n")
    print("[neck_agent] created .env with random secrets")

# load .env
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
DEPS = ["pyserial", "python-socketio[client]", "requests"]
if not in_venv():
    if not os.path.isdir(venv_dir):
        print("[neck_agent] creating venv…")
        import subprocess; subprocess.check_call([sys.executable, "-m", "venv", venv_dir])
        print("[neck_agent] installing deps…")
        subprocess.check_call([venv_py, "-m", "pip", "install", "--upgrade", "pip"])
        subprocess.check_call([venv_py, "-m", "pip", "install"] + DEPS)
    # re-exec into venv without reinstall
    os.execv(venv_py, [venv_py] + sys.argv)

# ── C) ACTUAL LOGIC ───────────────────────────────────────────────────────────
import argparse, time, serial, socketio, requests

SERVER = None
p = argparse.ArgumentParser(description="neck_agent")
p.add_argument("-s","--server-url", required=True, help="e.g. http://host:port")
p.add_argument("-u","--uuid",       required=True)
args = p.parse_args()
SERVER = args.server_url.rstrip("/")

# 1) login → get JWT
resp = requests.post(f"{SERVER}/login",
                     json={"uuid": args.uuid,
                           "password": os.environ["PEER_SHARED_SECRET"]})
if resp.status_code != 200:
    print("[neck_agent] login failed:", resp.text, file=sys.stderr); sys.exit(1)
token = resp.json()["token"]
print("[neck_agent] JWT acquired")

# 2) open serial
ser = None
for port in ("/dev/tty0","/dev/tty1"):
    try:
        ser = serial.Serial(port, 115200, timeout=1)
        print(f"[neck_agent] opened {port}"); break
    except:
        pass
if not ser:
    print("[neck_agent] no serial port found", file=sys.stderr); sys.exit(1)

# 3) connect socket.io
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

sio.connect(SERVER, auth={"token": token})
print("[neck_agent] ready")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    ser.close()
    sio.disconnect()
