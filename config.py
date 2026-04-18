# config.py
"""
Central configuration for the entire drone IoT system.
Every constant, threshold, port number, and secret lives here.
No other file should hardcode these values — always import from config.

Why centralise everything here?
    - One place to change a port or threshold instead of hunting across files.
    - Secrets are loaded from the environment (.env), never from source code.
    - If someone clones the repo, they get sensible EnvironmentErrors immediately
      rather than mysterious crashes deep inside the auth or CoAP logic.
"""

import os
from dotenv import load_dotenv

# load_dotenv() reads the .env file in the project root and injects its
# KEY=VALUE pairs into os.environ so os.getenv() can find them below.
# If no .env file exists, it silently does nothing — os.getenv() then
# returns None, and our guards below raise a clear EnvironmentError.
load_dotenv()


# ─────────────────────────────────────────────
# TELLO SDK (DRONE ↔ GATEWAY LAYER)
# ─────────────────────────────────────────────

# The Tello drone always gets this IP when it creates its own Wi-Fi hotspot.
# This is fixed by the drone's firmware — we cannot change it.
TELLO_IP = "192.168.10.1"

# Port 8889: We SEND SDK text commands here (e.g. "command", "takeoff", "land").
# The drone replies with "ok" or "error" on the same socket.
# This is a request/response channel — we initiate, drone responds.
TELLO_CMD_PORT = 8889

# Port 8890: The drone BROADCASTS its telemetry state here every ~100ms.
# We bind our state_socket to this port and passively listen.
# The drone initiates this — we never request it, it just arrives continuously.
TELLO_STATE_PORT = 8890

# Port 11111: Raw H.264 video stream from the drone's camera.
# Not used in this project — we focus on telemetry and control only.
# Kept here for completeness and potential future use.
TELLO_VIDEO_PORT = 11111


# ─────────────────────────────────────────────
# COAP SERVER (GATEWAY ↔ CLIENT LAYER)
# ─────────────────────────────────────────────

# The CoAP server (gateway) listens on this host.
# 127.0.0.1 = localhost — server and client run on the same machine.
# To run server and client on separate machines, change this to the
# server machine's LAN IP (e.g. "192.168.1.10") and update the client
# to point to the same address. No other code changes needed.
COAP_HOST = "127.0.0.1"

# Port 5683: The IANA-assigned standard port for CoAP.
# Analogous to port 80 for HTTP or port 443 for HTTPS.
# CoAP runs over UDP, not TCP — lightweight by design for IoT devices.
COAP_PORT = 5683


# ─────────────────────────────────────────────
# AUTHENTICATION (AES-128 MUTUAL AUTH)
# ─────────────────────────────────────────────

# PRESHARED_SECRET = λi.
# This is a 16-byte (128-bit) symmetric key shared between client and server
# BEFORE deployment — like a password baked into the hardware at the factory.
# It is used to:
#   1. Encrypt/decrypt the server challenge in step 2.
#   2. Hide the session key via XOR: ψ = λi ⊕ μkey
#   3. Encrypt the server's final proof in step 4.
# MUST be exactly 16 bytes for AES-128. Any string shorter or longer will
# cause a ValueError inside pycryptodome at encryption time.
_secret = os.getenv("PRESHARED_SECRET")
if not _secret:
    raise EnvironmentError(
        "PRESHARED_SECRET not set.\n"
        "Copy .env.example → .env and fill in a 16-character secret key.\n"
        "Example:  PRESHARED_SECRET=MySecret12345678"
    )

# DEVICE_ID is sent in plaintext in Step 1 of the handshake.
# It tells the server WHICH pre-shared secret to look up for this client.
# In a real system, each device has a UNIQUE Device ID mapped to its own λi.
# For this project, all clients share the same secret — simpler but correct.
_device_id = os.getenv("DEVICE_ID")
if not _device_id:
    raise EnvironmentError(
        "DEVICE_ID not set.\n"
        "Copy .env.example → .env and set a unique device identifier.\n"
        "Example:  DEVICE_ID=drone-monitor-client-01"
    )

# .encode() converts the string from .env into bytes, which is what
# pycryptodome's AES.new() expects. Strings won't work directly.
PRESHARED_SECRET = _secret.encode()
DEVICE_ID = _device_id

# After this many consecutive failed auth attempts from the same Device ID,
# that device is added to the blacklist and all further requests are rejected.
# Mirrors the DoS/brute-force protection described in the Week 8 lecture.
MAX_AUTH_FAILURES = 4


# ─────────────────────────────────────────────
# DASHBOARD & ALERT THRESHOLDS
# ─────────────────────────────────────────────

# If battery drops to or below this percentage, the CoAP server sends
# a CON (Confirmable) alert message to the client — client must ACK it.
# We use CON (not NON) for alerts because delivery must be guaranteed.
BATTERY_ALERT_THRESHOLD = 20    # percent

# If the drone's reported height exceeds this value, a CON alert fires.
# Tello reports height in centimetres above the takeoff point.
ALTITUDE_ALERT_THRESHOLD = 200  # cm

# How often the matplotlib dashboard redraws its plots.
# 500ms = 2 refreshes per second — fast enough to feel live,
# light enough not to saturate the CPU on the client machine.
DASHBOARD_UPDATE_INTERVAL = 500  # milliseconds