#!/usr/bin/env python3
"""
user_interface.py — minimal REPL client for the neck (signaling peer).

• First run:
    - asks for signaling-server URL
    - asks for a shared password (blank ⇒ random)
    - writes .env   and  config_interface.json
• Always:
    - reloads settings automatically (CLI can override)
    - relaunches itself inside ./int_venv  (created once)
    - (re)installs deps in that venv if they’re missing
    - keeps retrying /login until both server & neck_agent are ready
"""
from __future__ import annotations

# ───────────────────────── std-lib imports ────────────────────────── #
import os, sys, json, secrets, argparse, time, subprocess, pathlib

BASE_DIR   = pathlib.Path(__file__).resolve().parent
ENV_PATH   = BASE_DIR / ".env"
CFG_PATH   = BASE_DIR / "config_interface.json"

# ─────────────────────── 1) .env  (create / load) ─────────────────── #
if not ENV_PATH.exists():
    jwt_secret   = secrets.token_urlsafe(32)
    pw_input     = input("Enter shared password with signaling server (blank = random):\n> ").strip()
    shared_pw    = pw_input or secrets.token_urlsafe(16)
    ENV_PATH.write_text(
        f"PEER_SHARED_SECRET={shared_pw}\n"        # client only needs the shared PW
        f"# JWT_SECRET lives ONLY on the server\n"
    )
    print("[ui] created .env")

for ln in ENV_PATH.read_text().splitlines():
    if "=" in ln and not ln.lstrip().startswith("#"):
        k, v = ln.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

# ─────────────────────── 2) config_interface.json ──────────────────── #
cfg: dict[str, str] = {}
if CFG_PATH.exists():
    cfg = json.loads(CFG_PATH.read_text())

# ─────────────────────── 3) CLI / interactive prompts ─────────────── #
ap = argparse.ArgumentParser(description="Neck REPL client via signaling server")
ap.add_argument("-s", "--server-url", help="Signaling-server base URL")
ap.add_argument("-u", "--uuid",       help="Your peer UUID")
cli = ap.parse_args()

def ask(prompt: str) -> str:
    return input(prompt).strip()

server_url = (cli.server_url or cfg.get("server_url") or ask("Signaling server URL:\n> ")).rstrip("/")
uuid       = (cli.uuid       or cfg.get("uuid")       or ask("Your UUID (blank=random):\n> ") or
              "-".join(secrets.token_hex(2) for _ in range(4)))

cfg.update(server_url=server_url, uuid=uuid)
CFG_PATH.write_text(json.dumps(cfg, indent=4))

# ─────────────────────── 4) bootstrap int_venv ────────────────────── #
VENV_DIR   = BASE_DIR / "int_venv"
PY_VENV    = VENV_DIR / ("Scripts" if os.name == "nt" else "bin") / "python"

def inside_venv() -> bool:
    return pathlib.Path(sys.executable).resolve() == PY_VENV.resolve()

REQS = ["requests", "python-socketio[client]"]

if not inside_venv():
    if not VENV_DIR.exists():
        print("[ui] creating int_venv …")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
    # always try to install/upgrade deps when *leaving* host python
    subprocess.check_call([str(PY_VENV), "-m", "pip", "install", "--quiet", "--upgrade", "pip", *REQS])
    os.execv(str(PY_VENV), [str(PY_VENV), *sys.argv])   # re-exec inside venv

# 4b) even inside the venv, re-check and install anything missing —— #
missing: list[str] = []
for pkg, mod in [("requests", "requests"), ("python-socketio[client]", "socketio")]:
    try:
        __import__(mod)
    except ModuleNotFoundError:
        missing.append(pkg)

if missing:
    print("[ui] installing missing deps inside venv:", ", ".join(missing))
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *missing])
    os.execv(sys.executable, [sys.executable, *sys.argv])   # restart once with deps

# ─────────────────────── 5) runtime logic  ────────────────────────── #
import requests, socketio
from urllib.parse import urljoin

LOGIN_URL = urljoin(server_url + "/", "login")
SHARED_PW = os.environ["PEER_SHARED_SECRET"]

def try_login() -> "str | None":      # keep as str|None under Py≥3.10, fine on Py3.8/3.9
    try:
        r = requests.post(LOGIN_URL, json={"uuid": uuid, "password": SHARED_PW}, timeout=5)
        if r.status_code == 200 and "token" in r.json():
            return r.json()["token"]
        msg = "bad credentials" if r.status_code == 401 else f"HTTP {r.status_code}: {r.text.strip()}"
        print(f"[ui] /login → {msg}")
    except Exception as e:
        print(f"[ui] /login unreachable: {e}")
    return None

print(f"[ui] contacting {LOGIN_URL}")
token = None
while token is None:
    token = try_login() or (time.sleep(3) or None)

print("[ui] JWT acquired ✓")

# ─────────────────────── 6) Socket.IO client  ─────────────────────── #
sio = socketio.Client(reconnection=True)

@sio.event
def connect():
    print("[ui] WS connected")

@sio.event
def disconnect():
    print("[ui] WS disconnected")

@sio.on("peer-message")
def _on_peer(data):
    print(f"[ui] ← {data}")

connected = False
while not connected:
    try:
        sio.connect(server_url, auth={"token": token})
        connected = True
    except Exception as e:
        print(f"[ui] WS connect error: {e}")
        time.sleep(3)

# ─────────────────────── 7) REPL loop  ─────────────────────────────── #
print("\n[ui] READY — type neck commands, or 'quit' to exit.\n")
try:
    while True:
        cmd = input("> ").strip()
        if not cmd:
            continue
        if cmd.lower() in {"quit", "exit"}:
            break
        sio.emit("broadcast-message", {"message": cmd})
        print(f"[ui] → sent '{cmd}'")
except (EOFError, KeyboardInterrupt):
    pass
finally:
    sio.disconnect()
    print("[ui] bye")
