#!/usr/bin/env python3
"""
neck_agent.py  –  Socket.IO ⇄ Serial bridge + camera streamer (color+depth) with JWT
────────────────────────────────────────────────────────────────────────────────────
 • Creates / reuses  ag_venv/
 • Persists signalling-server URL & UUID in config.json
 • First run asks for the shared password (so it matches the UI)
 • Streams camera frames:
     - color: 1280x800 → resized & center-cropped to 640x360 → JPEG
     - depth: 640x360  → mapped 16/12 bit → 8-bit → PNG
 • Emits Socket.IO binary events: 'frame-color' and 'frame-depth'
"""

from __future__ import annotations
import os, sys, json, secrets, argparse, time, subprocess, pathlib, platform, venv, io, threading
from typing import Optional
from urllib.parse import urljoin

ROOT       = pathlib.Path(__file__).resolve().parent
VENV_DIR   = ROOT / "ag_venv"
VENV_PY    = VENV_DIR / ("Scripts" if platform.system() == "Windows" else "bin") / "python"
CFG_PATH   = ROOT / "config.json"
ENV_PATH   = ROOT / ".env"

DEPS = ["pyserial", "python-socketio[client]", "requests", "Pillow", "numpy"]

# ───────── venv bootstrap ─────────
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
        # ensure deps inside venv
        subprocess.check_call([str(VENV_PY), "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
        subprocess.check_call([str(VENV_PY), "-m", "pip", "install", "--quiet", *DEPS])
    os.execv(str(VENV_PY), [str(VENV_PY), *sys.argv])

# ───────── .env ─────────
if not ENV_PATH.exists():
    print("[agent] first run – need shared password with signalling server")
    pw = input("Shared password (same one you used in user_interface, blank=random):\n> ").strip()
    shared_pw = pw or secrets.token_urlsafe(16)
    ENV_PATH.write_text(
        f"PEER_SHARED_SECRET={shared_pw}\n"
        f"JWT_SECRET={secrets.token_urlsafe(32)}\n"
    )
    print("[agent] wrote .env")
for line in ENV_PATH.read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())
PEER_SECRET = os.environ["PEER_SHARED_SECRET"]

# ───────── config (server URL, UUID, camera URLs) ─────────
cfg: dict[str, str] = json.loads(CFG_PATH.read_text()) if CFG_PATH.exists() else {}
ap = argparse.ArgumentParser(description="neck_agent – Serial ↔ Socket.IO bridge + camera")
ap.add_argument("-s", "--server-url", help="signalling server base URL")
ap.add_argument("-u", "--uuid",       help="this agent's UUID")
ap.add_argument("--color-url",        help="color frame URL (default http://127.0.0.1:8080/camera/rs_color)")
ap.add_argument("--depth-url",        help="depth frame URL (default http://127.0.0.1:8080/camera/rs_depth)")
cli = ap.parse_args()

def ask(prompt: str) -> str: return input(prompt).strip()
server_url = (cli.server_url or cfg.get("server_url") or ask("Signalling server URL:\n> ")).rstrip("/")
agent_uuid = (cli.uuid       or cfg.get("uuid")       or ask("Agent UUID (blank=random):\n> ")
              or "-".join(secrets.token_hex(2) for _ in range(4)))
color_url  = (cli.color_url or cfg.get("color_url") or "http://127.0.0.1:8080/camera/rs_color")
depth_url  = (cli.depth_url or cfg.get("depth_url") or "http://127.0.0.1:8080/camera/rs_depth")

cfg.update(server_url=server_url, uuid=agent_uuid, color_url=color_url, depth_url=depth_url)
CFG_PATH.write_text(json.dumps(cfg, indent=4))
print(f"[agent] config  →  server={server_url}   uuid={agent_uuid}")
print(f"[agent] streams →  color={color_url}  depth={depth_url}")

# ───────── heavy imports ─────────
import requests, socketio, serial              # type: ignore
import numpy as np
from PIL import Image

# ───────── login loop ─────────
LOGIN_URL = urljoin(server_url + "/", "login")
def try_login() -> Optional[str]:
    try:
        r = requests.post(LOGIN_URL, json={"uuid": agent_uuid, "password": PEER_SECRET}, timeout=4)
        if r.status_code == 200 and "token" in r.json():
            return r.json()["token"]
        if r.status_code == 401: print("[agent] /login → bad credentials (server not aware yet)")
    except Exception as e:
        print(f"[agent] /login unreachable: {e}")
    return None

print(f"[agent] contacting {LOGIN_URL}")
jwt_token: Optional[str] = None
while jwt_token is None:
    jwt_token = try_login()
    if jwt_token is None: time.sleep(3)
print("[agent] JWT acquired ✓")

# ───────── open Serial (retry) ─────────
SER_CANDIDATES = ["/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/tty0", "/dev/tty1", "COM3", "COM4"]
ser: Optional[serial.Serial] = None
while ser is None:
    for port in SER_CANDIDATES:
        try:
            ser = serial.Serial(port, 115200, timeout=1)
            print("[agent] opened serial port:", port)
            break
        except Exception:
            ser = None
    if ser is None:
        print("[agent] no serial port – retry in 3 s")
        time.sleep(3)

# ───────── Socket.IO bridge ─────────
sio = socketio.Client(reconnection=True, reconnection_attempts=0)  # infinite tries

@sio.event
def connect():    print("[agent] WS connected")
@sio.event
def disconnect(): print("[agent] WS disconnected")

@sio.on("peer-message")
def _peer_msg(data):
    # accept generic peer messages too; route "neck" commands to serial
    msg = str(data.get("message", "")).strip()
    if msg:
        ser.write((msg + "\n").encode())
        print(f"[agent] → serial  {msg}")
        sio.emit("neck_ack", {"command": msg})

def _connect_ws():
    while True:
        try:
            sio.connect(server_url, auth={"token": jwt_token})
            break
        except Exception as e:
            print(f"[agent] WS connect error: {e}")
            time.sleep(3)
_connect_ws()

# ───────── camera processing helpers ─────────
TARGET_W, TARGET_H = 640, 360

def resize_center_crop_to(img: Image.Image, tw: int, th: int) -> Image.Image:
    # scale so both dims >= target, then center crop
    w, h = img.width, img.height
    scale = max(tw / w, th / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    img = img.resize((nw, nh), resample=Image.LANCZOS)
    left = (nw - tw) // 2
    top  = (nh - th) // 2
    return img.crop((left, top, left + tw, top + th))

def encode_jpeg(img: Image.Image, q: int = 72) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=q, optimize=False)
    return buf.getvalue()

def encode_png(gray8: np.ndarray) -> bytes:
    img = Image.fromarray(gray8, mode="L")
    buf = io.BytesIO()
    # PNG level 3 for speed
    img.save(buf, format="PNG", optimize=False, compress_level=3)
    return buf.getvalue()

def depth_to_8bit(arr: np.ndarray) -> np.ndarray:
    # robust 2-98 percentile stretch to 0..255
    a = arr.astype(np.float32, copy=False)
    lo, hi = np.percentile(a, (2.0, 98.0))
    if hi <= lo: hi = lo + 1.0
    a = np.clip((a - lo) / (hi - lo), 0.0, 1.0)
    return (a * 255.0 + 0.5).astype(np.uint8)

# ───────── camera loop (background) ─────────
_cam_stop = threading.Event()

def camera_loop():
    sess = requests.Session()
    sess.headers.update({"Connection": "keep-alive"})
    last_warn = 0.0
    while not _cam_stop.is_set():
        t0 = time.perf_counter()
        try:
            # fetch frames
            rc = sess.get(color_url, timeout=1.0)
            rd = sess.get(depth_url, timeout=1.0)
            if rc.status_code != 200 or rd.status_code != 200:
                raise RuntimeError(f"HTTP {rc.status_code}/{rd.status_code}")

            # COLOR → resize + crop → JPEG
            col_img = Image.open(io.BytesIO(rc.content)).convert("RGB")
            col_img = resize_center_crop_to(col_img, TARGET_W, TARGET_H)
            col_jpg = encode_jpeg(col_img, q=72)

            # DEPTH → load → 8-bit → PNG
            # Accept common formats: 8/16-bit PNG, JPEG heatmap, etc.
            try:
                dimg = Image.open(io.BytesIO(rd.content))
                if dimg.mode in ("I;16", "I;16B", "I;16L"):
                    arr = np.array(dimg, dtype=np.uint16)
                    gray8 = depth_to_8bit(arr)
                else:
                    # If already 8-bit (L or RGB heatmap), convert to L
                    dimg = dimg.convert("L")
                    gray8 = np.array(dimg, dtype=np.uint8)
                # ensure size is 640x360 (resize if needed)
                if dimg.size != (TARGET_W, TARGET_H):
                    dimg = Image.fromarray(gray8, mode="L").resize((TARGET_W, TARGET_H), Image.NEAREST)
                    gray8 = np.array(dimg, dtype=np.uint8)
                depth_png = encode_png(gray8)
            except Exception:
                # fallback: send as-is JPEG (fast path)
                depth_png = rd.content

            # emit as binary buffers (Socket.IO handles binary natively)
            sio.emit("frame-color", col_jpg)
            sio.emit("frame-depth", depth_png)

        except Exception as e:
            now = time.time()
            if now - last_warn > 2.0:
                print(f"[agent] camera loop warning: {e}")
                last_warn = now

        # aim ~10–15 fps (adjust as needed)
        dt = time.perf_counter() - t0
        time.sleep(max(0.0, 0.07 - dt))

cam_thr = threading.Thread(target=camera_loop, daemon=True)
cam_thr.start()

print("[agent] bridge + camera streaming live – Ctrl-C to quit")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    _cam_stop.set()
    try: cam_thr.join(timeout=1.0)
    except: ...
    try: ser.close()
    except: ...
    try: sio.disconnect()
    except: ...
    print("[agent] bye")
