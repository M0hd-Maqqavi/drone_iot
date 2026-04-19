# coap/client.py
"""
CoAP Client — Application Layer.

This is the client-side of the IoT system. It:
    1. Performs the 4-step mutual authentication handshake with the server.
    2. Subscribes to all telemetry resources via CoAP Observe.
    3. Receives push notifications and writes them to a local data store
       that the dashboard reads from.
    4. Sends flight commands to the server on demand.
    5. Handles CON alert messages (low battery, altitude exceeded).

Architecture reminder:
    This module runs entirely on the CLIENT machine.
    It never talks to the drone directly — only to the CoAP server.
    The drone, SDK, and shared state are all on the server side.

    [CoAP Server] ──CoAP/UDP──► [This client] ──► [Dashboard]

Async model:
    The client uses asyncio, same as the server. This means:
    - All network calls are `await`-ed — non-blocking.
    - Multiple Observe subscriptions run concurrently as asyncio Tasks.
    - The dashboard runs in the main thread; the client runs in an
      asyncio event loop in a background thread (wired up in main.py).

Data flow to dashboard:
    The client writes received telemetry into ClientState (defined below).
    The dashboard reads from ClientState directly.
    A threading.Lock protects concurrent access — same pattern as shared/state.py.
    This is correct: the dashboard is PART of the client, not a separate node.
"""

import json
import asyncio
import logging
import threading
from collections import deque

import aiocoap

from auth.handshake import AuthClient
from config import COAP_HOST, COAP_PORT, DEVICE_ID

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# CLIENT-SIDE TELEMETRY STORE
# ─────────────────────────────────────────────

class ClientState:
    """
    Thread-safe telemetry store on the CLIENT side.

    Why a separate store from shared/state.py?
        shared/state.py lives on the SERVER (gateway) — it's populated
        directly by the drone's UDP telemetry. The client never imports it.
        ClientState is populated by CoAP Observe notifications — the data
        has travelled: drone → SDK → gateway → CoAP → here.

    The dashboard reads from this object, not from shared/state.py.
    This maintains the correct architecture: client only knows CoAP.
    """

    def __init__(self):
        self._lock = threading.Lock()

        # Latest values (overwritten on each CoAP notification)
        self.battery     = 0.0
        self.height      = 0.0
        self.tof         = 0.0
        self.templ       = 0.0
        self.temph       = 0.0
        self.pitch       = 0.0
        self.roll        = 0.0
        self.yaw         = 0.0
        self.vgx         = 0.0
        self.vgy         = 0.0
        self.vgz         = 0.0
        self.agx         = 0.0
        self.agy         = 0.0
        self.agz         = 0.0
        self.connected   = False
        self.alerts      = []   # list of alert strings shown on dashboard
        self.tilt_alert = False

        # Rolling histories for dashboard charts (last 100 samples)
        self.battery_history     = deque(maxlen=100)
        self.height_history      = deque(maxlen=100)
        self.temp_history        = deque(maxlen=100)
        self.accel_history       = deque(maxlen=100)

    def update_battery(self, data: dict):
        with self._lock:
            self.battery = data.get("battery", self.battery)
            self.battery_history.append(self.battery)
            if data.get("alert"):
                alert_msg = f"⚠ LOW BATTERY: {self.battery:.0f}%"
                if alert_msg not in self.alerts:
                    self.alerts.append(alert_msg)
                    logger.warning(alert_msg)

    def update_height(self, data: dict):
        with self._lock:
            self.height = data.get("height", self.height)
            self.tof    = data.get("tof",    self.tof)
            self.height_history.append(self.height)
            if data.get("alert"):
                alert_msg = f"⚠ ALTITUDE EXCEEDED: {self.height:.0f}cm"
                if alert_msg not in self.alerts:
                    self.alerts.append(alert_msg)
                    logger.warning(alert_msg)

    def update_temperature(self, data: dict):
        with self._lock:
            self.templ = data.get("templ", self.templ)
            self.temph = data.get("temph", self.temph)
            self.temp_history.append(self.temph)

    def update_orientation(self, data: dict):
        with self._lock:
            self.pitch = data.get("pitch", self.pitch)
            self.roll  = data.get("roll",  self.roll)
            self.yaw   = data.get("yaw",   self.yaw)
            self.tilt_alert = data.get("tilt_alert", False)
            
            if self.tilt_alert:
                msg = f"⚠ DRONE NOT LEVEL — P:{self.pitch:+.1f}° R:{self.roll:+.1f}°"
                self.alerts = [a for a in self.alerts if "NOT LEVEL" not in a]
                self.alerts.append(msg)
            else:
                self.alerts = [a for a in self.alerts if "NOT LEVEL" not in a]

    def update_velocity(self, data: dict):
        with self._lock:
            self.vgx = data.get("vgx", self.vgx)
            self.vgy = data.get("vgy", self.vgy)
            self.vgz = data.get("vgz", self.vgz)

    def update_acceleration(self, data: dict):
        import math
        with self._lock:
            self.agx = data.get("agx", self.agx)
            self.agy = data.get("agy", self.agy)
            self.agz = data.get("agz", self.agz)
            mag = math.sqrt(self.agx**2 + self.agy**2 + self.agz**2)
            self.accel_history.append(mag)

    def snapshot(self) -> dict:
        """Thread-safe copy of latest values for the dashboard."""
        with self._lock:
            return {
                "battery":  self.battery,
                "height":   self.height,
                "tof":      self.tof,
                "templ":    self.templ,
                "temph":    self.temph,
                "pitch":    self.pitch,
                "roll":     self.roll,
                "yaw":      self.yaw,
                "vgx":      self.vgx,
                "vgy":      self.vgy,
                "vgz":      self.vgz,
                "agx":      self.agx,
                "agy":      self.agy,
                "agz":      self.agz,
                "connected": self.connected,
                "alerts":   list(self.alerts),
            }


# Global client state instance — dashboard imports this directly.
client_state = ClientState()


# ─────────────────────────────────────────────
# COAP CLIENT
# ─────────────────────────────────────────────

class DroneCoAPClient:
    """
    Handles all CoAP communication from the client side.

    Responsibilities:
        - Build the server base URI from config.
        - Run the 4-step auth handshake.
        - Subscribe to all telemetry resources via Observe.
        - Send flight commands and return drone responses.

    Usage (from main.py):
        client = DroneCoAPClient()
        await client.start()          # auth + subscribe
        await client.takeoff()        # send command
        await client.land()           # send command
        await client.move("up", 50)   # send command
        await client.stop()           # clean shutdown
    """

    def __init__(self):
        self.base_uri = f"coap://{COAP_HOST}:{COAP_PORT}"
        self.auth     = AuthClient()
        self.context  = None   # aiocoap context, set in start()

        # URI query suffix appended to every request for auth checking on server.
        # Format: ?device_id=drone-monitor-client-01
        self._qstring = f"device_id={DEVICE_ID}"

    # ── LIFECYCLE ────────────────────────────────────────────────────

    async def start(self):
        """
        Create the aiocoap context, run auth handshake, subscribe to telemetry.
        Call this once at startup before sending any commands.
        """
        # aiocoap.Context.create_client_context() sets up the UDP socket
        # and the CoAP message engine (retransmission, deduplication, etc.)
        self.context = await aiocoap.Context.create_client_context()
        logger.info("CoAP client context created.")

        # Must complete auth before telemetry or commands will be rejected.
        success = await self._run_auth_handshake()
        if not success:
            raise RuntimeError("Authentication failed — cannot proceed.")

        # Subscribe to all telemetry resources.
        await self._subscribe_all()
        logger.info("Client fully initialised — receiving telemetry.")

    async def stop(self):
        """Cleanly shut down the aiocoap context."""
        if hasattr(self, "_observe_tasks"):
            for task in self._observe_tasks:
                task.cancel()
        if self.context:
            await self.context.shutdown()
            logger.info("CoAP client shut down.")

    # ── AUTH HANDSHAKE ───────────────────────────────────────────────

    async def _run_auth_handshake(self) -> bool:
        """
        Execute all 4 steps of the mutual authentication handshake.

        Each step is a separate CoAP request to a dedicated auth resource.
        Steps must run in order — the server enforces state machine transitions.

        Returns True if all 4 steps succeed, False otherwise.
        """
        logger.info("Starting 4-step mutual authentication handshake...")

        # ── Step 1: Client → Server (Device ID) ──────────────────────
        # POST /auth/init with Device ID as JSON payload.
        # CON message — we must receive an ACK before proceeding.
        step1_payload = self.auth.step1_initiate()
        req = aiocoap.Message(
            code=aiocoap.POST,
            uri=f"{self.base_uri}/auth/init",
            payload=step1_payload
        )
        try:
            resp = await self.context.request(req).response
            if resp.code != aiocoap.CREATED:
                logger.error("Step 1 failed: server returned %s", resp.code)
                return False
            logger.info("Step 1: OK — session initiated.")
        except Exception as e:
            logger.error("Step 1 exception: %s", e)
            return False

        # ── Step 2: Server → Client (encrypted challenge) ────────────
        # GET /auth/challenge — server returns AES{λi, (ψ | ηserver)}.
        req = aiocoap.Message(
            code=aiocoap.GET,
            uri=f"{self.base_uri}/auth/challenge"
        )
        try:
            resp = await self.context.request(req).response
            if resp.code != aiocoap.CONTENT:
                logger.error("Step 2 failed: server returned %s", resp.code)
                return False

            # Decrypt challenge, recover μkey and ηserver.
            success = self.auth.step2_process_challenge(resp.payload)
            if not success:
                logger.error("Step 2 failed: could not decrypt challenge.")
                return False
            logger.info("Step 2: OK — challenge decrypted, μkey recovered.")
        except Exception as e:
            logger.error("Step 2 exception: %s", e)
            return False

        # ── Step 3: Client → Server (client response + challenge) ────
        # POST /auth/verify with AES{μkey, (Y | ηclient)}.
        step3_payload = self.auth.step3_respond_and_challenge()
        req = aiocoap.Message(
            code=aiocoap.POST,
            uri=f"{self.base_uri}/auth/verify",
            payload=step3_payload
        )
        try:
            resp = await self.context.request(req).response
            if resp.code != aiocoap.CHANGED:
                logger.error("Step 3 failed: server returned %s", resp.code)
                return False
            logger.info("Step 3: OK — client identity verified by server.")
        except Exception as e:
            logger.error("Step 3 exception: %s", e)
            return False

        # ── Step 4: Server → Client (server proof) ───────────────────
        # GET /auth/confirm — server returns AES{λi, (ηclient | μkey)}.
        req = aiocoap.Message(
            code=aiocoap.GET,
            uri=f"{self.base_uri}/auth/confirm"
        )
        try:
            resp = await self.context.request(req).response
            if resp.code != aiocoap.CONTENT:
                logger.error("Step 4 failed: server returned %s", resp.code)
                return False

            # Verify server's response — confirm mutual authentication.
            success = self.auth.step4_verify_server(resp.payload)
            if not success:
                logger.error("Step 4 failed: server identity NOT verified.")
                return False
            logger.info("Step 4: OK — server verified. MUTUAL AUTH COMPLETE ✓")
        except Exception as e:
            logger.error("Step 4 exception: %s", e)
            return False

        client_state.connected = True
        return True

    # ── OBSERVE SUBSCRIPTIONS ────────────────────────────────────────

    async def _subscribe_all(self):
        """
        Subscribe to all 7 telemetry resources via CoAP Observe.

        Each subscription is launched as a separate asyncio Task so they
        all run concurrently — we don't wait for one to finish before
        starting the next.

        How Observe works in aiocoap:
            request = self.context.request(msg)
            # .response gives the FIRST response (initial GET reply)
            # .observation gives an async iterator of subsequent notifications
            async for notification in request.observation:
                process(notification.payload)
        """
        self._observe_tasks = []
        
        resources = [
            ("battery",      client_state.update_battery),
            ("height",       client_state.update_height),
            ("temperature",  client_state.update_temperature),
            ("orientation",  client_state.update_orientation),
            ("velocity",     client_state.update_velocity),
            ("acceleration", client_state.update_acceleration),
            ("tof",          lambda d: None),
        ]

        for name, update_fn in resources:
            uri = f"{self.base_uri}/drone/telemetry/{name}?{self._qstring}"
            # Store reference — prevents garbage collection
            task = asyncio.create_task(
                self._observe_resource(uri, name, update_fn)
            )
            self._observe_tasks.append(task)
            logger.info("Subscribed to: %s", uri)

    async def _observe_resource(self, uri: str, name: str, update_fn):
        """
        Single resource observation loop.

        Sends a GET with Observe=0 to the server, then listens for
        push notifications indefinitely.

        Each notification payload is JSON — decoded and passed to update_fn
        which writes the values into client_state.

        If the observation is cancelled (server unreachable, network error),
        logs the error and exits. The main loop in main.py can restart the
        client if needed.

        Args:
            uri:       Full CoAP URI including query string.
            name:      Resource name (for logging).
            update_fn: client_state method to call with parsed JSON data.
        """
        msg = aiocoap.Message(
            code=aiocoap.GET,
            uri=uri,
            observe=0   # Observe=0 means "register me for updates"
        )

        try:
            req = self.context.request(msg)

            # First response — the initial GET reply from the server.
            first_response = await req.response
            if first_response.code == aiocoap.CONTENT:
                data = json.loads(first_response.payload.decode())
                update_fn(data)
                logger.debug("Initial %s: %s", name, data)

            # Subsequent responses — pushed by server whenever state changes.
            # This async for loop runs indefinitely until observation ends.
            async for notification in req.observation:
                try:
                    data = json.loads(notification.payload.decode())
                    update_fn(data)
                    logger.debug("Observe %s: %s", name, data)
                except json.JSONDecodeError as e:
                    logger.warning("Bad JSON from %s: %s", name, e)

        except asyncio.CancelledError:
            logger.info("Observation of %s cancelled.", name)
        except Exception as e:
            logger.error("Observation of %s failed: %s", name, e)

    # ── FLIGHT COMMANDS ──────────────────────────────────────────────

    async def _send_command(self, path: str, payload: dict = None) -> dict:
        """
        Send a flight command to the server via CoAP POST.

        Uses CON (Confirmable) message — server must ACK.
        aiocoap handles retransmission with exponential backoff automatically
        if the ACK is not received (up to MAX_RETRANSMIT=4 attempts).

        Args:
            path:    URI path after /drone/command/ (e.g. "takeoff").
            payload: Optional JSON body (used for move commands).

        Returns:
            dict with "status", "command", and "drone_response" keys.
        """
        uri = f"{self.base_uri}/drone/command/{path}?{self._qstring}"
        body = json.dumps(payload).encode() if payload else b""

        msg = aiocoap.Message(
            code=aiocoap.POST,
            uri=uri,
            payload=body
        )
        # aiocoap sends POST as CON by default when using the request() method.
        # No need to manually set mtype — the library manages this.

        try:
            resp = await self.context.request(msg).response
            result = json.loads(resp.payload.decode())
            logger.info("Command '%s': %s", path, result)
            return result
        except Exception as e:
            logger.error("Command '%s' failed: %s", path, e)
            return {"status": "error", "reason": str(e)}

    # ── PUBLIC COMMAND API ───────────────────────────────────────────
    # These are the methods main.py (and any future UI) call directly.
    # Each one is a thin wrapper around _send_command().

    async def takeoff(self) -> dict:
        """Command the drone to take off autonomously to ~80cm."""
        return await self._send_command("takeoff")

    async def land(self) -> dict:
        """Command the drone to land immediately."""
        return await self._send_command("land")

    async def emergency(self) -> dict:
        """
        Cut all motors immediately.
        USE WITH CAUTION — drone will drop from the air instantly.
        Only use if drone is behaving dangerously.
        """
        return await self._send_command("emergency")

    async def move(self, direction: str, distance: int) -> dict:
        """
        Move the drone in a direction by a given distance.

        Args:
            direction: one of up/down/left/right/forward/back/cw/ccw
            distance:  20–500 cm (or degrees for cw/ccw)
        """
        return await self._send_command("move", {
            "direction": direction,
            "distance":  distance
        })