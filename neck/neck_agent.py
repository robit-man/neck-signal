#!/usr/bin/env python3
"""
neck_agent.py  –  JWT-secured Socket.IO ⇄ Serial bridge + camera streamer

• boots / re-uses  ag_venv/  (installs pyserial, requests, python-socketio, pillow)
• remembers  server-URL  &  UUID  in  config.json
• first run asks for the shared password (.env → PEER_SHARED_SECRET)
• on serial-connect:
      – prints an example of a *full* command
      – sends HOME once
• every incoming command from the UI:
      – "home"  →  literal HOME
      – partial  →  merged into full X,Y,Z,H,S,A,R,P and validated
• camera streamer:
      – polls  http://127.0.0.1:8080/camera/rs_color  (default)  →  downscale+crop to 640×360 → JPEG
      – polls  http://127.0.0.1:8080/camera/rs_depth  (default)  →  forward binary PNG (or fix to 640×360)
      – emits Socket.IO binary events:  "frame-color"  and  "frame-depth"
"""

###############################################################################
# 0.  ▸▸▸  very light v-env bootstrap  ▸▸▸
###############################################################################
from __future__ import annotations
import os, sys, subprocess, venv, json, secrets, argparse, time, platform, pathlib, io, threading
from typing import Optional, Tuple
from urllib.parse import urljoin

ROOT      = pathlib.Path(__file__).resolve().parent
VENV_DIR  = ROOT / "ag_venv"
VENV_PY   = VENV_DIR / ("Scripts" if platform.system() == "Windows" else "bin") / "python"
DEPS      = ["pyserial", "python-socketio[client]", "requests", "pillow"]

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
                 ("python-socketio[client]", "socketio"),
                 ("pillow", "PIL")]:
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

# defaults (created into config.json if missing)
default_cfg = {
    "server_url": "",
    "uuid": "",
    # camera streamer knobs:
    "camera_host": "http://127.0.0.1:8080",
    "color_path": "/camera/rs_color",   # expected 1280x800 JPEG/PNG (we crop/resize to 640x360)
    "depth_path": "/camera/rs_depth",   # expected 640x360 PNG (forward as-is)
    "frame_hz": 12                      # ~12 FPS by default
}
cfg: dict = json.loads(CFG_PATH.read_text()) if CFG_PATH.exists() else {}
for k,v in default_cfg.items():
    cfg.setdefault(k, v)

ap = argparse.ArgumentParser()
ap.add_argument("-s","--server-url")
ap.add_argument("-u","--uuid")
ap.add_argument("--frame-hz", type=int, help="camera streaming fps (default 12)")
ap.add_argument("--camera-host", help="base URL for camera HTTP endpoints (default http://127.0.0.1:8080)")
ap.add_argument("--color-path", help="path for color endpoint (default /camera/rs_color)")
ap.add_argument("--depth-path", help="path for depth endpoint (default /camera/rs_depth)")
cli = ap.parse_args()

def ask(t:str)->str: return input(t).strip()

SERVER = (cli.server_url or cfg.get("server_url") or ask("Server URL:\n> ")).rstrip("/")
UUID   = (cli.uuid or cfg.get("uuid") or ask("Agent UUID [blank=random]:\n> ")
          or "-".join(secrets.token_hex(2) for _ in range(4)))

if cli.frame_hz:     cfg["frame_hz"] = max(1, min(60, cli.frame_hz))
if cli.camera_host:  cfg["camera_host"] = cli.camera_host.rstrip("/")
if cli.color_path:   cfg["color_path"] = cli.color_path
if cli.depth_path:   cfg["depth_path"] = cli.depth_path

cfg.update(server_url=SERVER, uuid=UUID)
CFG_PATH.write_text(json.dumps(cfg, indent=4))

print(f"[agent]   server={SERVER}\n          uuid={UUID}")
print(f"[agent]   camera_host={cfg['camera_host']}  frame_hz={cfg['frame_hz']}")

###############################################################################
# 2.  heavy imports  (safe inside v-env)
###############################################################################
import requests, socketio, serial        # type: ignore
from PIL import Image

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
# 5.  Socket.IO bridge (peer-message → serial)  + camera streaming
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

# ────────────────────────────────────────────────────────────────────
# 5.b  Camera streaming worker
# ────────────────────────────────────────────────────────────────────
def crop_to_aspect(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Center-crop PIL image to target aspect before resize."""
    tw, th = target_w, target_h
    W, H = img.size
    target_ratio = tw / th
    src_ratio = W / H
    if abs(src_ratio - target_ratio) < 1e-3:
        return img
    if src_ratio > target_ratio:
        # too wide → crop width
        new_w = int(H * target_ratio)
        left = (W - new_w) // 2
        return img.crop((left, 0, left + new_w, H))
    else:
        # too tall → crop height
        new_h = int(W / target_ratio)
        top = (H - new_h) // 2
        return img.crop((0, top, W, top + new_h))

class CameraStreamer:
    def __init__(self, sio_client: socketio.Client, cfg: dict):
        self.sio = sio_client
        self.host = cfg["camera_host"].rstrip("/")
        self.color_url = self.host + cfg["color_path"]
        self.depth_url = self.host + cfg["depth_path"]
        self.hz = max(1, min(60, int(cfg.get("frame_hz", 12))))
        self.period = 1.0 / self.hz
        self._stop = threading.Event()
        self._t_color = None
        self._t_depth = None

    def start(self):
        self._stop.clear()
        self._t_color = threading.Thread(target=self._loop_color, daemon=True)
        self._t_depth = threading.Thread(target=self._loop_depth, daemon=True)
        self._t_color.start()
        self._t_depth.start()
        print(f"[agent] camera streaming started @ {self.hz} FPS")

    def stop(self):
        self._stop.set()

    def _loop_color(self):
        session = requests.Session()
        target_size = (360, 640)
        while not self._stop.is_set():
            t0 = time.time()
            try:
                r = session.get(self.color_url, timeout=1.5)
                if r.status_code == 200 and r.content:
                    # open with PIL
                    with Image.open(io.BytesIO(r.content)) as im:
                        im = im.convert("RGB")
                        im = crop_to_aspect(im, *target_size)
                        im = im.resize(target_size, Image.BICUBIC)
                        # JPEG encode
                        buf = io.BytesIO()
                        im.save(buf, format="JPEG", quality=70, optimize=True)
                        data = buf.getvalue()
                        # emit as binary
                        if self.sio.connected:
                            self.sio.emit("frame-color", data)
                # else: ignore quietly to keep loop real-time
            except Exception as e:
                # keep running; transient failures are OK
                # print(f"[agent] color fetch error: {e}")
                pass
            # frame pacing
            dt = time.time() - t0
            if dt < self.period:
                time.sleep(self.period - dt)

    def _loop_depth(self):
        session = requests.Session()
        target_size = (360, 640)
        while not self._stop.is_set():
            t0 = time.time()
            try:
                r = session.get(self.depth_url, timeout=1.5)
                if r.status_code == 200 and r.content:
                    payload = r.content
                    # If the depth image is already 640x360 PNG, forward raw.
                    # Otherwise, attempt to normalize to 640x360 8-bit PNG.
                    do_forward = True
                    try:
                        with Image.open(io.BytesIO(payload)) as im:
                            W,H = im.size
                            if (W,H) != target_size:
                                do_forward = False
                                im = im.convert("L").resize(target_size, Image.NEAREST)
                                buf = io.BytesIO()
                                im.save(buf, format="PNG", optimize=True)
                                payload = buf.getvalue()
                    except Exception:
                        # if not an image, just forward raw
                        pass

                    if self.sio.connected:
                        self.sio.emit("frame-depth", payload)
            except Exception as e:
                # print(f"[agent] depth fetch error: {e}")
                pass
            dt = time.time() - t0
            if dt < self.period:
                time.sleep(self.period - dt)

camera_streamer = CameraStreamer(sio, cfg)

# connect with JWT, then start streaming
while True:
    try:
        sio.connect(SERVER, auth={"token":token})
        break
    except Exception as e:
        print("[agent] WS connect error:", e); time.sleep(3)

# start camera streaming once connected
camera_streamer.start()

print("[agent] bridge live – Ctrl-C to quit")
try:
    while True: time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    try: camera_streamer.stop()
    except: ...
    try: ser.close()
    except: ...
    try: sio.disconnect()
    except: ...
    print("[agent] bye")
