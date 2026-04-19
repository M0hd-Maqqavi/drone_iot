# Drone IoT Monitor
### A CoAP-Based IoT Telemetry and Control System for the DJI Tello Drone

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [How It Differs From the Standard Tello App](#2-how-it-differs-from-the-standard-tello-app)
3. [System Architecture](#3-system-architecture)
4. [Folder Structure](#4-folder-structure)
5. [Prerequisites](#5-prerequisites)
6. [Environment Setup](#6-environment-setup)
7. [Running on One Machine](#7-running-on-one-machine)
8. [Running on Two Separate Machines (USB Ethernet — macOS)](#8-running-on-two-separate-machines-usb-ethernet--macos)
9. [Keyboard Controls](#9-keyboard-controls)
10. [Dashboard Features](#10-dashboard-features)
11. [Where Files Are Saved](#11-where-files-are-saved)

---

## 1. Project Overview

**Drone IoT Monitor** is a hardware-based IoT project built for the *Introduction to IoT Systems* course (1501452) at the University of Sharjah. It reimplements drone communication and monitoring as a proper IoT system using the **CoAP protocol**, **AES-128 mutual authentication**, and a **live web dashboard**.

The system uses a **DJI Tello (TLW004)** drone as the IoT sensor node. The drone collects telemetry data (orientation, altitude, temperature, acceleration, battery, velocity) and streams live video. A gateway layer translates this into CoAP resources, and an authenticated client consumes them via a web dashboard.

**Key features:**
- 4-step AES-128 mutual authentication handshake (based on Week 8 lecture material)
- 7 observable CoAP telemetry resources with push notifications (Observe option)
- Bidirectional flight commands over CoAP (takeoff, land, move, rotate, emergency)
- Live MJPEG video feed served over HTTP
- Image capture and video recording
- Real-time alert system (low battery, altitude exceeded, drone not level)
- Live charts: battery, altitude, temperature, acceleration
- Web dashboard (HTML/CSS/JavaScript with Chart.js)
- Designed to run on one machine or two separate machines with no code changes

---

## 2. How It Differs From the Standard Tello App

| Feature | Official Tello App | This System |
|---|---|---|
| Protocol | Raw proprietary UDP (Tello SDK) | CoAP over UDP (standard IoT protocol) |
| Security | None — any device on the Wi-Fi can send commands | 4-step AES-128 mutual authentication |
| Architecture | Monolithic app (no layers) | Proper IoT: Device → Gateway → Client |
| Data | Displayed and discarded | Logged, stored, served as CoAP resources |
| Communication model | Drone pushes, app listens | CoAP Request/Response + Observe (pub/sub) |
| Extensibility | Closed, proprietary | Any CoAP client can connect and query |
| Alerts | None | CON messages on battery/altitude/tilt thresholds |
| Dashboard | Mobile joystick UI | Live web dashboard with charts and video |

**Why CoAP between gateway and client (not between drone and gateway)?**
The Tello's firmware is proprietary and fixed — it only speaks Tello SDK over UDP. We cannot modify it. The gateway translates SDK telemetry into CoAP resources, which is exactly what IoT gateways do in real deployments. The client never touches the SDK; it only speaks CoAP.

---

## 3. System Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                        APPLICATION LAYER                             │
│   coap/client.py + web/app.py + web/templates/index.html            │
│   - CoAP Observe subscriptions (7 resources)                        │
│   - Web dashboard (SSE telemetry, Chart.js, keyboard controls)      │
│   - Sends commands: browser → Flask → CoAP → server → drone         │
└───────────────────────────┬──────────────────────────────────────────┘
                            │ CoAP over UDP (port 5683)
                            │ 4-step AES-128 mutual auth
                            │ HTTP/MJPEG video (port 8080)
┌───────────────────────────┴──────────────────────────────────────────┐
│                        GATEWAY LAYER                                 │
│   coap/server.py + tello/drone.py + shared/state.py                 │
│   - Receives Tello SDK telemetry (UDP port 8890)                    │
│   - Exposes 7 CoAP resources (battery, height, temperature,         │
│     orientation, velocity, acceleration, tof)                       │
│   - Enforces authentication on all resource access                  │
│   - Forwards SDK commands to drone (UDP port 8889)                  │
│   video/stream.py                                                   │
│   - Decodes H.264 video (UDP port 11111) → MJPEG (HTTP port 8080)  │
└───────────────────────────┬──────────────────────────────────────────┘
                            │ Tello SDK over UDP
                            │ Port 8889: commands (gateway → drone)
                            │ Port 8890: telemetry (drone → gateway)
                            │ Port 11111: H.264 video (drone → gateway)
┌───────────────────────────┴──────────────────────────────────────────┐
│                        DEVICE LAYER                                  │
│   DJI Tello TLW004                                                   │
│   - IMU: pitch, roll, yaw, acceleration (agx, agy, agz)            │
│   - Barometric altimeter: height, baro                              │
│   - Time-of-Flight sensor: tof                                      │
│   - Thermometer: templ, temph                                       │
│   - Battery monitor: bat                                            │
│   - Camera: H.264 video stream                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### Authentication Flow (Week 8 lecture — AES-128 mutual auth)

```
CLIENT                                        SERVER
  │                                              │
  │── POST /auth/init  (Device ID) ─────────────►│  Step 1: Session initiation
  │◄── 2.01 Created ────────────────────────────│
  │                                              │
  │── GET /auth/challenge ───────────────────────►│  Step 2: Server sends AES{λi,(ψ|ηserver)}
  │◄── 2.05 Content (encrypted challenge) ───────│
  │                                              │
  │── POST /auth/verify (client response) ───────►│  Step 3: Client proves identity
  │◄── 2.04 Changed ────────────────────────────│
  │                                              │
  │── GET /auth/confirm ─────────────────────────►│  Step 4: Server proves identity
  │◄── 2.05 Content (server proof) ──────────────│
  │                                              │
  │  ✅ μkey shared — all subsequent data        │
  │     encrypted with AES{μkey, data}           │
```

---

## 4. Folder Structure

```
drone_iot/
│
├── auth/
│   ├── __init__.py
│   └── handshake.py          # 4-step AES-128 mutual auth (AuthServer + AuthClient)
│
├── coap/
│   ├── __init__.py
│   ├── server.py             # CoAP server: 7 telemetry resources + 4 command resources
│   └── client.py             # CoAP client: auth handshake + Observe subscriptions
│
├── shared/
│   ├── __init__.py
│   └── state.py              # Thread-safe in-memory telemetry store (server side)
│
├── tello/
│   ├── __init__.py
│   └── drone.py              # TelloBridge: SDK commands + telemetry listener
│
├── video/
│   ├── __init__.py
│   └── stream.py             # MJPEG server + image capture + video recording
│
├── web/
│   ├── __init__.py
│   ├── app.py                # Flask: dashboard server + SSE telemetry + command proxy
│   ├── templates/
│   │   └── index.html        # Web dashboard (HTML + CSS + JavaScript + Chart.js)
│   └── static/
│       └── chart.min.js      # Chart.js (local copy — works without internet)
│
├── captures/                 # Captured images (gitignored, saved locally)
├── recordings/               # Recorded videos (gitignored, saved locally)
│
├── config.py                 # All constants, ports, thresholds (reads from .env)
├── main.py                   # Entry point — starts all modules in correct order
├── client_main.py            # Entry point for CLIENT machine in two-machine setup
├── .env                      # Secrets and host config (gitignored — never committed)
├── .env.example              # Template showing required .env variables
├── requirements.txt          # All Python dependencies
├── flight_log.txt            # Runtime log (gitignored, generated each session)
└── .gitignore
```

### What each key file does

**`config.py`** — Single source of truth for all configuration. Every port, threshold, and secret is defined here. All values can be overridden via `.env` — no code changes needed to switch between one-machine and two-machine deployments.

**`auth/handshake.py`** — Implements the 4-step mutual authentication scheme from the Week 8 lecture. `AuthServer` handles the server side; `AuthClient` handles the client side. Uses AES-128-CBC encryption and XOR-based session key hiding (`ψ = λi ⊕ μkey`).

**`tello/drone.py`** — The only file that speaks Tello SDK. Manages two UDP sockets: one for sending commands (port 8889), one for receiving telemetry broadcasts (port 8890). Higher-level methods: `takeoff()`, `land()`, `emergency()`, `move()`, `rotate()`.

**`shared/state.py`** — Thread-safe telemetry store on the server/gateway side. Written by `drone.py` (telemetry thread), read by `coap/server.py` (asyncio thread). Uses `threading.Lock` and `deque(maxlen=100)` ring buffers.

**`coap/server.py`** — Exposes drone telemetry as 7 observable CoAP resources and 4 command resources. Enforces authentication on every request. Auth resources handle the 4-step handshake.

**`coap/client.py`** — Client-side telemetry store (`ClientState`) and CoAP client (`DroneCoAPClient`). Runs the auth handshake, subscribes to all 7 resources via Observe, sends flight commands as CON POST requests.

**`video/stream.py`** — Opens Tello's H.264 stream via OpenCV, decodes frames, serves them as MJPEG on port 8080. Also handles image capture (`/capture`) and video recording (`/record/start`, `/record/stop`).

**`web/app.py`** — Flask web server on the client side. Serves the dashboard HTML, pushes telemetry to the browser via SSE every 500ms, proxies flight commands from browser to CoAP client, handles `/shutdown` for clean QQ exit.

**`web/templates/index.html`** — Complete web dashboard. Live video feed (MJPEG), 4 live Chart.js charts, alert bar, status bar, media controls, keyboard controls. All in a single file.

---

## 5. Prerequisites

### Hardware
- DJI Tello drone (TLW004 or compatible)
- MacBook (or any machine that can connect to Tello Wi-Fi)
- For two-machine setup: USB Ethernet adapter

### Software
- Python 3.12+
- Miniconda or Anaconda (recommended for environment management)
- A modern browser (Chrome, Edge, Firefox, Safari)

---

## 6. Environment Setup

### Step 1 — Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/drone-iot.git
cd drone-iot
```

### Step 2 — Create a conda virtual environment

```bash
conda create -n DRONE-IOT python=3.12
conda activate DRONE-IOT
```

### Step 3 — Install dependencies

```bash
pip install -r requirements.txt
```

If `requirements.txt` is not present, install manually:

```bash
pip install djitellopy aiocoap pycryptodome matplotlib flask opencv-python requests python-dotenv
```

### Step 4 — Set up your `.env` file

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

Open `.env` and fill in:

```
# Pre-shared secret for AES-128 authentication
# MUST be exactly 16 characters — no more, no less
PRESHARED_SECRET=YourExact16Chars

# Unique identifier for this client device
DEVICE_ID=drone-monitor-client-01

# Web dashboard port (change if 5000 is in use — macOS uses 5000 for AirPlay)
WEB_PORT=5001
```

**Important notes:**
- `PRESHARED_SECRET` must be **exactly 16 characters**. AES-128 requires a 128-bit (16-byte) key. A shorter or longer value will cause a `ValueError` at runtime.
- The `.env` file is gitignored and never committed. Share the actual secret with teammates privately (not via GitHub).
- On macOS, port 5000 is used by AirPlay Receiver. Either set `WEB_PORT=5001` in `.env` or disable AirPlay Receiver in System Settings → General → AirDrop & Handoff.

### Step 5 — Verify the secret length

```bash
python -c "
import os
from dotenv import load_dotenv
load_dotenv()
s = os.getenv('PRESHARED_SECRET')
print(f'Length: {len(s)} — {\"OK\" if len(s)==16 else \"WRONG — must be 16 chars\"}')"
```

---

## 7. Running on One Machine

### Step 1 — Connect to drone Wi-Fi

1. Power on the Tello drone. Wait for the LED to stop flashing (solid yellow = ready).
2. On your Mac: **System Settings → Wi-Fi** → connect to `TELLO-XXXXXX`
3. You will lose internet access while connected — this is normal.

### Step 2 — Activate environment

```bash
conda activate DRONE-IOT
cd drone-iot
```

### Step 3 — Run

```bash
python main.py
```

### Step 4 — What to expect in the terminal

```
HH:MM:SS | INFO  | tello.drone  | SDK mode: OK — drone ready.
HH:MM:SS | INFO  | tello.drone  | Telemetry thread running on port 8890.
HH:MM:SS | INFO  | __main__     | Battery: 87%
HH:MM:SS | INFO  | __main__     | Video stream enabled.
HH:MM:SS | INFO  | auth.handshake | Step 1: Sending initiation...
HH:MM:SS | INFO  | auth.handshake | Step 2: Challenge decrypted, μkey recovered
HH:MM:SS | INFO  | auth.handshake | Step 3: Client verified successfully
HH:MM:SS | INFO  | auth.handshake | Step 4: Server verified. Mutual authentication complete.
HH:MM:SS | INFO  | __main__     | Opening browser: http://localhost:5001
HH:MM:SS | INFO  | __main__     | System running. Press Ctrl+C to shut down.
```

The browser opens automatically at `http://localhost:5001`. The dashboard shows live telemetry within 2–3 seconds.

### Step 5 — Shut down

Press `Q` twice on the dashboard — the drone lands automatically and the entire Python process shuts down cleanly. Alternatively, press `Ctrl+C` in the terminal.

---

## 8. Running on Two Separate Machines (USB Ethernet — macOS)

This setup uses a Mac as the **server/gateway** (connected to the drone) and a Windows laptop as the **client** (running the dashboard browser). A USB Ethernet adapter creates a direct wired LAN between the two machines — this avoids the Wi-Fi conflict where the Mac would need to be on both the Tello Wi-Fi and the home network simultaneously.

### The networking problem

The Mac must connect to the Tello's Wi-Fi to control the drone. But the client machine needs to reach the Mac over a network. macOS cannot share a Wi-Fi connection over Wi-Fi, and the Mac cannot be on two Wi-Fi networks simultaneously. The solution is a **direct wired connection** via USB Ethernet.

```
[Tello Wi-Fi] ←──── [Mac Wi-Fi]
                     [Mac USB Ethernet] ────► [Windows Ethernet port]
```

### Hardware needed

- USB-C to Ethernet adapter for the Mac
- Ethernet cable
- Ethernet port on the Windows laptop (or a USB to Ethernet adapter for Windows too)

---

### Server setup (Mac)

**Step 1** — Connect Mac to Tello Wi-Fi (as usual).

**Step 2** — Connect Mac to Windows via Ethernet cable through the USB adapter.

**Step 3** — Find the Mac's IP address on the Ethernet interface:

```bash
ifconfig en3 | grep "inet "
# Replace en3 with your actual USB Ethernet interface name
# Run: ifconfig | grep -A 4 "en" to find it
```

Note the IP, e.g. `169.254.x.x` (link-local) or `192.168.x.x` if you configured static IPs.

**Step 4** — Update `.env` on Mac:

```
PRESHARED_SECRET=YourExact16Chars
DEVICE_ID=drone-monitor-client-01
COAP_HOST=0.0.0.0
MJPEG_HOST=0.0.0.0
WEB_PORT=5001
```

Setting `COAP_HOST=0.0.0.0` makes the CoAP server bind to all interfaces — it will accept connections from the Windows machine over Ethernet.

**Step 5** — Run the server:

```bash
conda activate DRONE-IOT
python main.py
```

The browser will open on the Mac too — you can ignore it or close it.

---

### Client setup (Windows)

**Step 1** — Install Python 3.12 from python.org. During install, check "Add Python to PATH".

**Step 2** — Copy these files/folders from the Mac to the Windows machine (via USB drive or shared folder):

```
auth/
coap/client.py
coap/__init__.py
shared/state.py
shared/__init__.py
web/
config.py
client_main.py
requirements.txt
```

These files are NOT needed on Windows:
- `tello/` — drone SDK runs on the server only
- `video/stream.py` — video capture runs on the server only
- `main.py` — replaced by `client_main.py`

**Step 3** — Install dependencies on Windows:

```bash
pip install aiocoap pycryptodome flask requests python-dotenv
```

**Step 4** — Create `.env` on Windows, pointing to the Mac's Ethernet IP:

```
PRESHARED_SECRET=YourExact16Chars
DEVICE_ID=drone-monitor-client-01
COAP_HOST=169.254.x.x        # Mac's Ethernet IP address
MJPEG_HOST=169.254.x.x       # same Mac IP
WEB_PORT=5001
```

The `PRESHARED_SECRET` and `DEVICE_ID` must be identical to the Mac's `.env`.

**Step 5** — Run the client:

```bash
python client_main.py
```

The browser opens automatically on Windows at `http://localhost:5001`. It connects to the CoAP server on the Mac, performs the auth handshake, and starts receiving telemetry. The video feed is pulled directly from the Mac's MJPEG server at `http://MAC_IP:8080/video_feed`.

---

### Two-machine data flow

```
[Tello Drone]
      │ UDP (Tello SDK)
      ▼
[MAC — SERVER/GATEWAY]                    [WINDOWS — CLIENT]
  tello/drone.py  ← telemetry             coap/client.py
  shared/state.py ← stores data      ←── auth handshake + Observe
  coap/server.py  → CoAP resources   ───► receives telemetry via CoAP
  video/stream.py → MJPEG port 8080       web/app.py → Flask SSE
                                               │
                                          [Browser on Windows]
                                           localhost:5001
                                           video: http://MAC_IP:8080
```

---

## 9. Keyboard Controls

The browser window must be focused (clicked) to receive keyboard input.

| Key | Action |
|---|---|
| `T` | Takeoff |
| `L` | Land |
| `W` | Move up 30cm |
| `S` | Move down 30cm |
| `A` | Rotate counterclockwise 20° |
| `D` | Rotate clockwise 20° |
| `↑` Arrow | Move forward 50cm |
| `↓` Arrow | Move backward 50cm |
| `←` Arrow | Move left 50cm |
| `→` Arrow | Move right 50cm |
| `E` `E` | Emergency stop (double press) — cuts all motors immediately |
| `Q` `Q` | Quit (double press) — lands drone, shuts down system |

---

## 10. Dashboard Features

- **Alert bar** — Red banner at the top. Shows warnings for: low battery (≤20%), altitude exceeded (>200cm), drone not level (pitch or roll >10° while on ground).
- **Live video feed** — MJPEG stream from the drone's camera, centre of the dashboard.
- **📷 Capture Image** — Saves current frame as a timestamped JPEG.
- **🎥 Start/Stop Recording** — Records video to a timestamped MP4. A blinking REC indicator shows while recording.
- **Status bar** — Live pitch, roll, yaw, battery, height, ToF, velocity.
- **Last command** — Shows the most recently executed command.
- **4 live charts** — Battery (%), Altitude (cm), Temperature (°C), Acceleration (cm/s²). Updated every 500ms with 100-sample rolling history.

---

## 11. Where Files Are Saved

All captured images and recordings are saved on the **server/gateway machine** (the Mac connected to the drone), in the project root:

```
drone_iot/
├── captures/
│   └── capture_20260419_221547.jpg
└── recordings/
    └── recording_20260419_221832.mp4
```

These folders are listed in `.gitignore` and are never pushed to GitHub. In a two-machine setup, the files save on the Mac regardless of which machine triggered the capture — because `video/stream.py` runs on the Mac.