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
    # Latest single values
    pitch: float = 0.0
    roll: float = 0.0
    yaw: float = 0.0
    vgx: float = 0.0
    vgy: float = 0.0
    vgz: float = 0.0
    templ: float = 0.0
    temph: float = 0.0
    tof: float = 0.0        # time-of-flight distance to ground (cm)
    height: float = 0.0
    battery: float = 0.0
    baro: float = 0.0
    agx: float = 0.0
    agy: float = 0.0
    agz: float = 0.0
    connected: bool = False

    # Rolling history for dashboard plots (last 100 samples)
    battery_history: deque = field(default_factory=lambda: deque(maxlen=100))
    height_history: deque = field(default_factory=lambda: deque(maxlen=100))
    temp_history: deque = field(default_factory=lambda: deque(maxlen=100))
    accel_history: deque = field(default_factory=lambda: deque(maxlen=100))


# Global singleton + its lock
telemetry = TelemetryState()
lock = threading.Lock()


def update(parsed: dict):
    """Called by drone.py every time a new telemetry packet arrives."""
    with lock:
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
        telemetry.connected = True

        # Append to histories
        telemetry.battery_history.append(telemetry.battery)
        telemetry.height_history.append(telemetry.height)
        telemetry.temp_history.append(telemetry.temph)
        
        # Magnitude of acceleration vector
        import math
        accel_mag = math.sqrt(telemetry.agx**2 + telemetry.agy**2 + telemetry.agz**2)
        telemetry.accel_history.append(accel_mag)


def snapshot() -> dict:
    """Returns a clean dict copy of latest telemetry — safe to read from any thread."""
    with lock:
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