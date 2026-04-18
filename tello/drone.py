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


def parse_telemetry(raw: str) -> dict:
    """
    Tello broadcasts state as a semicolon-separated key:value string, e.g.:
    "pitch:-1;roll:0;yaw:3;vgx:0;...;bat:87;\r\n"
    We strip whitespace, split on ';', then split each chunk on ':'.
    """
    result = {}
    for item in raw.strip().split(";"):
        if ":" in item:
            k, v = item.split(":", 1)
            result[k.strip()] = v.strip()
    return result


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

    def __init__(self):
        self.cmd_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.state_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.running = False

    def send_command(self, cmd: str) -> str:
        """Send a Tello SDK command and wait for 'ok' or 'error' response."""
        self.cmd_socket.sendto(cmd.encode(), (TELLO_IP, TELLO_CMD_PORT))
        try:
            self.cmd_socket.settimeout(5.0)
            response, _ = self.cmd_socket.recvfrom(1024)
            return response.decode().strip()
        except socket.timeout:
            return "timeout"

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
                data, _ = self.state_socket.recvfrom(1024)
                parsed = parse_telemetry(data.decode())
                if parsed:
                    state.update(parsed)
            except socket.timeout:
                continue  # no packet in 2 sec — just loop again
            except Exception as e:
                logger.warning("Telemetry parse error: %s", e)

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

        self.running = True
        t = threading.Thread(target=self._telemetry_loop, daemon=True)
        t.start()
        logger.info("Tello bridge connected and telemetry thread running.")

    def disconnect(self):
        self.running = False
        self.cmd_socket.close()
        self.state_socket.close()
        logger.info("Tello bridge disconnected.")