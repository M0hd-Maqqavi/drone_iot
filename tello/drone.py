# tello/drone.py
"""
Gateway-layer bridge between the physical Tello drone and our software.

Responsibility:
    This module owns everything on the DRONE ↔ GATEWAY side of the architecture.
    It speaks the Tello SDK (proprietary UDP) so no other module has to.
    Everything above this layer speaks CoAP and never touches the SDK directly.

Two communication channels (both UDP, both completely separate):
    ┌─────────────────────────────────────────────────────────────────┐
    │  cmd_socket   → port 8889  (we SEND commands, drone RESPONDS)  │
    │  state_socket ← port 8890  (drone BROADCASTS, we LISTEN)       │
    └─────────────────────────────────────────────────────────────────┘

Why UDP and not TCP?
    The Tello's firmware uses UDP exclusively. TCP's connection handshake
    and reliability guarantees add overhead the drone's MCU doesn't need —
    it just fires-and-forgets telemetry 10 times per second.
    Lost telemetry packets are acceptable; the next one arrives in 100ms anyway.
"""

import socket
import threading
import logging
from config import TELLO_IP, TELLO_CMD_PORT, TELLO_STATE_PORT
import shared.state as state

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# TELEMETRY PARSER
# ─────────────────────────────────────────────

def parse_telemetry(raw: str) -> dict:
    """
    Convert the raw Tello state broadcast string into a Python dict.

    The drone sends a semicolon-delimited key:value string, e.g.:
        "pitch:-1;roll:0;yaw:3;vgx:0;vgy:0;vgz:0;templ:63;temph:66;
         tof:10;h:0;bat:87;baro:56.32;time:0;agx:-7.00;agy:-1.00;agz:-1000.00;\r\n"

    Parsing strategy:
        1. strip()  — remove leading/trailing whitespace and the trailing \r\n
        2. split(";") — split on semicolons → list of "key:value" strings
        3. For each chunk: split on ":" once → [key, value]
        4. Store in dict. Values remain strings here; state.update() casts to float.

    Why split(":", 1) instead of split(":")  ?
        The maxsplit=1 argument means we only split on the FIRST colon.
        Future firmware versions might include values that contain colons
        (e.g. timestamps). This makes the parser forward-compatible.

    Args:
        raw: raw bytes decoded to string from the UDP packet.

    Returns:
        dict of {field_name: value_string} pairs.
        Empty dict if the packet was entirely malformed.
    """
    result = {}
    for item in raw.strip().split(";"):
        if ":" in item:
            k, v = item.split(":", 1)
            result[k.strip()] = v.strip()
        # Fragments without a colon (e.g. trailing empty string after last ";")
        # are silently skipped — no exception, no log noise.
    return result


# ─────────────────────────────────────────────
# TELLO BRIDGE
# ─────────────────────────────────────────────

class TelloBridge:
    """
    Manages the two UDP sockets that form the drone ↔ gateway link.

    Lifecycle:
        bridge = TelloBridge()   # create sockets (no network activity yet)
        bridge.connect()         # enter SDK mode + start telemetry thread
        bridge.send_command("takeoff")   # optional: send flight commands
        bridge.disconnect()      # stop thread + close sockets

    Thread model:
        - Main thread  : calls connect(), send_command(), disconnect()
        - Daemon thread: runs _telemetry_loop() continuously in the background
        The daemon thread is non-blocking from the main thread's perspective.
        'daemon=True' means it is automatically killed when the main process exits,
        so we never need to explicitly join it on shutdown.
    """

    def __init__(self):
        """
        Create the two UDP sockets. No network activity happens here yet.

        socket.AF_INET   → IPv4 addressing
        socket.SOCK_DGRAM → UDP (datagram, connectionless)
            vs SOCK_STREAM which would be TCP (connection-oriented)

        Two sockets are needed because they serve opposite directions and
        bind to different local ports. A single socket cannot both bind to
        port 8890 (for receiving) and freely pick a port (for sending commands).
        """
        self.cmd_socket   = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.state_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # Flag used by _telemetry_loop to know when to stop.
        # Set to True by connect(), back to False by disconnect().
        self.running = False

    # ── COMMAND CHANNEL ──────────────────────────────────────────────

    def send_command(self, cmd: str) -> str:
        """
        Send a single Tello SDK text command and return the drone's response.

        How it works:
            1. Encode the command string to bytes (UDP sends bytes, not strings).
            2. sendto() fires the UDP packet at the drone's command port.
               UDP is fire-and-forget — sendto() returns immediately, no waiting.
            3. settimeout(5.0) makes recvfrom() raise socket.timeout if no reply
               arrives within 5 seconds — prevents the call hanging forever.
            4. recvfrom() blocks until a UDP packet arrives or timeout fires.
               We discard the sender address (_) since we know it's the drone.
            5. Decode the bytes response back to a string and strip whitespace.

        Common Tello SDK commands:
            "command"       → enter SDK mode (MUST be sent first)
            "takeoff"       → autonomous takeoff to ~80cm
            "land"          → land immediately
            "emergency"     → cut all motors instantly (use carefully)
            "battery?"      → query battery level (returns e.g. "87")
            "speed 50"      → set speed to 50 cm/s
            "up 50"         → move up 50 cm
            "cw 90"         → rotate 90° clockwise

        Args:
            cmd: Tello SDK command string (no encoding, just plain text).

        Returns:
            "ok"      — command accepted and executed.
            "error"   — command rejected (bad syntax, unsafe state, etc.).
            "timeout" — no response within 5 seconds (drone may be unreachable).
        """
        self.cmd_socket.sendto(cmd.encode(), (TELLO_IP, TELLO_CMD_PORT))
        try:
            self.cmd_socket.settimeout(5.0)
            response, _ = self.cmd_socket.recvfrom(1024)
            return response.decode().strip()
        except socket.timeout:
            # Treat timeout as an explicit failure so callers can handle it.
            # Don't raise — the caller decides whether to retry or abort.
            return "timeout"

    # ── TELEMETRY CHANNEL ────────────────────────────────────────────

    def _telemetry_loop(self):
        """
        Background thread: continuously receives and processes telemetry packets.

        How it works:
            1. bind(("", TELLO_STATE_PORT)) — attach the socket to port 8890
               on ALL network interfaces ("" means 0.0.0.0 = any interface).
               This is what allows the OS to route the drone's broadcast
               packets to our socket. Without bind(), recvfrom() would never
               receive anything on this port.
            2. settimeout(2.0) — recvfrom() won't block forever. If no packet
               arrives in 2 seconds, it raises socket.timeout and we loop back.
               This keeps the thread responsive to self.running becoming False.
            3. In the loop: receive → decode → parse → write to shared state.
            4. On exception: log and continue. One bad packet must not kill the
               listener; the next packet arrives in 100ms.

        Why a daemon thread?
            Setting daemon=True (in connect()) means this thread is automatically
            killed when the main program exits. We don't need to signal it or
            join it — Python handles cleanup for daemon threads automatically.
        """
        # Bind to all interfaces on port 8890 so drone's UDP broadcasts arrive here.
        self.state_socket.bind(("", TELLO_STATE_PORT))

        # 2-second timeout: keeps the loop from blocking indefinitely so it can
        # check self.running and exit cleanly when disconnect() sets it to False.
        self.state_socket.settimeout(2.0)
        logger.info("Telemetry listener started on port %d", TELLO_STATE_PORT)

        while self.running:
            try:
                # Block until a UDP packet arrives or the 2s timeout fires.
                # 1024 bytes is more than enough — Tello state packets are ~150 bytes.
                data, _ = self.state_socket.recvfrom(1024)

                # Decode bytes → string, then parse into a dict of field:value pairs.
                parsed = parse_telemetry(data.decode())

                if parsed:
                    # Write all fields to the thread-safe shared store.
                    # state.update() acquires the lock internally — we don't need to here.
                    state.update(parsed)

            except socket.timeout:
                # No packet arrived in 2 seconds — normal if drone is idle.
                # Just loop back and check self.running again.
                continue

            except Exception as e:
                # Catch-all: malformed packet, decode error, etc.
                # Log it but keep the loop alive — next packet arrives soon.
                logger.warning("Telemetry parse error: %s", e)

    # ── LIFECYCLE ────────────────────────────────────────────────────

    def connect(self):
        """
        Initialise the drone link and start the telemetry listener thread.

        Steps:
            1. Send "command" → drone enters SDK mode (replies "ok").
               Without this, the drone ignores all subsequent commands.
               This is the Tello's version of a handshake.
            2. Set self.running = True so _telemetry_loop keeps running.
            3. Spawn a daemon thread running _telemetry_loop.
               The thread starts immediately and listens for telemetry.

        Raises:
            ConnectionError: if the drone doesn't respond with "ok" to "command".
            This usually means:
                - You're not connected to the drone's Wi-Fi.
                - The drone is not powered on.
                - Another application is already using port 8889.
        """
        logger.info("Connecting to Tello drone at %s:%d ...", TELLO_IP, TELLO_CMD_PORT)

        resp = self.send_command("command")
        if resp != "ok":
            raise ConnectionError(
                f"Drone did not enter SDK mode. Response: '{resp}'\n"
                f"Make sure you are connected to the drone's Wi-Fi "
                f"(SSID: TELLO-XXXXXX) and the drone is powered on."
            )
        logger.info("SDK mode: OK — drone is ready to accept commands.")

        self.running = True
        t = threading.Thread(target=self._telemetry_loop, daemon=True)
        t.start()
        logger.info("Telemetry thread started — receiving state on port %d.", TELLO_STATE_PORT)

    def disconnect(self):
        """
        Gracefully stop the telemetry thread and release socket resources.

        Steps:
            1. self.running = False → _telemetry_loop exits on its next iteration
               (within at most 2 seconds due to the socket timeout).
            2. Close both sockets → releases the OS port bindings immediately.
               After this, no more packets can be sent or received.

        Note: We don't join() the daemon thread. Since it's a daemon, Python
        will clean it up when the process exits. Calling join() here would
        block for up to 2 seconds (the socket timeout) with no real benefit.
        """
        self.running = False
        self.cmd_socket.close()
        self.state_socket.close()
        logger.info("Tello bridge disconnected and sockets closed.")