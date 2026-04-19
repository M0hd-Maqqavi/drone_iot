# config.py
"""
Central configuration for the entire drone IoT system.
Every constant, threshold, port number, and secret lives here.
No other file should hardcode these values — always import from config.

Separate machine support:
    All host/port values are read from .env so they can be changed
    without touching any code. On same machine, defaults work fine.
    On separate machines: set COAP_HOST and MJPEG_HOST to the server's
    LAN IP in the client machine's .env file.
"""

import os
from dotenv import load_dotenv

load_dotenv()


# ─────────────────────────────────────────────
# TELLO SDK (DRONE ↔ GATEWAY LAYER)
# ─────────────────────────────────────────────

TELLO_IP         = "192.168.10.1"
TELLO_CMD_PORT   = 8889
TELLO_STATE_PORT = 8890
TELLO_VIDEO_PORT = 11111


# ─────────────────────────────────────────────
# COAP SERVER (GATEWAY ↔ CLIENT LAYER)
# ─────────────────────────────────────────────

# Server binds to 0.0.0.0 (all interfaces).
# Client connects to COAP_HOST — on same machine this is 127.0.0.1,
# on separate machines set COAP_HOST to server's LAN IP in client's .env.
COAP_HOST = os.getenv("COAP_HOST", "127.0.0.1")
COAP_PORT = int(os.getenv("COAP_PORT", "5683"))


# ─────────────────────────────────────────────
# VIDEO STREAM (GATEWAY → CLIENT)
# ─────────────────────────────────────────────

# MJPEG server binds to 0.0.0.0 on the gateway machine.
# Browser accesses it at http://MJPEG_HOST:MJPEG_PORT/video_feed.
# On same machine: MJPEG_HOST = 127.0.0.1 (default).
# On separate machines: set MJPEG_HOST to server's LAN IP in client .env.
MJPEG_HOST = os.getenv("MJPEG_HOST", "127.0.0.1")
MJPEG_PORT = int(os.getenv("MJPEG_PORT", "8080"))


# ─────────────────────────────────────────────
# WEB DASHBOARD (CLIENT SIDE)
# ─────────────────────────────────────────────

# Flask web dashboard — always runs on the client machine.
# Browser opens http://localhost:WEB_PORT
WEB_PORT = int(os.getenv("WEB_PORT", "5000"))


# ─────────────────────────────────────────────
# AUTHENTICATION (AES-128 MUTUAL AUTH)
# ─────────────────────────────────────────────

_secret = os.getenv("PRESHARED_SECRET")
if not _secret:
    raise EnvironmentError(
        "PRESHARED_SECRET not set.\n"
        "Copy .env.example → .env and fill in a 16-character secret key.\n"
        "Example:  PRESHARED_SECRET=MySecret12345678"
    )

_device_id = os.getenv("DEVICE_ID")
if not _device_id:
    raise EnvironmentError(
        "DEVICE_ID not set.\n"
        "Copy .env.example → .env and set a unique device identifier.\n"
        "Example:  DEVICE_ID=drone-monitor-client-01"
    )

PRESHARED_SECRET  = _secret.encode()
DEVICE_ID         = _device_id
MAX_AUTH_FAILURES = 4


# ─────────────────────────────────────────────
# ALERT THRESHOLDS
# ─────────────────────────────────────────────

BATTERY_ALERT_THRESHOLD  = 20   # percent
ALTITUDE_ALERT_THRESHOLD = 200  # cm
TILT_ALERT_THRESHOLD     = 10   # degrees — pitch or roll while on ground

# SSE push interval — how often Flask sends telemetry to browser
DASHBOARD_UPDATE_INTERVAL = 500  # milliseconds