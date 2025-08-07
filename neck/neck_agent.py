#!/usr/bin/env python3
"""
neck_agent.py  –  JWT-secured Socket.IO ⇄ Serial bridge

• boots / re-uses  ag_venv/  (installs pyserial, requests, python-socketio)
• remembers  server-URL  &  UUID  in  config.json
• first run asks for the shared password (.env → PEER_SHARED_SECRET)
• on serial-connect:
      – prints an example of a *full* command
      – sends HOME once
• every incoming command from the UI:
      – "home"  →  literal HOME
      – partial  →  merged into full X,Y,Z,H,S,A,R,P and validated
"""

###############################################################################
# 0.  ▸▸▸  very light v-env bootstrap  ▸▸▸
###############################################################################
from __future__ import annotations
import os, sys, subprocess, venv, json, secrets, argparse, time, platform, pathlib
from typing import Optional
from urllib.parse import urljoin

ROOT      = pathlib.Path(__file__).resolve().parent
VENV_DIR  = ROOT / "ag_venv"
VENV_PY   = VENV_DIR / ("Scripts" if platform.system() == "Windows" else "bin") / "python"
DEPS      = ["pyserial", "python-socketio[client]", "requests"]

def in_venv() -> bool:
    return sys.prefix != sys.base_prefix               # reliable for Py ≥3.4

if not in_venv():
    if not VENV_DIR.exists():
        print("[agent] creating ag_venv …")
        venv.EnvBuilder(with_pip=True).create(VENV_DIR)
        subprocess.check_call([str(VENV_PY), "-m", "pip", "install",
                               "--quiet", "--upgrade", "pip", *DEPS])
    os.execv(str(VENV_PY), [str(VENV_PY), *sys.argv])

# inside the venv: make sure *all* deps are present (add later if user deletes)
missing: list[str] = []
for pkg, mod in [("pyserial", "serial"),
                 ("requests", "requests"),
                 ("python-socketio[client]", "socketio")]:
    try: __import__(mod)
    except ModuleNotFoundError: missing.append(pkg)
if missing:
    print("[agent] installing extra deps:", ", ".join(missing))
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "--quiet", *missing])
    os.execv(sys.executable, [sys.executable, *sys.argv])     # restart once

###############################################################################
# 1.  .env  and  config.json
###############################################################################
ENV_PATH = ROOT / ".env"
CFG_PATH = ROOT / "config.json"

if not ENV_PATH.exists():
    pw = input("Shared password (same you gave the UI) [blank=random]:\n> ").strip() \
         or secrets.token_urlsafe(16)
    ENV_PATH.write_text(f"PEER_SHARED_SECRET={pw}\n"
                        f"JWT_SECRET={secrets.token_urlsafe(32)}\n")
    print("[agent] wrote .env")

for ln in ENV_PATH.read_text().splitlines():
    if "=" in ln and not ln.lstrip().startswith("#"):
        k,v = ln.split("=",1); os.environ.setdefault(k.strip(), v.strip())
PEER_SECRET = os.environ["PEER_SHARED_SECRET"]

cfg: dict = json.loads(CFG_PATH.read_text()) if CFG_PATH.exists() else {}

ap = argparse.ArgumentParser()
ap.add_argument("-s","--server-url")
ap.add_argument("-u","--uuid")
cli = ap.parse_args()

def ask(t:str)->str: return input(t).strip()

SERVER = (cli.server_url or cfg.get("server_url") or ask("Server URL:\n> ")).rstrip("/")
UUID   = (cli.uuid or cfg.get("uuid") or ask("Agent UUID [blank=random]:\n> ")
          or "-".join(secrets.token_hex(2) for _ in range(4)))

cfg.update(server_url=SERVER, uuid=UUID)
CFG_PATH.write_text(json.dumps(cfg, indent=4))

print(f"[agent]   server={SERVER}\n          uuid={UUID}")

###############################################################################
# 2.  heavy imports  (safe inside v-env)
###############################################################################
import requests, socketio, serial        # type: ignore

###############################################################################
# 3.  JWT login loop
###############################################################################
LOGIN_URL = urljoin(SERVER + "/", "login")
def try_login() -> Optional[str]:
    try:
        r = requests.post(LOGIN_URL, json={"uuid":UUID, "password":PEER_SECRET}, timeout=4)
        if r.status_code == 200 and "token" in r.json(): return r.json()["token"]
        print("[agent] /login →", r.status_code)
    except Exception as e: print("[agent] /login unreachable:", e)
    return None

token = None
while token is None:
    token = try_login() or (time.sleep(3) or None)
print("[agent] JWT ✓")

###############################################################################
# 4.  serial connect (+ HOME once, example)
###############################################################################
SER_POSSIBLE = ["/dev/ttyUSB0","/dev/ttyUSB1","/dev/tty0","/dev/tty1","COM3","COM4"]
ser: Optional[serial.Serial] = None
while ser is None:
    for p in SER_POSSIBLE:
        try:
            ser = serial.Serial(p, 115200, timeout=1)
            print("[agent] opened serial:", p)
            break
        except Exception:
            ser = None
    if ser is None:
        print("[agent] no serial – retry in 3 s"); time.sleep(3)

# ▸▸ internal state / helpers  ──────────────────────────────────────────────
ALLOWED = {
    "X": (-700, 700, int), "Y": (-700, 700, int), "Z": (-700, 700, int),
    "H": (0, 70, int),     "S": (0, 10, float),   "A": (0, 10, float),
    "R": (-700, 700, int), "P": (-700, 700, int),
}
state = {k: (1.0 if k in ("S","A") else 0) for k in ALLOWED}

def validate(cmd:str)->bool:
    if cmd.lower()=="home": return True
    seen=set()
    for tok in cmd.split(","):
        m = __import__("re").match(r"^([XYZHSARP])(-?\d+(?:\.\d+)?)$", tok.strip())
        if not m or m.group(1) in seen: return False
        k,val = m.group(1), m.group(2); low,high,cast = ALLOWED[k]
        try: v=cast(val)
        except: return False
        if not low<=v<=high: return False
        seen.add(k)
    return True

def merge(cmd:str)->str:
    if cmd.lower()=="home":
        for k in state: state[k]=1.0 if k in ("S","A") else 0
        return "HOME"
    for tok in cmd.split(","):
        m = __import__("re").match(r"^([XYZHSARP])(-?\d+(?:\.\d+)?)$", tok.strip())
        if m:
            k,val=m.group(1),m.group(2); state[k]=ALLOWED[k][2](val)
    return ",".join(f"{k}{state[k]}" for k in ["X","Y","Z","H","S","A","R","P"])

# example + home
print("[agent] example full cmd:  X100,Y-50,Z0,H30,S1,A1,R0,P0")
ser.write(b"HOME\n"); print("[agent] → serial  HOME")

###############################################################################
# 5.  Socket.IO bridge (peer-message → serial)
###############################################################################
sio = socketio.Client(reconnection=True, reconnection_attempts=0)

@sio.event
def connect():    print("[agent] WS connected")
@sio.event
def disconnect(): print("[agent] WS disconnected")

def handle_cmd(raw:str):
    if not validate(raw):
        print("[agent] ✗ invalid:", raw); return
    full = merge(raw)
    try:
        ser.write((full+"\n").encode())
        print(f"[agent] → serial  {full}")
        sio.emit("neck_ack", {"command":full})
    except Exception as e:
        print("[agent] serial error:", e)

@sio.on("peer-message")          # relayed from UI
def _msg(d): handle_cmd(str(d.get("message","")).strip())

@sio.on("neck_command")          # optional direct event
def _cmd(d): handle_cmd(str(d.get("command","")).strip())

# connect with JWT
while True:
    try:
        sio.connect(SERVER, auth={"token":token}); break
    except Exception as e:
        print("[agent] WS connect error:", e); time.sleep(3)

print("[agent] bridge live – Ctrl-C to quit")
try:
    while True: time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    try: ser.close()
    except: ...
    try: sio.disconnect()
    except: ...
    print("[agent] bye")
