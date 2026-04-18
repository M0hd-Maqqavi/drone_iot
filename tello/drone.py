# tello/drone.py
# Handles everything related to the physical drone:
#   - Connecting to it over its Wi-Fi
#   - Putting it into SDK mode
#   - Parsing the telemetry state stream it broadcasts every 100ms

import socket
import threading
import logging
from config import TELLO_IP, TELLO_CMD_PORT, TELLO_STATE_PORT
import shared.state as state

logger = logging.getLogger(__name__)

# Parses the telemetry string the drone broadcasts into a dict of key:value pairs.
def parse_telemetry(raw: str) -> dict:
    """
    Tello broadcasts state as a semicolon-separated key:value string, e.g.:
    "pitch:-1;roll:0;yaw:3;vgx:0;...;bat:87;\r\n"
    We strip whitespace, split on ';', then split each chunk on ':'.
    """
    result = {} # Store parsed key:value pairs here. Keys mirror names from the Tello state message.
    for item in raw.strip().split(";"):
        # We strip whitespace, split on ';', then split each chunk on ':'.
        if ":" in item:
            # Split only once in case a future value ever contains ':'.
            k, v = item.split(":", 1)
            result[k.strip()] = v.strip()
        # Silently ignore malformed fragments without a key:value shape.
    return result

# The bridge between the physical drone and our software. Two UDP sockets:
#   - cmd_socket — we talk TO the drone (send "command", "takeoff", etc.)
#   - state_socket — we LISTEN to the drone (it fires telemetry at us every 100ms)
class TelloBridge:
    """
    Manages two UDP sockets:
      - cmd_socket: we SEND commands to the drone (port 8889)
      - state_socket: we LISTEN for telemetry FROM the drone (port 8890)
    
    UDP is connectionless — there's no "handshake" like TCP.
    We just send bytes to an IP:port and listen on a port.
    The drone's firmware is hardcoded to send state packets to whoever
    sent the initial "command" string, on port 8890.
    """

    # __init__ sets up sockets but does not connect to the drone yet. connect() does the actual handshake and starts the telemetry thread.
    def __init__(self):
        # One UDP socket for command/response and one dedicated telemetry listener.
        # Sockets are kept open for the lifetime of the bridge.
        self.cmd_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.state_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.running = False

    # send_command is a helper for sending a command and waiting for the response.
    def send_command(self, cmd: str) -> str:
        """Send a Tello SDK command and wait for 'ok' or 'error' response."""
        # Command channel is request/response over UDP.
        self.cmd_socket.sendto(cmd.encode(), (TELLO_IP, TELLO_CMD_PORT))
        try:
            # Per-command timeout keeps startup and control paths responsive.
            self.cmd_socket.settimeout(5.0)
            response, _ = self.cmd_socket.recvfrom(1024)
            # Drone responses are short text like "ok" or "error".
            return response.decode().strip()
        except socket.timeout:
            # Callers treat timeout as an explicit failure state.
            return "timeout"

    # _telemetry_loop runs in a background thread, continuously listening for telemetry packets and updating shared state.
    def _telemetry_loop(self):
        """
        Runs in a background thread.
        Binds to port 8890 on our machine, then sits in a loop
        reading UDP packets the drone sends every 100ms.
        Each packet is parsed and written to shared state.
        """
        # Bind to all interfaces on port 8890 so we receive the drone's broadcasts
        self.state_socket.bind(("", TELLO_STATE_PORT))
        self.state_socket.settimeout(2.0)
        logger.info("Telemetry listener started on port %d", TELLO_STATE_PORT)

        while self.running:
            try:
                # 1024 bytes is ample for Tello state packets.
                data, _ = self.state_socket.recvfrom(1024)
                parsed = parse_telemetry(data.decode())
                if parsed:
                    # Central shared store is lock-protected inside state.update.
                    state.update(parsed)
            except socket.timeout:
                continue  # no packet in 2 sec — just loop again
            except Exception as e:
                # Keep listener alive even if one packet is malformed.
                logger.warning("Telemetry parse error: %s", e)

    # connect() is the main entry point to start talking to the drone. It sends the initial "command" to enter SDK mode, then starts the telemetry thread.
    def connect(self):
        """
        1. Send 'command' to enter SDK mode — drone replies 'ok'
        2. Send 'streamon' to start video (we won't display it but good practice)
        3. Kick off background thread for telemetry
        """
        logger.info("Connecting to Tello drone...")
        resp = self.send_command("command")
        if resp != "ok":
            raise ConnectionError(f"Drone did not enter SDK mode. Response: {resp}")
        logger.info("SDK mode: OK")

        # Daemon thread exits with the process; no explicit join required on shutdown.
        self.running = True
        t = threading.Thread(target=self._telemetry_loop, daemon=True)
        t.start()
        logger.info("Tello bridge connected and telemetry thread running.")

    # disconnect() cleanly shuts down the telemetry thread and closes sockets.
    def disconnect(self):
        # Stop loop first, then close sockets to release OS resources immediately.
        self.running = False
        self.cmd_socket.close()
        self.state_socket.close()
        logger.info("Tello bridge disconnected.")