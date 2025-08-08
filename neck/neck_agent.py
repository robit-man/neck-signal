#!/usr/bin/env python3
"""
neck_agent.py  –  JWT-secured Socket.IO ⇄ Serial bridge + camera streamer

Changes in this version:
• Emits frames ONLY via Socket.IO events ("frame-color", "frame-depth") with bytes payloads.
• Color+Depth captured in the same cycle to keep them temporally aligned (shared seq + timestamp).
• Robust reconnect: on disconnect OR expired token, re-login and reconnect automatically.
• Threads are lifecycle-managed; streaming pauses while disconnected and resumes after reconnect.
"""

###############################################################################
# 0.  ▸▸▸  very light v-env bootstrap  ▸▸▸
###############################################################################
from __future__ import annotations
import os, sys, subprocess, venv, json, secrets, argparse, time, platform, pathlib, io, threading, re
from typing import Optional, Tuple
from urllib.parse import urljoin

ROOT      = pathlib.Path(__file__).resolve().parent
VENV_DIR  = ROOT / "ag_venv"
VENV_PY   = VENV_DIR / ("Scripts" if platform.system() == "Windows" else "bin") / "python"
DEPS      = ["pyserial", "python-socketio[client]", "requests", "pillow"]

def in_venv() -> bool:
    return sys.prefix != sys.base_prefix

if not in_venv():
    if not VENV_DIR.exists():
        print("[agent] creating ag_venv …")
        venv.EnvBuilder(with_pip=True).create(VENV_DIR)
        subprocess.check_call([str(VENV_PY), "-m", "pip", "install",
                               "--quiet", "--upgrade", "pip", *DEPS])
    os.execv(str(VENV_PY), [str(VENV_PY), *sys.argv])

# ensure deps if user nuked them later
missing: list[str] = []
for pkg, mod in [("pyserial", "serial"),
                 ("requests", "requests"),
                 ("python-socketio[client]", "socketio"),
                 ("pillow", "PIL")]:
    try: __import__(mod)
    except ModuleNotFoundError: missing.append(pkg)
if missing:
    print("[agent] installing extra deps:", ", ".join(missing))
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *missing])
    os.execv(sys.executable, [sys.executable, *sys.argv])

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

# defaults
default_cfg = {
    "server_url": "",
    "uuid": "",
    # camera streamer knobs:
    "camera_host": "http://127.0.0.1:8080",
    "color_path": "/camera/rs_color",   # we normalize to 640x360 JPEG
    "depth_path": "/camera/rs_depth",   # we normalize to 640x360 PNG (8-bit)
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
# 3.  JWT login helper
###############################################################################
LOGIN_URL = urljoin(SERVER + "/", "login")

def try_login() -> Optional[str]:
    try:
        r = requests.post(LOGIN_URL, json={"uuid":UUID, "password":PEER_SECRET}, timeout=5)
        if r.status_code == 200:
            j = r.json()
            tok = j.get("token")
            if tok:
                return tok
        print("[agent] /login →", r.status_code, getattr(r, "text", ""))
    except Exception as e:
        print("[agent] /login unreachable:", e)
    return None

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
        m = re.match(r"^([XYZHSARP])(-?\d+(?:\.\d+)?)$", tok.strip())
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
        m = re.match(r"^([XYZHSARP])(-?\d+(?:\.\d+)?)$", tok.strip())
        if m:
            k,val=m.group(1),m.group(2); state[k]=ALLOWED[k][2](val)
    return ",".join(f"{k}{state[k]}" for k in ["X","Y","Z","H","S","A","R","P"])

print("[agent] example full cmd:  X100,Y-50,Z0,H30,S1,A1,R0,P0")
ser.write(b"HOME\n"); print("[agent] → serial  HOME")

###############################################################################
# 5.  Socket.IO client + camera streaming (aligned)
###############################################################################
# We manage reconnect ourselves so we can refresh JWTs.
sio = socketio.Client(reconnection=False, logger=False, engineio_logger=False)

_ws_stop = threading.Event()
_stream_stop = threading.Event()
_connected_lock = threading.Lock()

def handle_cmd(raw:str):
    if not validate(raw):
        print("[agent] ✗ invalid:", raw); return
    full = merge(raw)
    try:
        ser.write((full+"\n").encode())
        print(f"[agent] → serial  {full}")
        if sio.connected:
            sio.emit("neck_ack", {"command":full})
    except Exception as e:
        print("[agent] serial error:", e)

@sio.on("peer-message")
def _msg(d): handle_cmd(str(d.get("message","")).strip())

@sio.on("neck_command")
def _cmd(d): handle_cmd(str(d.get("command","")).strip())

@sio.event
def connect():
    print("[agent] WS connected")
    with _connected_lock:
        _stream_stop.clear()  # let stream run

@sio.event
def disconnect():
    print("[agent] WS disconnected")
    with _connected_lock:
        _stream_stop.set()    # pause streaming emits (fetch continues but no emits)

@sio.event
def connect_error(data):
    # will be handled by our ws loop (we'll re-login and retry)
    print("[agent] connect_error:", data)

# ---- Camera helpers ---------------------------------------------------
def crop_to_aspect(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    tw, th = target_w, target_h
    W, H = img.size
    target_ratio = tw / th
    src_ratio = W / H
    if abs(src_ratio - target_ratio) < 1e-3:
        return img
    if src_ratio > target_ratio:
        new_w = int(H * target_ratio)
        left = (W - new_w) // 2
        return img.crop((left, 0, left + new_w, H))
    else:
        new_h = int(W / target_ratio)
        top = (H - new_h) // 2
        return img.crop((0, top, W, top + new_h))

class CameraStreamer:
    """Single-cycle alignment: fetch color then depth within the same period."""
    def __init__(self, sio_client: socketio.Client, cfg: dict):
        self.sio = sio_client
        self.host = cfg["camera_host"].rstrip("/")
        self.color_url = self.host + cfg["color_path"]
        self.depth_url = self.host + cfg["depth_path"]
        self.hz = max(1, min(60, int(cfg.get("frame_hz", 12))))
        self.period = 1.0 / self.hz
        self._stop = threading.Event()
        self._t = None
        self.seq = 0

    def start(self):
        if self._t and self._t.is_alive(): return
        self._stop.clear()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()
        print(f"[agent] camera streaming loop @ {self.hz} FPS")

    def stop(self):
        self._stop.set()

    def _loop(self):
        session = requests.Session()
        headers = {"Connection": "keep-alive"}
        target_size = (360, 640)  # (W,H) normalized
        while not self._stop.is_set():
            t0 = time.time()
            # fetch color
            color_bytes = None
            depth_bytes = None
            t_color = t_depth = None

            try:
                r = session.get(self.color_url, timeout=1.5, headers=headers)
                if r.status_code == 200 and r.content:
                    with Image.open(io.BytesIO(r.content)) as im:
                        im = im.convert("RGB")
                        im = crop_to_aspect(im, *target_size)
                        im = im.resize(target_size, Image.BICUBIC)
                        buf = io.BytesIO()
                        im.save(buf, format="JPEG", quality=70, optimize=True)
                        color_bytes = buf.getvalue()
                        t_color = time.monotonic()
            except Exception:
                pass

            # fetch depth immediately after color (temporal pairing)
            try:
                r = session.get(self.depth_url, timeout=1.5, headers=headers)
                if r.status_code == 200 and r.content:
                    payload = r.content
                    try:
                        with Image.open(io.BytesIO(payload)) as im:
                            W,H = im.size
                            if (W,H) != target_size:
                                im = im.convert("L").resize(target_size, Image.NEAREST)
                                buf = io.BytesIO()
                                im.save(buf, format="PNG", optimize=True)
                                payload = buf.getvalue()
                        depth_bytes = payload
                        t_depth = time.monotonic()
                    except Exception:
                        # not an image? still treat as payload
                        depth_bytes = payload
                        t_depth = time.monotonic()
            except Exception:
                pass

            # Emit only when connected; keep color→depth order and minimal skew
            if self.sio.connected and not _stream_stop.is_set():
                seq = self.seq = (self.seq + 1) & 0x7fffffff
                # Optional: warn if skew is high
                if t_color and t_depth and abs(t_color - t_depth) > 0.06:
                    # just a note; no structural changes to payload
                    print(f"[agent] warn: color/depth skew {abs(t_color - t_depth)*1000:.0f} ms")

                if color_bytes is not None:
                    self.sio.emit("frame-color", color_bytes)
                if depth_bytes is not None:
                    self.sio.emit("frame-depth", depth_bytes)

            # frame pacing
            dt = time.time() - t0
            if dt < self.period:
                time.sleep(self.period - dt)

camera_streamer = CameraStreamer(sio, cfg)

###############################################################################
# 6.  WS connect/reconnect loop (refresh JWT on demand)
###############################################################################
def ws_loop():
    """Runs forever. If disconnected or token expires, re-login and reconnect."""
    cur_token = None
    backoff = 1.0
    while not _ws_stop.is_set():
        if not sio.connected:
            # (re)login
            tok = try_login()
            if not tok:
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 8.0)
                continue
            cur_token = tok
            backoff = 1.0
            # connect (websocket transport to avoid any polling oddities)
            try:
                sio.connect(
                    SERVER,
                    auth={"token": cur_token},
                    transports=["websocket"],
                    socketio_path="/socket.io",
                    wait_timeout=6,
                )
                # start/ensure streaming
                camera_streamer.start()
            except Exception as e:
                print("[agent] WS connect error:", e)
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 8.0)
                continue
        # when connected, sleep a bit and re-check
        time.sleep(1.0)

###############################################################################
# 7.  Main
###############################################################################
print("[agent] bridge live – Ctrl-C to quit")
t_ws = threading.Thread(target=ws_loop, daemon=True)
t_ws.start()

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    try: _ws_stop.set()
    except: ...
    try: camera_streamer.stop()
    except: ...
    try: ser.close()
    except: ...
    try:
        if sio.connected:
            sio.disconnect()
    except: ...
    print("[agent] bye")
