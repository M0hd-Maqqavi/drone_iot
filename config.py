# config.py
import os
from dotenv import load_dotenv

load_dotenv()  # reads .env file into environment variables

# --- Tello SDK ---
TELLO_IP = "192.168.10.1"
TELLO_CMD_PORT = 8889       # send commands TO drone
TELLO_STATE_PORT = 8890     # drone broadcasts telemetry TO us on this port
TELLO_VIDEO_PORT = 11111    # not used in this project

# --- CoAP Server ---
COAP_HOST = "127.0.0.1"     # localhost — server and client run on same machine
COAP_PORT = 5683            # standard CoAP port (like HTTP's 80)

# --- Auth ---
# Pre-shared secret (λi) — 16 bytes = 128 bits, must be same on client and server
# In a real deployment this would be provisioned into hardware before deployment

# Copy the .env.example file to .env and fill in your own values for the secret and device ID

# For security, we read the pre-shared secret from an environment variable, with a default fallback
# The .env file should contain a line like:
# PRESHARED_SECRET=<your-16-byte-secret-here>
# DEVICE_ID=<your-device-id-here>
_secret = os.getenv("PRESHARED_SECRET")
PRESHARED_SECRET = _secret.encode()   # convert to bytes for AES
DEVICE_ID = os.getenv("DEVICE_ID")
MAX_AUTH_FAILURES = 4

# --- Dashboard ---
BATTERY_ALERT_THRESHOLD = 20    # % — triggers CON alert message
ALTITUDE_ALERT_THRESHOLD = 200  # cm
DASHBOARD_UPDATE_INTERVAL = 500 # ms — matplotlib refresh rate