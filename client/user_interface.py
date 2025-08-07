#!/usr/bin/env python3
"""
user_interface.py — minimal REPL client for the neck (signaling peer).

• First run:
    - asks for signaling-server URL & shared password
    - writes .env   and  config_interface.json
• Always:
    - reloads settings automatically (CLI can override)
    - relaunches itself inside ./int_venv  (created on first run)
    - installs missing deps exactly once if ever absent
    - keeps retrying /login until server & neck_agent are ready
"""
from __future__ import annotations
import os, sys, json, secrets, argparse, time, subprocess, pathlib
from urllib.parse import urljoin

# ────────────────────────── paths & constants ───────────────────────── #
BASE_DIR   = pathlib.Path(__file__).resolve().parent
ENV_PATH   = BASE_DIR / ".env"
CFG_PATH   = BASE_DIR / "config_interface.json"
VENV_DIR   = BASE_DIR / "int_venv"
PY_VENV    = VENV_DIR / ("Scripts" if os.name == "nt" else "bin") / "python"
REQS       = ["requests", "python-socketio[client]"]

# ─────────────────────  0) helper: inside v-env?  ───────────────────── #
def in_venv() -> bool:
    """True when running from *any* virtual-env (robust)."""
    return sys.prefix != sys.base_prefix   # standard venv litmus test

def ensure_int_venv_once() -> None:
    """Create venv + pip-install REQS the *first* time only."""
    if VENV_DIR.exists():
        return
    print("[ui] creating int_venv …")
    subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
    print("[ui] installing deps …  (one-time)")
    subprocess.check_call([str(PY_VENV), "-m", "pip", "install",
                           "--quiet", "--upgrade", "pip", *REQS])

# ─────────────────────  1) .env  (create / load)  ───────────────────── #
if not ENV_PATH.exists():
    shared_pw = input("Shared password with signaling server (blank = random):\n> ").strip() \
                or secrets.token_urlsafe(16)
    ENV_PATH.write_text(f"PEER_SHARED_SECRET={shared_pw}\n")
    print("[ui] wrote .env")

for ln in ENV_PATH.read_text().splitlines():
    if "=" in ln and not ln.lstrip().startswith("#"):
        k, v = ln.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

SHARED_PW = os.environ["PEER_SHARED_SECRET"]

# ───────────────────  2) config_interface.json  ────────────────────── #
cfg: dict[str, str] = json.loads(CFG_PATH.read_text()) if CFG_PATH.exists() else {}

# ───────────────────  3) CLI parsing & prompts  ────────────────────── #
ap = argparse.ArgumentParser(description="Neck REPL client")
ap.add_argument("-s", "--server-url", help="Signaling-server base URL")
ap.add_argument("-u", "--uuid",       help="Your peer UUID")
cli = ap.parse_args()

def ask(prompt: str) -> str: return input(prompt).strip()

server_url = (cli.server_url or cfg.get("server_url")
              or ask("Signaling server URL:\n> ")).rstrip("/")
uuid       = (cli.uuid or cfg.get("uuid")
              or ask("Your UUID (blank = random):\n> ")
              or "-".join(secrets.token_hex(2) for _ in range(4)))

cfg.update(server_url=server_url, uuid=uuid)
CFG_PATH.write_text(json.dumps(cfg, indent=4))

# ───────────────────  4) ensure we are running inside v-env  ───────── #
if not in_venv():
    ensure_int_venv_once()
    # re-exec inside the freshly created / existing v-env
    os.execv(str(PY_VENV), [str(PY_VENV), *sys.argv])

# (   From here down we **are** inside the v-env   ) ------------------- #

# ───────────────────  4b) install *missing* deps once  ─────────────── #
missing: list[str] = []
try:
    import requests          # type: ignore
except ImportError:
    missing.append("requests")

try:
    import socketio          # type: ignore
except ImportError:
    missing.append("python-socketio[client]")

if missing:
    print("[ui] missing deps detected – installing:", ", ".join(missing))
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "--quiet", *missing])
    # relaunch once with deps present
    os.execv(sys.executable, [sys.executable, *sys.argv])

# ───────────────────  5) runtime logic (login loop)  ───────────────── #
import requests, socketio   # noqa: E402  (after deps ensured)

LOGIN_URL = urljoin(server_url + "/", "login")

def try_login() -> str | None:
    try:
        r = requests.post(LOGIN_URL, json={"uuid": uuid, "password": SHARED_PW}, timeout=5)
        if r.status_code == 200:
            return r.json()["token"]
        msg = "bad credentials" if r.status_code == 401 else f"HTTP {r.status_code}"
        print(f"[ui] /login → {msg}")
    except Exception as e:   # noqa: BLE001
        print(f"[ui] /login unreachable: {e}")
    return None

print(f"[ui] contacting {LOGIN_URL}")
token: str | None = None
while token is None:
    token = try_login() or (time.sleep(3) or None)
print("[ui] JWT acquired ✓")

# ───────────────────  6) Socket.IO  + REPL  ────────────────────────── #
sio = socketio.Client(reconnection=True, logger=False)

@sio.event
def connect():    print("[ui] WS connected")
@sio.event
def disconnect(): print("[ui] WS disconnected")
@sio.on("peer-message")
def _peer(data):  print(f"[ui] ← {data}")

while True:
    try:
        sio.connect(server_url, auth={"token": token})
        break
    except Exception as e:                        # noqa: BLE001
        print(f"[ui] WS connect error: {e}")
        time.sleep(3)

print("\n[ui] READY — type commands or 'quit'.\n")
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
