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
PRESHARED_SECRET = b"ThisIsMySecret!!"   # exactly 16 bytes

# Device ID — client identifies itself with this in Step 1 of handshake
DEVICE_ID = "drone-monitor-client-01"

# Max failed auth attempts before blacklist
MAX_AUTH_FAILURES = 4

# --- Dashboard ---
BATTERY_ALERT_THRESHOLD = 20   # % — triggers CON alert message
ALTITUDE_ALERT_THRESHOLD = 200 # cm
DASHBOARD_UPDATE_INTERVAL = 500 # ms — matplotlib refresh rate