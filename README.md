```text
repo-root/
│
├── server/
│   └── signaling_server.py      ← “hub”: brokers every peer ↔ peer message
│
├── client/
│   └── user_interface.py        ← REPL-style CLI that sends commands
│
└── neck/
    └── neck_agent.py            ← tiny daemon on the robot, relays to serial
```

---

# Neck-Signal — distributed control skeleton for your Stewart-platform “neck”

Lightweight enough to run on a Pi Zero, but batteries-included:

* **JWT-secured Socket-IO signalling hub** that auto-exposes itself via LocalTunnel
* Tiny **Python UI client** (REPL) that discovers the hub, retries, auto-relaunches in its own v-env
* **Neck agent** that picks the first available `/dev/ttyUSB*`, speaks the firmware protocol, and mirrors every WebSocket command straight to the hardware

Everything self-bootstraps:
no global `pip install`, no Docker, no Node — the first run of every component builds a **local virtual-env** next to the script, installs its own deps and re-execs.

---

## Quick-start

```bash
# ❶ start the signalling server (needs outbound 443 for the tunnel)
$ cd server && python3 signaling_server.py
#   first run: answer three prompts
#   – CORS origins:            *          (or your domains)
#   – LocalTunnel subdomain:   neck-signal
#   – Shared password:         hunter2   ← remember this!

# ❷ on the robot / MCU host
$ cd neck && python3 neck_agent.py
#   first run asks:
#   – Shared password (must match step ❶)
#   – Hub URL (printed by the server, e.g. https://neck-signal.loca.lt)
#   – UUID for this agent (enter or <return> for random)

# ❸ on your laptop
$ cd client && python3 user_interface.py
#   first run asks for
#   – Hub URL                 (same https://neck-signal.loca.lt)
#   – Shared password         (again hunter2)
#   – UUID for *you*          (<return> for random)
#   Then type `home`, `X50`, `HOME`, …
```

The UI prints what you type, the agent prints what it forwards, and you should see the neck move.

---

## 1  Signalling server (`server/signaling_server.py`)

| behaviour               | notes                                                                                                                                                                                                                                                                                                      |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **auto-env**            | creates `server/venv/` (one-time)                                                                                                                                                                                                                                                                          |
| **first-run prompts**   | CORS origins, tunnel sub-domain, shared password                                                                                                                                                                                                                                                           |
| **persistent config**   | `server/config.json`                                                                                                                                                                                                                                                                                       |
| **ENV**                 | `.env` → **`JWT_SECRET`** & **`PEER_SHARED_SECRET`** (the shared pw)                                                                                                                                                                                                                                       |
| **public URL**          | prints after tunnel comes up; stored as `localtunnel_domain`                                                                                                                                                                                                                                               |
| **HTTP API**            | `GET /` → health <br>`POST /login` → issue JWT <br>  payload `{"uuid":"...", "password":"..."}`                                                                                                                                                                                                            |
| **Socket-IO namespace** | default (`/`)                                                                                                                                                                                                                                                                                              |
| **socket events**       | **client → server**<br>  `connect(auth={'token':JWT})`<br>  `broadcast-message {message:str}`<br>  `peer-message {target:sid, message:str}`<br><br>**server → client**<br>  `existing-peers [...peers]`<br>  `new-peer {id, uuid, roles}`<br>  `peer-disconnect sid`<br>  `peer-message {peerId, message}` |
| **heartbeat**           | hits the public URL every 60 s; restarts if 5× failures                                                                                                                                                                                                                                                    |

---

## 2  User Interface (`client/user_interface.py`)

| behaviour             | notes                                                                                                      |
| --------------------- | ---------------------------------------------------------------------------------------------------------- |
| **auto-env**          | builds `client/int_venv/`                                                                                  |
| **first-run prompts** | hub URL, shared password, UUID                                                                             |
| **persistent config** | `client/config_interface.json`                                                                             |
| **ENV**               | `.env` holds **`PEER_SHARED_SECRET`**                                                                      |
| **CLI flags**         | `-s/--server-url`, `-u/--uuid` override config                                                             |
| **loop**              | 1) polls `/login` until JWT; <br>2) connects via Socket-IO; <br>3) REPL – every line → `broadcast-message` |
| **REPL commands**     | any plain text; e.g. `HOME`, `X50,Y-20`, etc.                                                              |

---

## 3  Neck agent (`neck/neck_agent.py`)

| behaviour             | notes                                                                            |
| --------------------- | -------------------------------------------------------------------------------- |
| **auto-env**          | builds `neck/ag_venv/`                                                           |
| **first-run prompts** | shared password, hub URL, UUID                                                   |
| **serial detection**  | tries `/dev/ttyUSB0 ¹` → `/dev/ttyUSB1` → `/dev/tty0/1` → `COM3/4`; 115 200 baud |
| **startup action**    | once WS connected, immediately **sends `HOME\n`**                                |
| **Socket-IO events**  | listens for `peer-message {"message": …}` or `neck_command {"command": …}`       |
| **serial protocol**   | every command → `ser.write((cmd+"\n").encode())`                                 |
| **ack**               | after writing, emits `neck_ack {"command": cmd}` back to UI                      |

¹ Edit `SER_CANDIDATES` in the script if your adapter appears elsewhere.

---

## Communication protocol

```
POST /login                        # token handshake
{
  "uuid": "50da-3e29-a2f4-eae4",
  "password": "hunter2"
}

# server → {"token":"...JWT...","uuid":"50da-…"}

# WebSocket (Socket-IO, default namespace)
client.connect(auth={"token": "...JWT..."})

broadcast-message {"message": "HOME"}   # UI → server → everyone except UI
peer-message     {"target": <sid>, "message":"X30"}   # directed

neck_agent listens → writes "X30\n" to serial
neck_agent emits  → neck_ack {"command": "X30"}
```

All commands are **verbatim ASCII lines** ending with `\n`.
Firmware accepts:

* `HOME`
* Individual actuator: `1:55,2:60,…`
* Euler / head: `X-700…700,Y…,Z…,H0…70,S0…10,A0…10,R…,P…`
* Quaternion: `Q:w,x,y,z,H..,S..,A..`

---

## Development & troubleshooting

* Delete a component’s `*/venv` or `*/ag_venv` / `*/int_venv` if you ever want a clean reinstall.
* Run the signalling server with `--port 5000 --subdomain mytest` to override saved config.
* `PYTHONUNBUFFERED=1` env-var streams the agent’s prints instantly over SSH.
* The agent prints every line it writes to UART – use a USB-TLL cable + `screen` on another PC to confirm on-wire bytes.


