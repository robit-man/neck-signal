#!/usr/bin/env python3
"""
neck_agent.py  –  Socket.IO ⇄ Serial bridge secured with JWT
──────────────────────────────────────────────────────────────
 • Creates / reuses  ag_venv/
 • Persists signalling-server URL  &  UUID  in config.json
 • First run asks for the shared password (so it matches the UI)
"""

from __future__ import annotations

# ── 0. std-lib imports (only!) ──────────────────────────────────────
import os, sys, json, secrets, argparse, time, subprocess, pathlib, platform, venv
from typing import Optional
from urllib.parse import urljoin

ROOT       = pathlib.Path(__file__).resolve().parent
VENV_DIR   = ROOT / "ag_venv"
VENV_PY    = VENV_DIR / ("Scripts" if platform.system() == "Windows" else "bin") / "python"
CFG_PATH   = ROOT / "config.json"
ENV_PATH   = ROOT / ".env"

DEPS = ["pyserial", "python-socketio[client]", "requests"]

# ───────────────── 1) v-env bootstrap (before heavy imports) ──────────────────
def inside_venv() -> bool:
    return pathlib.Path(sys.executable).resolve() == VENV_PY.resolve()

def create_venv():
    print("[agent] creating ag_venv …")
    venv.EnvBuilder(with_pip=True).create(VENV_DIR)
    subprocess.check_call([str(VENV_PY), "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
    subprocess.check_call([str(VENV_PY), "-m", "pip", "install", "--quiet", *DEPS])

if not inside_venv():
    if not VENV_DIR.exists():
        create_venv()
    else:
        # ensure deps; if any missing we'll install then exec again
        missing = []
        for pkg, mod in [("pyserial", "serial"),
                         ("requests", "requests"),
                         ("python-socketio[client]", "socketio")]:
            try:
                __import__(mod)
            except ImportError:
                missing.append(pkg)
        if missing:
            print("[agent] installing missing deps inside venv:", ", ".join(missing))
            subprocess.check_call([str(VENV_PY), "-m", "pip", "install", "--quiet", *missing])
    # re-exec inside v-env
    os.execv(str(VENV_PY), [str(VENV_PY), *sys.argv])

# ───────────────── 2) .env creation / loading ────────────────────────────────
if not ENV_PATH.exists():
    print("[agent] first run – need shared password with signalling server")
    pw = input("Shared password (same one you used in user_interface, blank=random):\n> ").strip()
    shared_pw = pw or secrets.token_urlsafe(16)
    ENV_PATH.write_text(
        f"PEER_SHARED_SECRET={shared_pw}\n"
        f"JWT_SECRET={secrets.token_urlsafe(32)}   # not used by agent but handy\n"
    )
    print("[agent] wrote .env")

for line in ENV_PATH.read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

PEER_SECRET = os.environ["PEER_SHARED_SECRET"]

# ───────────────── 3) config.json (server URL & UUID) ─────────────────────────
cfg: dict[str, str] = {}
if CFG_PATH.exists():
    cfg = json.loads(CFG_PATH.read_text())

ap = argparse.ArgumentParser(description="neck_agent – Serial ↔ Socket.IO bridge")
ap.add_argument("-s", "--server-url", help="signalling server base URL")
ap.add_argument("-u", "--uuid",       help="this agent's UUID")
cli = ap.parse_args()

def ask(prompt: str) -> str:
    return input(prompt).strip()

server_url = (cli.server_url or cfg.get("server_url") or ask("Signalling server URL:\n> ")).rstrip("/")
agent_uuid = (cli.uuid       or cfg.get("uuid")       or ask("Agent UUID (blank=random):\n> ") or
              "-".join(secrets.token_hex(2) for _ in range(4)))

cfg.update(server_url=server_url, uuid=agent_uuid)
CFG_PATH.write_text(json.dumps(cfg, indent=4))

print(f"[agent] config  →  server={server_url}   uuid={agent_uuid}")

# ───────────────── 4) heavy imports (safe in v-env) ───────────────────────────
import requests, socketio, serial         # type: ignore  (pylint / mypy)

# ───────────────── 5) helper: keep-trying POST /login ─────────────────────────
LOGIN_URL = urljoin(server_url + "/", "login")

def try_login() -> Optional[str]:
    try:
        r = requests.post(LOGIN_URL,
                          json={"uuid": agent_uuid, "password": PEER_SECRET},
                          timeout=4)
        if r.status_code == 200 and "token" in r.json():
            return r.json()["token"]
        if r.status_code == 401:
            print("[agent] /login → bad credentials (server not aware yet)")
    except Exception as e:  # noqa: broad-except
        print(f"[agent] /login unreachable: {e}")
    return None

print(f"[agent] contacting {LOGIN_URL}")
jwt_token: Optional[str] = None
while jwt_token is None:
    jwt_token = try_login()
    if jwt_token is None:
        time.sleep(3)

print("[agent] JWT acquired ✓")

# ───────────────── 6) open Serial (retry) ─────────────────────────────────────
SER_CANDIDATES = ["/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/tty0", "/dev/tty1", "COM3", "COM4"]
ser: Optional[serial.Serial] = None
while ser is None:
    for port in SER_CANDIDATES:
        try:
            ser = serial.Serial(port, 115200, timeout=1)
            print("[agent] opened serial port:", port)
            break
        except Exception:  # noqa: broad-except
            ser = None
    if ser is None:
        print("[agent] no serial port – retry in 3 s")
        time.sleep(3)

# ───────────────── 7) Socket.IO ⇄ Serial bridge ──────────────────────────────
sio = socketio.Client(reconnection=True, reconnection_attempts=0)  # infinite tries

@sio.event
def connect():
    print("[agent] WS connected")

@sio.event
def disconnect():
    print("[agent] WS disconnected")

@sio.on("neck_command")
def on_neck_command(data):
    cmd = str(data.get("command", "")).strip()
    if cmd:
        ser.write((cmd + "\n").encode())
        print(f"[agent] → serial  {cmd}")
        sio.emit("neck_ack", {"command": cmd})

# connect (auth with JWT)
connected = False
while not connected:
    try:
        sio.connect(server_url, auth={"token": jwt_token})
        connected = True
    except Exception as e:  # noqa: broad-except
        print(f"[agent] WS connect error: {e}")
        time.sleep(3)

print("[agent] bridge live – Ctrl-C to quit")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    try:
        ser.close()
    except Exception:  # noqa: broad-except
        ...
    try:
        sio.disconnect()
    except Exception:  # noqa: broad-except
        ...
    print("[agent] bye")
