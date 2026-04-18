# shared/state.py
# Thread-safe store for the latest drone telemetry.
# drone.py writes to it, coap server reads from it, dashboard reads from it.
# We use threading.Lock because multiple threads touch this simultaneously.

import threading
from dataclasses import dataclass, field
from typing import Optional
from collections import deque

@dataclass
class TelemetryState:
    """Shared in-memory telemetry cache plus short rolling histories."""

    # Latest single values from the most recent telemetry packet.
    # These are overwritten on each update.

    # Orientation (degrees)
    pitch: float = 0.0  # The Orientation of the drone around the front-to-back axis. Positive values indicate the drone's nose is tilted upward, negative values indicate the nose is tilted downward.
    roll: float = 0.0   # The Orientation of the drone around the left-to-right axis. Positive values indicate the drone is tilted to the right, negative values indicate the drone is tilted to the left.
    yaw: float = 0.0    # The Orientation of the drone around the vertical axis. Positive values indicate the drone is rotated clockwise, negative values indicate the drone is rotated counterclockwise.

    # Velocity (cm/s)
    vgx: float = 0.0    # The drone's forward/backward velocity. Positive values indicate forward movement, negative values indicate backward movement.
    vgy: float = 0.0    # The drone's left/right velocity. Positive values indicate movement to the right, negative values indicate movement to the left.
    vgz: float = 0.0    # The drone's up/down velocity. Positive values indicate movement upward, negative values indicate movement downward.

    # Temperature (C)
    templ: float = 0.0  # The drone's temperature as measured by the lower bound.
    temph: float = 0.0  # The drone's temperature as measured by the upper bound.

    # Distance / altitude
    tof: float = 0.0      # time-of-flight distance to ground (cm)
    height: float = 0.0   # The drone's current altitude above the takeoff point (cm).

    # Power / pressure
    battery: float = 0.0  # The drone's current battery level as a percentage. 
    
    # Acceleration components
    agx: float = 0.0    # The drone's acceleration along the x-axis (forward/backward). Positive values indicate acceleration in the forward direction, negative values indicate acceleration in the backward direction.
    agy: float = 0.0    # The drone's acceleration along the y-axis (left/right). Positive values indicate acceleration to the right, negative values indicate acceleration to the left.
    agz: float = 0.0    # The drone's acceleration along the z-axis (up/down). Positive values indicate acceleration upward, negative values indicate acceleration downward.

    # True once at least one valid packet has been parsed.
    connected: bool = False  # Indicates whether the drone is currently connected and transmitting telemetry data.

    # Rolling history for dashboard plots (last 100 samples).
    # Keeping this capped prevents unbounded memory growth.
    # Histories are append-only, one sample per parsed telemetry packet.
    battery_history: deque = field(default_factory=lambda: deque(maxlen=100))
    height_history: deque = field(default_factory=lambda: deque(maxlen=100))
    temp_history: deque = field(default_factory=lambda: deque(maxlen=100))
    accel_history: deque = field(default_factory=lambda: deque(maxlen=100))


# Global singleton + its lock
# Every module imports this same instance, so all telemetry consumers stay in sync.
telemetry = TelemetryState()
lock = threading.Lock()


def update(parsed: dict):
    """Called by drone.py every time a new telemetry packet arrives."""
    # Lock the full write so readers never observe a half-updated packet.
    with lock:
        # Each key defaults to 0 so occasional missing fields do not crash updates.
        # Keys mirror names from the parsed Tello state message.
        telemetry.pitch   = float(parsed.get("pitch", 0))
        telemetry.roll    = float(parsed.get("roll", 0))
        telemetry.yaw     = float(parsed.get("yaw", 0))
        telemetry.vgx     = float(parsed.get("vgx", 0))
        telemetry.vgy     = float(parsed.get("vgy", 0))
        telemetry.vgz     = float(parsed.get("vgz", 0))
        telemetry.templ   = float(parsed.get("templ", 0))
        telemetry.temph   = float(parsed.get("temph", 0))
        telemetry.tof     = float(parsed.get("tof", 0))
        telemetry.height  = float(parsed.get("h", 0))
        telemetry.battery = float(parsed.get("bat", 0))
        telemetry.baro    = float(parsed.get("baro", 0))
        telemetry.agx     = float(parsed.get("agx", 0))
        telemetry.agy     = float(parsed.get("agy", 0))
        telemetry.agz     = float(parsed.get("agz", 0))
        # Mark link as healthy after successfully parsing a packet.
        telemetry.connected = True

        # Append selected fields used by dashboard charts.
        telemetry.battery_history.append(telemetry.battery)
        telemetry.height_history.append(telemetry.height)
        # Use temph (upper chip temperature) as a conservative heat signal.
        telemetry.temp_history.append(telemetry.temph)
        
        # Store acceleration magnitude so the dashboard can show one stable signal.
        import math
        accel_mag = math.sqrt(telemetry.agx**2 + telemetry.agy**2 + telemetry.agz**2)
        telemetry.accel_history.append(accel_mag)


def snapshot() -> dict:
    """Returns a clean dict copy of latest telemetry — safe to read from any thread."""
    with lock:
        # Return a plain dict copy to avoid exposing mutable shared state.
        # Histories are intentionally excluded to keep this snapshot lightweight.
        return {
            "pitch": telemetry.pitch,
            "roll": telemetry.roll,
            "yaw": telemetry.yaw,
            "height": telemetry.height,
            "battery": telemetry.battery,
            "baro": telemetry.baro,
            "tof": telemetry.tof,
            "templ": telemetry.templ,
            "temph": telemetry.temph,
            "agx": telemetry.agx,
            "agy": telemetry.agy,
            "agz": telemetry.agz,
            "connected": telemetry.connected,
        }