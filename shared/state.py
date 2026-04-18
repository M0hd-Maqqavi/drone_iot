# shared/state.py
"""
Thread-safe in-memory telemetry store.

Why does this file exist as a separate module?
    The drone telemetry arrives on a background thread (drone.py).
    The CoAP server reads it on another thread.
    The dashboard reads it on the main thread.
    Three threads, one data store — without a lock, two threads could
    read/write simultaneously and produce corrupted or half-updated values.
    This module is the single controlled access point for all telemetry.

Design decisions:
    - Global singleton (telemetry + lock): every module that imports this
      file gets the SAME instance. No passing objects around.
    - threading.Lock: a simple mutual-exclusion lock. Only one thread
      can be inside a 'with lock:' block at a time. Others wait.
    - deque(maxlen=100): a fixed-size ring buffer. When it fills up,
      the oldest entry is automatically discarded. Prevents unbounded
      memory growth during long flights.
    - snapshot(): returns a plain dict copy, not a reference to the
      live object. Callers can safely read it after releasing the lock.
"""

import math
import threading
from collections import deque
from dataclasses import dataclass, field


# ─────────────────────────────────────────────
# TELEMETRY DATA STRUCTURE
# ─────────────────────────────────────────────

@dataclass
class TelemetryState:
    """
    Holds the most recent values from every field in the Tello state packet,
    plus rolling histories used by the dashboard charts.

    The Tello broadcasts a semicolon-delimited string every ~100ms, e.g.:
        pitch:-1;roll:0;yaw:3;vgx:0;vgy:0;vgz:0;templ:63;temph:66;
        tof:10;h:0;bat:87;baro:56.32;time:0;agx:-7.00;agy:-1.00;agz:-1000.00;

    Each field below corresponds directly to one key in that string.
    All numeric fields default to 0.0 so the object is valid before
    the first packet arrives.
    """

    # ── ORIENTATION (degrees) ────────────────────────────────────────
    # The drone's angular position in 3D space.
    # These three angles together fully describe which way the drone is facing
    # and how it is tilted — essential for understanding flight stability.

    pitch: float = 0.0
    # Rotation around the left-right axis (nose up/down).
    # Positive → nose tilted upward. Negative → nose tilted downward.
    # A hovering drone should read close to 0. Forward flight → negative pitch.

    roll: float = 0.0
    # Rotation around the front-back axis (left/right tilt).
    # Positive → tilted to the right. Negative → tilted to the left.
    # Sideways flight produces non-zero roll.

    yaw: float = 0.0
    # Rotation around the vertical axis (compass heading).
    # Positive → clockwise rotation. Negative → counterclockwise.
    # Full 360° turn sweeps through +180 and wraps to -180.

    # ── VELOCITY (cm/s) ──────────────────────────────────────────────
    # Ground-frame velocity components. These describe how fast and in what
    # direction the drone is actually moving through the air, not just
    # which way it is pointed.

    vgx: float = 0.0
    # Forward (+) / backward (-) velocity in cm/s.

    vgy: float = 0.0
    # Right (+) / left (-) velocity in cm/s.

    vgz: float = 0.0
    # Upward (+) / downward (-) velocity in cm/s.

    # ── TEMPERATURE (°C) ─────────────────────────────────────────────
    # The Tello reports a temperature RANGE rather than a single value.
    # This reflects the internal chip temperature, not ambient air temperature.
    # High temperatures can indicate the motors or ESCs are overheating.

    templ: float = 0.0   # Lower bound of the temperature range (°C).
    temph: float = 0.0   # Upper bound of the temperature range (°C).
    # We use temph in histories as the conservative (worst-case) heat signal.

    # ── DISTANCE / ALTITUDE ──────────────────────────────────────────

    tof: float = 0.0
    # Time-of-Flight sensor reading: distance from the drone's belly to the
    # ground directly below it (cm). Uses infrared laser ranging.
    # More reliable than barometric height at low altitudes (<2m).

    height: float = 0.0
    # Barometric altitude above the takeoff point (cm).
    # Computed from air pressure changes since takeoff.
    # More reliable than tof at higher altitudes.
    # Maps to the "h" key in the raw Tello state string.

    # ── POWER ────────────────────────────────────────────────────────

    battery: float = 0.0
    # Remaining battery charge as a percentage (0–100).
    # Maps to the "bat" key in the raw Tello state string.
    # A CoAP CON alert fires when this drops to BATTERY_ALERT_THRESHOLD.

    baro: float = 0.0
    # Raw barometric pressure reading (cm, relative).
    # Used internally by the drone for altitude hold — exposed here for logging.

    # ── ACCELERATION (cm/s²) ─────────────────────────────────────────
    # Raw accelerometer readings from the IMU (Inertial Measurement Unit).
    # These reflect forces acting on the drone, including gravity (~980 cm/s²
    # downward) and any movement-induced forces.

    agx: float = 0.0   # Acceleration along the X axis (forward/backward).
    agy: float = 0.0   # Acceleration along the Y axis (left/right).
    agz: float = 0.0    # Acceleration along the Z axis (up/down).
    # At rest on the ground this reads ~-1000 cm/s² (1g downward due to gravity).

    # ── CONNECTION FLAG ──────────────────────────────────────────────

    connected: bool = False
    # False until the first valid telemetry packet has been parsed and stored.
    # The CoAP server and dashboard use this to show a "waiting for drone"
    # state rather than displaying misleading zeros.

    # ── ROLLING HISTORIES (for dashboard charts) ─────────────────────
    # deque with maxlen=100 acts as a circular buffer.
    # Each telemetry update appends one value. Once full (100 entries),
    # the oldest entry is silently dropped to make room for the new one.
    # This gives us ~10 seconds of history at 100ms intervals.
    # field(default_factory=...) is required for mutable defaults in dataclasses —
    # without it every instance would share the SAME deque object.

    battery_history: deque = field(default_factory=lambda: deque(maxlen=100))
    height_history:  deque = field(default_factory=lambda: deque(maxlen=100))
    temp_history:    deque = field(default_factory=lambda: deque(maxlen=100))
    accel_history:   deque = field(default_factory=lambda: deque(maxlen=100))
    # accel_history stores the scalar magnitude √(agx²+agy²+agz²) rather than
    # individual axes — one stable signal is cleaner to plot than three noisy ones.


# ─────────────────────────────────────────────
# GLOBAL SINGLETON
# ─────────────────────────────────────────────

# Single shared instance. Every module that does `import shared.state as state`
# gets a reference to this exact object. Python's module system guarantees it
# is only created once per interpreter process.
telemetry = TelemetryState()

# The mutex (mutual exclusion lock) that protects telemetry from concurrent access.
# Rule: ANY read or write to telemetry fields must happen inside `with lock:`.
lock = threading.Lock()

# ─────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────

def update(parsed: dict) -> None:
    """
    Overwrite all telemetry fields with values from a freshly parsed packet.
    Called by drone.py on every incoming state broadcast (~100ms interval).

    Args:
        parsed: dict produced by parse_telemetry(), e.g.
                {"pitch": "-1", "roll": "0", "bat": "87", ...}
                Values are still strings at this point — we cast to float here.

    Thread safety:
        Acquires the lock for the entire write so no reader can observe a
        state where some fields are from the new packet and others from the old one.

    Defaults:
        parsed.get(key, 0) returns 0 if a key is missing.
        This handles rare malformed packets without crashing the listener thread.
    """
    with lock:
        telemetry.pitch   = float(parsed.get("pitch",  0))
        telemetry.roll    = float(parsed.get("roll",   0))
        telemetry.yaw     = float(parsed.get("yaw",    0))
        telemetry.vgx     = float(parsed.get("vgx",    0))
        telemetry.vgy     = float(parsed.get("vgy",    0))
        telemetry.vgz     = float(parsed.get("vgz",    0))
        telemetry.templ   = float(parsed.get("templ",  0))
        telemetry.temph   = float(parsed.get("temph",  0))
        telemetry.tof     = float(parsed.get("tof",    0))
        telemetry.height  = float(parsed.get("h",      0))  # note: key is "h" not "height"
        telemetry.battery = float(parsed.get("bat",    0))  # note: key is "bat" not "battery"
        telemetry.baro    = float(parsed.get("baro",   0))
        telemetry.agx     = float(parsed.get("agx",    0))
        telemetry.agy     = float(parsed.get("agy",    0))
        telemetry.agz     = float(parsed.get("agz",    0))

        # Mark the link as healthy — at least one valid packet has arrived.
        telemetry.connected = True

        # ── Update rolling histories ─────────────────────────────────
        telemetry.battery_history.append(telemetry.battery)
        telemetry.height_history.append(telemetry.height)
        telemetry.temp_history.append(telemetry.temph)   # conservative upper temp

        # Scalar acceleration magnitude: √(agx² + agy² + agz²)
        # At rest: reads ~1000 cm/s² (gravity). During flight: higher values
        # indicate manoeuvres or vibration. Single value is easier to plot.
        accel_mag = math.sqrt(
            telemetry.agx ** 2 +
            telemetry.agy ** 2 +
            telemetry.agz ** 2
        )
        telemetry.accel_history.append(accel_mag)


def snapshot() -> dict:
    """
    Return a lightweight, lock-safe copy of the current telemetry values.

    Why return a dict instead of the TelemetryState object itself?
        Returning the live object would expose mutable shared state to callers.
        They could read it AFTER the lock is released, during a concurrent write,
        causing a data race. A plain dict copy is immutable from the caller's
        perspective once the lock is released.

    Note: Rolling histories are intentionally excluded.
        They are only needed by the dashboard, which reads them directly
        under its own lock acquisition. Including them here would make
        every CoAP resource GET copy 100-element lists unnecessarily.

    Returns:
        dict with string keys and float/bool values, safe to use from any thread.
    """
    with lock:
        return {
            "pitch":     telemetry.pitch,
            "roll":      telemetry.roll,
            "yaw":       telemetry.yaw,
            "height":    telemetry.height,
            "battery":   telemetry.battery,
            "baro":      telemetry.baro,
            "tof":       telemetry.tof,
            "templ":     telemetry.templ,
            "temph":     telemetry.temph,
            "agx":       telemetry.agx,
            "agy":       telemetry.agy,
            "agz":       telemetry.agz,
            "connected": telemetry.connected,
        }