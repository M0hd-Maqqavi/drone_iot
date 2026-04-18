# coap/server.py
"""
CoAP Server — Gateway Application Layer.

This is the heart of the gateway. It does two things:
    1. Exposes drone telemetry as observable CoAP resources.
    2. Accepts flight commands from authenticated clients and
       forwards them to the drone via the Tello SDK.
    3. Manages the 4-step mutual authentication handshake.

Architecture reminder:
    [Drone] ──SDK/UDP──► [TelloBridge in drone.py]
                                    │
                             writes to shared state
                                    │
                                    ▼
                         [This CoAP server reads shared state]
                                    │
                          exposes as CoAP resources
                                    │
                                    ▼
                         [CoAP Client — coap/client.py]

How aiocoap works (Flask analogy):
    - aiocoap is async (uses asyncio, not threads).
    - You define Resource classes (like Flask view classes).
    - Each resource handles one URI path.
    - render_get / render_post are like Flask's get() / post() methods.
    - The site (like Flask's app) maps URI paths to resource instances.
    - aiocoap handles CON/NON/ACK/token management automatically.

Observe (pub/sub for CoAP):
    - Client sends GET with Observe=0 option → "subscribe me".
    - Server stores that client as an observer.
    - Whenever data changes, server calls self._notify() to push updates.
    - Client receives NON messages automatically — no polling needed.
    - This is exactly the pub/sub behaviour described in the Week 3-5 lecture.
"""

import json
import asyncio
import logging
import aiocoap
import aiocoap.resource as resource
from aiocoap.numbers.contentformat import ContentFormat

from auth.handshake import AuthServer
from config import COAP_HOST, COAP_PORT, BATTERY_ALERT_THRESHOLD, ALTITUDE_ALERT_THRESHOLD
import shared.state as state

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# SHARED AUTH SERVER INSTANCE
# ─────────────────────────────────────────────

# One AuthServer instance is shared across all auth resource handlers.
# This is important — session state (nonces, μkey, blacklist) must persist
# across the 4 separate HTTP-like request/response steps.
auth_server = AuthServer()


# ─────────────────────────────────────────────
# AUTH RESOURCES (Steps 1–4)
# ─────────────────────────────────────────────

class AuthInitResource(resource.Resource):
    """
    Step 1: POST /auth/init
    Client sends its Device ID in plaintext.
    Server registers a pending session.

    CoAP method: POST (client is creating a new session resource).
    Response codes:
        2.01 Created  — session accepted, proceed to step 2.
        4.01 Unauthorized — blacklisted device.
        4.00 Bad Request  — malformed payload.
    """

    async def render_post(self, request):
        result = auth_server.step1_receive_initiation(request.payload)

        if result["status"] == "ok":
            # Store device_id in a simple way so step 2 knows who to challenge.
            # In a multi-client system you'd use session tokens, but for this
            # project one active client is sufficient.
            self.pending_device_id = result["device_id"]
            logger.info("Auth Step 1: session initiated for %s", result["device_id"])
            return aiocoap.Message(
                code=aiocoap.CREATED,
                payload=json.dumps(result).encode()
            )
        elif result.get("reason") == "blacklisted":
            return aiocoap.Message(
                code=aiocoap.UNAUTHORIZED,
                payload=b'{"status":"error","reason":"blacklisted"}'
            )
        else:
            return aiocoap.Message(
                code=aiocoap.BAD_REQUEST,
                payload=json.dumps(result).encode()
            )


class AuthChallengeResource(resource.Resource):
    """
    Step 2: GET /auth/challenge
    Server generates and returns the encrypted challenge.
    Client must decrypt it using λi to recover μkey and ηserver.

    CoAP method: GET (client is retrieving the challenge data).
    Response: raw encrypted bytes (not JSON — binary crypto payload).
    """

    def __init__(self, auth_init_resource):
        super().__init__()
        # Reference to AuthInitResource so we can read pending_device_id.
        # This is a simple way to share state between resource handlers
        # without a global variable.
        self._init_resource = auth_init_resource

    async def render_get(self, request):
        device_id = getattr(self._init_resource, "pending_device_id", None)

        if not device_id:
            return aiocoap.Message(
                code=aiocoap.BAD_REQUEST,
                payload=b"No pending session. Complete step 1 first."
            )

        # Generate challenge: ηserver, μkey, ψ = λi XOR μkey, then AES encrypt.
        encrypted_challenge = auth_server.step2_send_challenge(device_id)
        logger.info("Auth Step 2: challenge sent to %s", device_id)

        return aiocoap.Message(
            code=aiocoap.CONTENT,
            payload=encrypted_challenge
            # No content format set — raw binary crypto payload.
        )


class AuthVerifyResource(resource.Resource):
    """
    Step 3: POST /auth/verify
    Client proves its identity by responding to the challenge.
    Client sends: AES{μkey, (ηserver XOR λi | ηclient)}

    Server:
        - Decrypts with μkey (proves client got μkey right in step 2).
        - Recovers ηserver, verifies it matches what was sent.
        - Stores ηclient for use in step 4.
    """

    def __init__(self, auth_init_resource):
        super().__init__()
        self._init_resource = auth_init_resource

    async def render_post(self, request):
        device_id = getattr(self._init_resource, "pending_device_id", None)

        if not device_id:
            return aiocoap.Message(
                code=aiocoap.BAD_REQUEST,
                payload=b"No pending session."
            )

        result = auth_server.step3_verify_client(device_id, request.payload)

        if result["status"] == "ok":
            logger.info("Auth Step 3: client verified — %s", device_id)
            return aiocoap.Message(
                code=aiocoap.CHANGED,
                payload=b'{"status":"ok"}'
            )
        else:
            logger.warning("Auth Step 3: verification FAILED for %s", device_id)
            return aiocoap.Message(
                code=aiocoap.UNAUTHORIZED,
                payload=json.dumps(result).encode()
            )


class AuthConfirmResource(resource.Resource):
    """
    Step 4: GET /auth/confirm
    Server proves its own identity back to the client.
    Server sends: AES{λi, (ηclient | μkey)}

    Client decrypts with λi, finds its own ηclient inside — 
    only a real server with λi could produce this.
    After this: MUTUAL AUTHENTICATION COMPLETE.
    """

    def __init__(self, auth_init_resource):
        super().__init__()
        self._init_resource = auth_init_resource

    async def render_get(self, request):
        device_id = getattr(self._init_resource, "pending_device_id", None)

        if not device_id:
            return aiocoap.Message(
                code=aiocoap.BAD_REQUEST,
                payload=b"No pending session."
            )

        encrypted_response = auth_server.step4_send_response(device_id)
        logger.info("Auth Step 4: mutual auth complete for %s", device_id)

        return aiocoap.Message(
            code=aiocoap.CONTENT,
            payload=encrypted_response
        )


# ─────────────────────────────────────────────
# AUTH GUARD (decorator-style helper)
# ─────────────────────────────────────────────

def require_auth(device_id: str) -> bool:
    """
    Check if a device is authenticated before allowing access to
    telemetry or command resources.

    Every telemetry GET and command POST calls this first.
    If it returns False, the resource returns 4.01 Unauthorized.

    In a production system this would also validate a session token
    or check the μkey signature on the request payload. For this project,
    checking the authentication state is sufficient.
    """
    return auth_server.is_authenticated(device_id)


# ─────────────────────────────────────────────
# TELEMETRY RESOURCES (Observable)
# ─────────────────────────────────────────────

class TelemetryResource(resource.ObservableResource):
    """
    Base class for all observable telemetry resources.

    ObservableResource (aiocoap):
        - Extends the basic Resource class with observer management.
        - Maintains a list of subscribed clients internally.
        - Call self.updated_state() to trigger a push notification
          to all current observers — aiocoap handles the rest.

    How observation works in practice:
        1. Client sends GET with Observe=0 option.
        2. aiocoap registers the client as an observer automatically.
        3. Our background task calls self.updated_state() periodically.
        4. aiocoap calls render_get() again and pushes the result to observers
           as NON messages (no ACK required — appropriate for periodic telemetry).
        5. For alerts (battery/altitude), we use CON by raising the
           notify_observers flag — aiocoap escalates to CON automatically.

    Auth check:
        Every render_get checks require_auth(). If the client hasn't
        completed the 4-step handshake, it gets a 4.01 Unauthorized.
        Device ID is sent as a URI query parameter: ?device_id=xxx
    """

    def __init__(self, field_name: str, device_id_header: str = "device_id"):
        super().__init__()
        self.field_name = field_name
        # How often (seconds) to push updates to observers.
        # 1 second is a good balance — fast enough to feel live,
        # not so fast that it floods the CoAP channel.
        self.notify_period = 1.0
        # Background task handle (set in start_notify_task).
        self._notify_task = None

    def _get_value(self) -> dict:
        """
        Subclasses override this to return the specific telemetry field(s).
        Returns a dict that will be JSON-encoded as the CoAP payload.
        """
        raise NotImplementedError

    def _check_alerts(self, data: dict) -> bool:
        """
        Returns True if this update should be sent as a CON (alert) message
        rather than a NON (normal telemetry) message.
        Subclasses override this for fields that have alert thresholds.
        """
        return False

    async def render_get(self, request):
        """
        Called by aiocoap both for initial GET and for each Observe notification.
        Returns the current value as a JSON payload.
        """
        # Extract device_id from URI query string: ?device_id=xxx
        # aiocoap exposes query options as a list of "key=value" strings.
        device_id = None
        for opt in request.opt.uri_query:
            if opt.startswith("device_id="):
                device_id = opt.split("=", 1)[1]

        if not device_id or not require_auth(device_id):
            return aiocoap.Message(
                code=aiocoap.UNAUTHORIZED,
                payload=b'{"error":"not authenticated"}'
            )

        data = self._get_value()
        payload = json.dumps(data).encode()

        return aiocoap.Message(
            code=aiocoap.CONTENT,
            payload=payload,
            content_format=ContentFormat.JSON
        )

    async def _notify_loop(self):
        """
        Background coroutine: wakes up every notify_period seconds and
        calls updated_state() to push new telemetry to all observers.

        updated_state() is an aiocoap method — it triggers aiocoap to
        call render_get() again and push the result to all registered observers.
        """
        while True:
            await asyncio.sleep(self.notify_period)
            self.updated_state()

    def start_notify_task(self, loop):
        """
        Start the background notification loop.
        Called once when the server starts up.
        """
        self._notify_task = loop.create_task(self._notify_loop())


# ── Individual telemetry resource classes ────────────────────────────

class BatteryResource(TelemetryResource):
    """
    GET /drone/telemetry/battery
    Returns: {"battery": 87.0, "unit": "%", "alert": false}
    Alert fires (CON) when battery <= BATTERY_ALERT_THRESHOLD.
    """

    def __init__(self):
        super().__init__("battery")

    def _get_value(self) -> dict:
        snap = state.snapshot()
        battery = snap["battery"]
        alert = battery <= BATTERY_ALERT_THRESHOLD and snap["connected"]
        if alert:
            logger.warning("ALERT: Battery low — %.0f%%", battery)
        return {"battery": battery, "unit": "%", "alert": alert}

    def _check_alerts(self, data: dict) -> bool:
        return data.get("alert", False)


class HeightResource(TelemetryResource):
    """
    GET /drone/telemetry/height
    Returns: {"height": 120.0, "tof": 115.0, "unit": "cm", "alert": false}
    Includes both barometric height and ToF distance for comparison.
    Alert fires when height > ALTITUDE_ALERT_THRESHOLD.
    """

    def __init__(self):
        super().__init__("height")

    def _get_value(self) -> dict:
        snap = state.snapshot()
        height = snap["height"]
        alert = height > ALTITUDE_ALERT_THRESHOLD and snap["connected"]
        if alert:
            logger.warning("ALERT: Altitude threshold exceeded — %.0fcm", height)
        return {
            "height": height,
            "tof": snap["tof"],
            "unit": "cm",
            "alert": alert
        }

    def _check_alerts(self, data: dict) -> bool:
        return data.get("alert", False)


class TemperatureResource(TelemetryResource):
    """
    GET /drone/telemetry/temperature
    Returns: {"templ": 63.0, "temph": 66.0, "unit": "C"}
    Both lower and upper bounds from the drone's IMU temperature sensor.
    """

    def __init__(self):
        super().__init__("temperature")

    def _get_value(self) -> dict:
        snap = state.snapshot()
        return {
            "templ": snap["templ"],
            "temph": snap["temph"],
            "unit": "C"
        }


class OrientationResource(TelemetryResource):
    """
    GET /drone/telemetry/orientation
    Returns: {"pitch": -1.0, "roll": 0.0, "yaw": 3.0, "unit": "degrees"}
    Full 3-axis orientation of the drone.
    """

    def __init__(self):
        super().__init__("orientation")

    def _get_value(self) -> dict:
        snap = state.snapshot()
        return {
            "pitch": snap["pitch"],
            "roll":  snap["roll"],
            "yaw":   snap["yaw"],
            "unit":  "degrees"
        }


class VelocityResource(TelemetryResource):
    """
    GET /drone/telemetry/velocity
    Returns: {"vgx": 0.0, "vgy": 0.0, "vgz": 0.0, "unit": "cm/s"}
    Ground-frame velocity in all three axes.
    """

    def __init__(self):
        super().__init__("velocity")

    def _get_value(self) -> dict:
        snap = state.snapshot()
        return {
            "vgx":  snap["vgx"],
            "vgy":  snap["vgy"],
            "vgz":  snap["vgz"],
            "unit": "cm/s"
        }


class AccelerationResource(TelemetryResource):
    """
    GET /drone/telemetry/acceleration
    Returns: {"agx": -7.0, "agy": -1.0, "agz": -1000.0, "unit": "cm/s2"}
    Raw IMU accelerometer values. agz ≈ -1000 at rest (1g gravity downward).
    """

    def __init__(self):
        super().__init__("acceleration")

    def _get_value(self) -> dict:
        snap = state.snapshot()
        return {
            "agx":  snap["agx"],
            "agy":  snap["agy"],
            "agz":  snap["agz"],
            "unit": "cm/s2"
        }


class TofResource(TelemetryResource):
    """
    GET /drone/telemetry/tof
    Returns: {"tof": 10.0, "unit": "cm"}
    Time-of-Flight infrared distance to the surface below the drone.
    More reliable than barometric height at low altitudes.
    """

    def __init__(self):
        super().__init__("tof")

    def _get_value(self) -> dict:
        snap = state.snapshot()
        return {"tof": snap["tof"], "unit": "cm"}


# ─────────────────────────────────────────────
# COMMAND RESOURCES
# ─────────────────────────────────────────────

class CommandResource(resource.Resource):
    """
    Base class for all drone command resources.

    Each command is a CoAP POST to a specific URI:
        POST /drone/command/takeoff
        POST /drone/command/land
        POST /drone/command/move
        POST /drone/command/emergency

    The resource:
        1. Checks authentication.
        2. Forwards the SDK command to the drone via TelloBridge.
        3. Waits for "ok" or "error" from the drone.
        4. Returns the result to the client.

    Why CON for commands?
        Flight commands must be delivered reliably. CON messages are
        retransmitted with exponential backoff if no ACK is received.
        This matches the Week 6 lecture: use CON for critical messages,
        NON for high-frequency telemetry.

    TelloBridge reference:
        The bridge instance is passed in at server startup (from main.py).
        This avoids a circular import and keeps drone.py decoupled from
        the CoAP layer.
    """

    def __init__(self, bridge, sdk_command: str):
        """
        Args:
            bridge: TelloBridge instance (from drone.py).
            sdk_command: The exact Tello SDK string to send (e.g. "takeoff").
                         For parameterised commands like "move", the payload
                         overrides this. See MoveResource below.
        """
        super().__init__()
        self.bridge = bridge
        self.sdk_command = sdk_command

    async def render_post(self, request):
        # Auth check — same pattern as telemetry resources.
        device_id = None
        for opt in request.opt.uri_query:
            if opt.startswith("device_id="):
                device_id = opt.split("=", 1)[1]

        if not device_id or not require_auth(device_id):
            return aiocoap.Message(
                code=aiocoap.UNAUTHORIZED,
                payload=b'{"error":"not authenticated"}'
            )

        # send_command() is synchronous (blocking socket call).
        # We run it in a thread pool executor so it doesn't block the
        # asyncio event loop — other coroutines can run while we wait
        # for the drone's "ok" response.
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,  # default thread pool
            self.bridge.send_command,
            self.sdk_command
        )

        logger.info("Command '%s' → drone response: '%s'", self.sdk_command, response)

        if response == "ok":
            return aiocoap.Message(
                code=aiocoap.CHANGED,
                payload=json.dumps({
                    "status": "ok",
                    "command": self.sdk_command,
                    "drone_response": response
                }).encode()
            )
        else:
            # "error" or "timeout" — return 5.00 Internal Server Error.
            return aiocoap.Message(
                code=aiocoap.INTERNAL_SERVER_ERROR,
                payload=json.dumps({
                    "status": "error",
                    "command": self.sdk_command,
                    "drone_response": response
                }).encode()
            )


class MoveResource(resource.Resource):
    """
    POST /drone/command/move
    Accepts a JSON payload specifying direction and distance.

    Expected payload:
        {"direction": "up", "distance": 50}
        {"direction": "forward", "distance": 100}
        {"direction": "cw", "distance": 90}   ← rotation in degrees

    Valid directions: up, down, left, right, forward, back, cw, ccw

    Translates to Tello SDK command: "up 50", "forward 100", "cw 90" etc.
    Distance range: 20–500 cm (Tello SDK limit).
    """

    VALID_DIRECTIONS = {"up", "down", "left", "right", "forward", "back", "cw", "ccw"}
    MIN_DIST = 20
    MAX_DIST = 500

    def __init__(self, bridge):
        super().__init__()
        self.bridge = bridge

    async def render_post(self, request):
        # Auth check.
        device_id = None
        for opt in request.opt.uri_query:
            if opt.startswith("device_id="):
                device_id = opt.split("=", 1)[1]

        if not device_id or not require_auth(device_id):
            return aiocoap.Message(
                code=aiocoap.UNAUTHORIZED,
                payload=b'{"error":"not authenticated"}'
            )

        # Parse JSON payload.
        try:
            body = json.loads(request.payload.decode())
            direction = body.get("direction", "").lower()
            distance = int(body.get("distance", 0))
        except (json.JSONDecodeError, ValueError):
            return aiocoap.Message(
                code=aiocoap.BAD_REQUEST,
                payload=b'{"error":"invalid JSON payload"}'
            )

        # Validate direction and distance.
        if direction not in self.VALID_DIRECTIONS:
            return aiocoap.Message(
                code=aiocoap.BAD_REQUEST,
                payload=json.dumps({
                    "error": f"invalid direction '{direction}'",
                    "valid": list(self.VALID_DIRECTIONS)
                }).encode()
            )

        if not (self.MIN_DIST <= distance <= self.MAX_DIST):
            return aiocoap.Message(
                code=aiocoap.BAD_REQUEST,
                payload=json.dumps({
                    "error": f"distance must be {self.MIN_DIST}–{self.MAX_DIST} cm",
                    "received": distance
                }).encode()
            )

        # Build and send SDK command.
        sdk_cmd = f"{direction} {distance}"
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, self.bridge.send_command, sdk_cmd)

        logger.info("Move command '%s' → drone response: '%s'", sdk_cmd, response)

        code = aiocoap.CHANGED if response == "ok" else aiocoap.INTERNAL_SERVER_ERROR
        return aiocoap.Message(
            code=code,
            payload=json.dumps({
                "status": "ok" if response == "ok" else "error",
                "command": sdk_cmd,
                "drone_response": response
            }).encode()
        )


# ─────────────────────────────────────────────
# SERVER STARTUP
# ─────────────────────────────────────────────

async def create_server(bridge):
    """
    Build the CoAP resource tree and start the server.

    aiocoap uses a Site object as the root of the resource tree.
    Resources are added with site.add_resource(path_tuple, resource_instance).
    Path tuples map directly to URI paths:
        ("drone", "telemetry", "battery") → coap://host/drone/telemetry/battery

    Args:
        bridge: TelloBridge instance from drone.py.
                Passed through to command resources so they can send SDK commands.

    Returns:
        The running aiocoap server context (kept alive in main.py).
    """

    # ── Auth resources ────────────────────────────────────────────────
    # auth_init is instantiated first because steps 2–4 hold a reference
    # to it to read pending_device_id across the multi-step handshake.
    auth_init = AuthInitResource()

    # ── Telemetry resources ───────────────────────────────────────────
    battery_res     = BatteryResource()
    height_res      = HeightResource()
    temperature_res = TemperatureResource()
    orientation_res = OrientationResource()
    velocity_res    = VelocityResource()
    acceleration_res = AccelerationResource()
    tof_res         = TofResource()

    # ── Command resources ─────────────────────────────────────────────
    takeoff_res  = CommandResource(bridge, "takeoff")
    land_res     = CommandResource(bridge, "land")
    emergency_res = CommandResource(bridge, "emergency")
    move_res     = MoveResource(bridge)

    # ── Build the resource tree ───────────────────────────────────────
    root = resource.Site()

    # Auth endpoints
    root.add_resource(("auth", "init"),      auth_init)
    root.add_resource(("auth", "challenge"), AuthChallengeResource(auth_init))
    root.add_resource(("auth", "verify"),    AuthVerifyResource(auth_init))
    root.add_resource(("auth", "confirm"),   AuthConfirmResource(auth_init))

    # Telemetry endpoints
    root.add_resource(("drone", "telemetry", "battery"),      battery_res)
    root.add_resource(("drone", "telemetry", "height"),       height_res)
    root.add_resource(("drone", "telemetry", "temperature"),  temperature_res)
    root.add_resource(("drone", "telemetry", "orientation"),  orientation_res)
    root.add_resource(("drone", "telemetry", "velocity"),     velocity_res)
    root.add_resource(("drone", "telemetry", "acceleration"), acceleration_res)
    root.add_resource(("drone", "telemetry", "tof"),          tof_res)

    # Command endpoints
    root.add_resource(("drone", "command", "takeoff"),   takeoff_res)
    root.add_resource(("drone", "command", "land"),      land_res)
    root.add_resource(("drone", "command", "emergency"), emergency_res)
    root.add_resource(("drone", "command", "move"),      move_res)

    # ── Start Observe notification tasks ─────────────────────────────
    # Each observable resource needs a background task that periodically
    # calls updated_state() to push new values to subscribed clients.
    loop = asyncio.get_event_loop()
    for res in [battery_res, height_res, temperature_res,
                orientation_res, velocity_res, acceleration_res, tof_res]:
        res.start_notify_task(loop)

    # ── Bind and start the CoAP server ───────────────────────────────
    # aiocoap.Context.create_server_context() binds a UDP socket to
    # COAP_HOST:COAP_PORT and starts listening for incoming CoAP messages.
    # This is the CoAP equivalent of app.run() in Flask.
    server_context = await aiocoap.Context.create_server_context(
        root,
        bind=(COAP_HOST, COAP_PORT)
    )

    logger.info("CoAP server listening on coap://%s:%d", COAP_HOST, COAP_PORT)
    return server_context