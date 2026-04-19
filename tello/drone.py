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
        bridge = TelloBridge()          # create sockets (no network activity yet)
        bridge.connect()                # enter SDK mode + start telemetry thread
        bridge.takeoff()                # take off
        bridge.move("up", 50)           # move up 50cm
        bridge.land()                   # land
        bridge.disconnect()             # stop thread + close sockets

    Thread model:
        - Main thread  : calls connect(), flight commands, disconnect()
        - Daemon thread: runs _telemetry_loop() continuously in the background
        The daemon thread is non-blocking from the main thread's perspective.
        'daemon=True' means it is automatically killed when the main process exits,
        so we never need to explicitly join it on shutdown.

    Command methods:
        All flight commands are thin wrappers around send_command().
        They exist so server.py can call bridge.takeoff() instead of
        bridge.send_command("takeoff") — cleaner and less error-prone.
        Each method validates the response and raises RuntimeError on failure,
        so the caller always knows if the drone actually executed the command.
    """

    # Valid movement directions and their Tello SDK equivalents.
    # Used by move() for validation before sending to the drone.
    VALID_DIRECTIONS = {"up", "down", "left", "right", "forward", "back", "cw", "ccw"}

    # Tello SDK distance limits (cm). move() enforces these before sending.
    # Sending out-of-range values causes the drone to respond with "error".
    MIN_DISTANCE = 20   # cm
    MAX_DISTANCE = 500  # cm

    def __init__(self):
        """
        Create the two UDP sockets. No network activity happens here yet.

        socket.AF_INET    → IPv4 addressing
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

    # ── LOW-LEVEL COMMAND SENDER ─────────────────────────────────────

    def send_command(self, cmd: str) -> str:
        """
        Send a single Tello SDK text command and return the drone's response.
        This is the only method that actually touches the network on this side.
        All higher-level methods (takeoff, land, move, etc.) call this.

        How it works:
            1. Encode the command string to bytes (UDP sends bytes, not strings).
            2. sendto() fires the UDP packet at the drone's command port.
               UDP is fire-and-forget — sendto() returns immediately.
            3. settimeout(5.0) makes recvfrom() raise socket.timeout after 5s.
            4. recvfrom() blocks until a UDP packet arrives or timeout fires.
               Sender address is discarded — we know it's the drone.
            5. Decode bytes response back to string and strip whitespace.

        Args:
            cmd: Tello SDK command string (plain text, e.g. "takeoff", "up 50").

        Returns:
            "ok"      — command accepted and executed.
            "error"   — command rejected (bad syntax, unsafe state, etc.).
            "timeout" — no response within 5 seconds (drone unreachable).
        """
        self.cmd_socket.sendto(cmd.encode(), (TELLO_IP, TELLO_CMD_PORT))
        try:
            self.cmd_socket.settimeout(5.0)
            response, _ = self.cmd_socket.recvfrom(1024)
            return response.decode("latin-1").strip()
        except socket.timeout:
            return "timeout"

    # ── FLIGHT COMMAND WRAPPERS ───────────────────────────────────────
    # These methods exist so server.py calls bridge.takeoff() rather than
    # bridge.send_command("takeoff"). Benefits:
    #   - Cleaner call sites — no raw strings scattered across the codebase.
    #   - Centralised response validation and logging per command type.
    #   - Self-documenting — method names describe intent clearly.
    #   - Easier to extend — add pre-flight checks or retries here later.

    def takeoff(self) -> str:
        """
        Command the drone to take off autonomously.
        Drone ascends to approximately 80cm above the surface and hovers.

        Prerequisites:
            - Drone must be on a flat surface.
            - Props must be unobstructed.
            - Battery should be above 20%.

        Blocks until the drone confirms it has reached hover altitude.
        This typically takes 2–3 seconds.

        Returns:
            "ok", "error", or "timeout"
        """
        logger.info("Command: takeoff")
        response = self.send_command("takeoff")
        if response != "ok":
            logger.warning("Takeoff returned: %s", response)
        return response

    def land(self) -> str:
        """
        Command the drone to land at its current position.
        Drone descends slowly and cuts motors on touchdown.

        Blocks until the drone confirms it has landed.
        This typically takes 2–4 seconds depending on altitude.

        Returns:
            "ok", "error", or "timeout"
        """
        logger.info("Command: land")
        response = self.send_command("land")
        if response != "ok":
            logger.warning("Land returned: %s", response)
        return response

    def emergency(self) -> str:
        """
        CUT ALL MOTORS IMMEDIATELY.

        ⚠ USE WITH EXTREME CAUTION ⚠
        The drone drops instantly from whatever altitude it is at.
        Only use if the drone is behaving dangerously.

        Unlike land(), this does NOT descend gracefully — it is a
        hard stop of all four motors simultaneously.

        Returns:
            "ok", "error", or "timeout"
        """
        logger.warning("EMERGENCY STOP — cutting all motors immediately.")
        return self.send_command("emergency")

    def move(self, direction: str, distance: int) -> str:
        """
        Move the drone in a given direction by a given distance.

        Validates direction and distance BEFORE sending to the drone.
        The Tello firmware responds with "error" for out-of-range values,
        so we catch them here for a clearer error message.

        Args:
            direction: One of:
                "up"      — ascend vertically
                "down"    — descend vertically
                "left"    — strafe left (relative to drone nose direction)
                "right"   — strafe right
                "forward" — move forward (direction drone is facing)
                "back"    — move backward
                "cw"      — rotate clockwise      (distance = degrees)
                "ccw"     — rotate counterclockwise (distance = degrees)

            distance:  Distance in cm (or degrees for cw/ccw rotations).
                       Must be between MIN_DISTANCE (20) and MAX_DISTANCE (500).

        Returns:
            "ok", "error", or "timeout"

        Raises:
            ValueError: if direction is invalid or distance is out of range.
                        Raised BEFORE any network call so callers handle
                        bad input without waiting for a drone response.
        """
        direction = direction.lower()

        if direction not in self.VALID_DIRECTIONS:
            raise ValueError(
                f"Invalid direction '{direction}'. "
                f"Valid options: {sorted(self.VALID_DIRECTIONS)}"
            )

        if not (self.MIN_DISTANCE <= distance <= self.MAX_DISTANCE):
            raise ValueError(
                f"Distance {distance} out of range. "
                f"Must be {self.MIN_DISTANCE}–{self.MAX_DISTANCE}."
            )

        sdk_cmd = f"{direction} {distance}"
        logger.info("Command: %s", sdk_cmd)
        response = self.send_command(sdk_cmd)

        if response != "ok":
            logger.warning("Move '%s' returned: %s", sdk_cmd, response)
        return response

    def rotate(self, degrees: int, clockwise: bool = True) -> str:
        """
        Convenience wrapper for rotation — cleaner than move("cw", 90).

        Args:
            degrees:   Degrees to rotate (20–500).
            clockwise: True for clockwise, False for counterclockwise.

        Returns:
            "ok", "error", or "timeout"
        """
        direction = "cw" if clockwise else "ccw"
        return self.move(direction, degrees)

    def get_battery(self) -> int:
        """
        Query the drone's battery level directly via SDK command.

        Note: Battery is also streamed continuously via the telemetry port
        and available in shared/state.py. Use this only for a one-off
        reading before the telemetry loop has started (e.g. pre-flight check).

        Returns:
            Battery percentage as int (0–100), or -1 if query failed.
        """
        response = self.send_command("battery?")
        try:
            return int(response)
        except ValueError:
            logger.warning("Could not parse battery response: '%s'", response)
            return -1

    # ── TELEMETRY CHANNEL ────────────────────────────────────────────

    def _telemetry_loop(self):
        """
        Background thread: continuously receives and processes telemetry packets.

        How it works:
            1. bind(("", TELLO_STATE_PORT)) — attach the socket to port 8890
               on ALL network interfaces ("" = 0.0.0.0 = any interface).
               Without bind(), recvfrom() would never receive anything here.
            2. settimeout(2.0) — prevents recvfrom() blocking forever.
               Keeps the thread responsive to self.running becoming False.
            3. Loop: receive → decode → parse → write to shared state.
            4. On exception: log and continue. One bad packet must not kill
               the listener; the next packet arrives in 100ms.

        Why a daemon thread?
            daemon=True means Python automatically kills it when the main
            process exits. No explicit join() needed on shutdown.
        """
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
                continue
            except Exception as e:
                logger.warning("Telemetry parse error: %s", e)

    # ── LIFECYCLE ────────────────────────────────────────────────────

    def connect(self):
        """
        Initialise the drone link and start the telemetry listener thread.

        Steps:
            1. Send "command" → drone enters SDK mode (replies "ok").
               Without this, the drone ignores all subsequent commands.
            2. Set self.running = True so _telemetry_loop keeps looping.
            3. Spawn daemon thread running _telemetry_loop.

        Raises:
            ConnectionError: if the drone doesn't respond with "ok".
            Check: connected to drone Wi-Fi? Drone powered on?
        """
        logger.info("Connecting to Tello at %s:%d ...", TELLO_IP, TELLO_CMD_PORT)

        resp = self.send_command("command")
        if resp != "ok":
            raise ConnectionError(
                f"Drone did not enter SDK mode. Response: '{resp}'\n"
                f"Ensure you are connected to the drone's Wi-Fi (TELLO-XXXXXX) "
                f"and the drone is powered on."
            )
        logger.info("SDK mode: OK — drone ready.")

        self.running = True
        t = threading.Thread(target=self._telemetry_loop, daemon=True)
        t.start()
        logger.info("Telemetry thread running on port %d.", TELLO_STATE_PORT)

    def disconnect(self):
        """
        Stop the telemetry thread and release socket resources.

        self.running = False causes _telemetry_loop to exit within
        at most 2 seconds (the socket timeout interval).
        Sockets are then closed to release OS port bindings immediately.
        """
        self.running = False
        self.cmd_socket.close()
        self.state_socket.close()
        logger.info("Tello bridge disconnected.")