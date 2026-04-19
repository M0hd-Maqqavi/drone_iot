# dashboard/plot.py
"""
Live Matplotlib Dashboard — Client-side visualisation.

Reads from ClientState (coap/client.py) which is populated by CoAP
Observe notifications. Never reads from shared/state.py — that lives
on the server side. This keeps the architecture clean.

Layout:
    ┌──────────────────────────────────────────────────────┐
    │  ALERT BAR (red, only visible when alerts exist)     │
    ├─────────────────────┬────────────────────────────────┤
    │  Battery %          │  Altitude (cm)                 │
    ├─────────────────────┼────────────────────────────────┤
    │  Temperature (°C)   │  Acceleration magnitude        │
    ├─────────────────────┴────────────────────────────────┤
    │  STATUS BAR: orientation + velocity + last command   │
    │  KEYS: [T]akeoff [L]and [U]p [D]own [F]wd [B]ack     │
    │         [R]otate CW  [E][E] Emergency                │
    └──────────────────────────────────────────────────────┘

Threading model:
    matplotlib's FuncAnimation runs on the MAIN thread.
    The CoAP client runs in an asyncio event loop on a BACKGROUND thread.
    ClientState (with its threading.Lock) is the safe bridge between them.
    The dashboard only reads ClientState — it never writes to it.

Keyboard input:
    matplotlib's key_press_event is used to capture keypresses.
    Each keypress puts a command string into a thread-safe Queue.
    The FuncAnimation update function drains the queue each frame
    and schedules the async command on the client's event loop using
    asyncio.run_coroutine_threadsafe() — the correct way to call
    async code from a synchronous (main thread) context.
"""

import asyncio
import logging
import queue
from collections import deque

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.animation import FuncAnimation

from config import DASHBOARD_UPDATE_INTERVAL, BATTERY_ALERT_THRESHOLD, ALTITUDE_ALERT_THRESHOLD

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# KEYBOARD COMMAND QUEUE
# ─────────────────────────────────────────────

# Thread-safe queue between the matplotlib key handler (main thread)
# and the command dispatcher inside FuncAnimation (also main thread,
# but needs to bridge into the asyncio loop on the background thread).
# queue.Queue is thread-safe by design — no lock needed.
_command_queue = queue.Queue()

# Tracks whether 'E' (emergency) was pressed once already.
# Emergency requires TWO consecutive presses as a safety guard —
# accidental single keypress won't cut the motors.
_emergency_primed = False
_quit_primed = False


def _on_key_press(event, client, loop):
    """
    matplotlib key_press_event callback.
    Called on the main thread whenever a key is pressed while the
    dashboard window is focused.

    Maps key → async command on DroneCoAPClient.
    Puts a coroutine into _command_queue so FuncAnimation can dispatch it.

    Args:
        event:  matplotlib KeyEvent (event.key is the key string)
        client: DroneCoAPClient instance (from coap/client.py)
        loop:   asyncio event loop running on the background thread
    """
    global _emergency_primed, _quit_primed
    key = event.key.lower() if event.key else ""

    # ── Takeoff ───────────────────────────────────────────────────────
    if key == "t":
        logger.info("Key: T → takeoff")
        asyncio.run_coroutine_threadsafe(client.takeoff(), loop)
        _command_queue.put("takeoff")
        _emergency_primed = False
        _quit_primed = False

    # ── Land ─────────────────────────────────────────────────────────
    elif key == "l":
        logger.info("Key: L → land")
        asyncio.run_coroutine_threadsafe(client.land(), loop)
        _command_queue.put("land")
        _emergency_primed = False
        _quit_primed = False

    # ── Movement (arrow keys) ─────────────────────────────────────────
    elif key == "up":
        logger.info("Key: ↑ → forward 50")
        asyncio.run_coroutine_threadsafe(client.move("forward", 50), loop)
        _command_queue.put("forward 50cm")
        _emergency_primed = False
        _quit_primed = False

    elif key == "down":
        logger.info("Key: ↓ → back 50")
        asyncio.run_coroutine_threadsafe(client.move("back", 50), loop)
        _command_queue.put("back 50cm")
        _emergency_primed = False
        _quit_primed = False

    elif key == "left":
        logger.info("Key: ← → left 50")
        asyncio.run_coroutine_threadsafe(client.move("left", 50), loop)
        _command_queue.put("left 50cm")
        _emergency_primed = False
        _quit_primed = False

    elif key == "right":
        logger.info("Key: → → right 50")
        asyncio.run_coroutine_threadsafe(client.move("right", 50), loop)
        _command_queue.put("right 50cm")
        _emergency_primed = False
        _quit_primed = False

    # ── Altitude (W/S) ────────────────────────────────────────────────
    elif key == "w":
        logger.info("Key: W → up 30")
        asyncio.run_coroutine_threadsafe(client.move("up", 30), loop)
        _command_queue.put("up 30cm")
        _emergency_primed = False
        _quit_primed = False

    elif key == "s":
        logger.info("Key: S → down 30")
        asyncio.run_coroutine_threadsafe(client.move("down", 30), loop)
        _command_queue.put("down 30cm")
        _emergency_primed = False
        _quit_primed = False

    # ── Rotation (A/D) ────────────────────────────────────────────────
    elif key == "a":
        logger.info("Key: A → ccw 20")
        asyncio.run_coroutine_threadsafe(client.move("ccw", 20), loop)
        _command_queue.put("rotate CCW 20°")
        _emergency_primed = False
        _quit_primed = False

    elif key == "d":
        logger.info("Key: D → cw 20")
        asyncio.run_coroutine_threadsafe(client.move("cw", 20), loop)
        _command_queue.put("rotate CW 20°")
        _emergency_primed = False
        _quit_primed = False

    # ── Emergency (double E) ──────────────────────────────────────────
    elif key == "e":
        if _emergency_primed:
            logger.warning("Key: E+E → EMERGENCY STOP")
            asyncio.run_coroutine_threadsafe(client.emergency(), loop)
            _command_queue.put("⚠ EMERGENCY STOP")
            _emergency_primed = False
        else:
            _emergency_primed = True
            _command_queue.put("⚠ Press E again to confirm EMERGENCY")
            logger.info("Key: E → emergency primed")
        _quit_primed = False

    # ── Quit (double Q) ───────────────────────────────────────────────
    elif key == "q":
        if _quit_primed:
            logger.info("Key: Q+Q → quit")
            asyncio.run_coroutine_threadsafe(client.land(), loop)
            _command_queue.put("landing then quitting...")
            _quit_primed = False
            # Small delay so land command fires before closing
            import threading
            threading.Timer(3.0, plt.close, args=["all"]).start()
        else:
            _quit_primed = True
            _command_queue.put("Press Q again to quit")
            logger.info("Key: Q → quit primed")
        _emergency_primed = False

    else:
        # Any unrecognised key cancels both primed states
        _emergency_primed = False
        _quit_primed = False


# ─────────────────────────────────────────────
# DASHBOARD CLASS
# ─────────────────────────────────────────────

class Dashboard:
    """
    Builds and animates the matplotlib dashboard.

    Usage (from main.py):
        dashboard = Dashboard(client, loop)
        dashboard.start()   # blocks — runs matplotlib event loop
    """

    def __init__(self, client, loop):
        """
        Args:
            client: DroneCoAPClient instance — used to send commands on keypress.
            loop:   asyncio event loop running on the background thread —
                    needed to schedule async commands from the sync main thread.
        """
        self.client = client
        self.loop   = loop

        # Track last command for status bar display
        self._last_command = "none"

        # ── Build figure ──────────────────────────────────────────────
        # figsize=(12, 7): wide enough for 2 columns, tall enough for info bars.
        # facecolor matches a dark terminal aesthetic — easier on the eyes.
        self.fig = plt.figure(figsize=(12, 7), facecolor="#1a1a2e")
        self.fig.canvas.manager.set_window_title("Drone IoT Monitor")

        # GridSpec: 4 rows × 2 cols
        # Row 0: alert bar (thin)
        # Rows 1–2: 4 chart panels (2×2)
        # Row 3: status + key hint bar (thin)
        gs = gridspec.GridSpec(
            4, 2,
            figure=self.fig,
            height_ratios=[0.08, 1, 1, 0.12],
            hspace=0.45,
            wspace=0.3,
            left=0.07, right=0.97,
            top=0.95,  bottom=0.05
        )

        # ── Alert bar (row 0, spans both columns) ─────────────────────
        self.ax_alert = self.fig.add_subplot(gs[0, :])
        self.ax_alert.set_axis_off()
        self.alert_text = self.ax_alert.text(
            0.5, 0.5, "",
            transform=self.ax_alert.transAxes,
            ha="center", va="center",
            fontsize=10, fontweight="bold",
            color="white",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="#c0392b", alpha=0.0)
        )

        # ── Chart panels ──────────────────────────────────────────────
        chart_style = dict(facecolor="#16213e")

        self.ax_battery = self.fig.add_subplot(gs[1, 0], **chart_style)
        self.ax_height  = self.fig.add_subplot(gs[1, 1], **chart_style)
        self.ax_temp    = self.fig.add_subplot(gs[2, 0], **chart_style)
        self.ax_accel   = self.fig.add_subplot(gs[2, 1], **chart_style)

        # Line objects — updated in _update() without clearing axes each frame.
        # This is much faster than ax.clear() + ax.plot() every 500ms.
        (self.line_battery,) = self.ax_battery.plot([], [], color="#f39c12", lw=1.5)
        (self.line_height,)  = self.ax_height.plot([],  [], color="#2ecc71", lw=1.5)
        (self.line_temp,)    = self.ax_temp.plot([],    [], color="#e74c3c", lw=1.5)
        (self.line_accel,)   = self.ax_accel.plot([],   [], color="#9b59b6", lw=1.5)

        # Alert threshold lines (horizontal dashed)
        self.ax_battery.axhline(
            BATTERY_ALERT_THRESHOLD, color="#e74c3c",
            linestyle="--", linewidth=0.8, alpha=0.7, label=f"Alert {BATTERY_ALERT_THRESHOLD}%"
        )
        self.ax_height.axhline(
            ALTITUDE_ALERT_THRESHOLD, color="#e74c3c",
            linestyle="--", linewidth=0.8, alpha=0.7, label=f"Alert {ALTITUDE_ALERT_THRESHOLD}cm"
        )

        self._style_axes()

        # ── Status bar (row 3, spans both columns) ────────────────────
        self.ax_status = self.fig.add_subplot(gs[3, :])
        self.ax_status.set_axis_off()
        self.status_text = self.ax_status.text(
            0.5, 0.7, "Connecting...",
            transform=self.ax_status.transAxes,
            ha="center", va="center",
            fontsize=8, color="#bdc3c7"
        )
        # Key hint bar — static text, drawn once
        self.ax_status.text(
            0.5, 0.1,
            "  [T]akeoff  [L]and  [W]up  [S]down  [A]ccw  [D]cw  "
            "[↑]fwd  [↓]back  [←]left  [→]right  [E][E]Emergency  [Q][Q]Quit  ",
            transform=self.ax_status.transAxes,
            ha="center", va="center",
            fontsize=7.5, color="#7f8c8d"
        )

        # ── Keyboard binding ──────────────────────────────────────────
        # Connect the key_press_event to our handler.
        # lambda passes client and loop without needing them as globals.
        self.fig.canvas.mpl_connect(
            "key_press_event",
            lambda event: _on_key_press(event, self.client, self.loop)
        )

    def _style_axes(self):
        """Apply consistent dark-theme styling to all 4 chart axes."""
        panels = [
            (self.ax_battery, "Battery",      "%",      [0, 100]),
            (self.ax_height,  "Altitude",     "cm",     None),
            (self.ax_temp,    "Temperature",  "°C",     None),
            (self.ax_accel,   "Acceleration", "cm/s²",  None),
        ]
        for ax, title, unit, ylim in panels:
            ax.set_title(title, color="white", fontsize=9, pad=4)
            ax.set_ylabel(unit, color="#7f8c8d", fontsize=8)
            ax.tick_params(colors="#7f8c8d", labelsize=7)
            ax.set_facecolor("#16213e")
            for spine in ax.spines.values():
                spine.set_edgecolor("#2c3e50")
            ax.grid(True, color="#2c3e50", linewidth=0.5, alpha=0.7)
            ax.set_xlim(0, 100)   # 100 samples on x-axis
            if ylim:
                ax.set_ylim(ylim)

    def _update(self, frame):
        """
        Called by FuncAnimation every DASHBOARD_UPDATE_INTERVAL ms.
        Reads ClientState, updates all line data, updates text elements.

        Args:
            frame: frame counter (unused — we read live state instead)

        Returns:
            List of artists that changed — tells matplotlib what to redraw.
            Returning all lines + texts is safe (minor performance cost).
        """
        # Import here to avoid circular import at module load time.
        # client.py imports nothing from dashboard, but dashboard imports
        # client_state from client.py. Deferred import keeps it clean.
        from coap.client import client_state

        # ── Drain command queue ───────────────────────────────────────
        # Pick up any commands triggered by keypresses since last frame.
        try:
            while True:
                self._last_command = _command_queue.get_nowait()
        except queue.Empty:
            pass

        # ── Read latest telemetry (thread-safe snapshot) ──────────────
        snap = client_state.snapshot()

        # ── Update chart lines ────────────────────────────────────────
        # deque → list for matplotlib. x-axis is just the sample index.
        def _set_line(line, history):
            data = list(history)
            if data:
                line.set_data(range(len(data)), data)

        with client_state._lock:
            _set_line(self.line_battery, client_state.battery_history)
            _set_line(self.line_height,  client_state.height_history)
            _set_line(self.line_temp,    client_state.temp_history)
            _set_line(self.line_accel,   client_state.accel_history)

        # Auto-scale y-axis for charts without fixed limits
        for ax in [self.ax_height, self.ax_temp, self.ax_accel]:
            ax.relim()
            ax.autoscale_view(scalex=False, scaley=True)

        # ── Alert bar ─────────────────────────────────────────────────
        alerts = snap["alerts"]
        if alerts:
            # Show the most recent alert
            self.alert_text.set_text(alerts[-1])
            self.alert_text.get_bbox_patch().set_alpha(0.85)
        else:
            self.alert_text.set_text("")
            self.alert_text.get_bbox_patch().set_alpha(0.0)

        # ── Status bar ────────────────────────────────────────────────
        if snap["connected"]:
            status = (
                f"P:{snap['pitch']:+.0f}°  R:{snap['roll']:+.0f}°  Y:{snap['yaw']:+.0f}°  │  "
                f"Bat:{snap['battery']:.0f}%  H:{snap['height']:.0f}cm  ToF:{snap['tof']:.0f}cm  │  "
                f"Vx:{snap['vgx']:+.0f}  Vy:{snap['vgy']:+.0f}  Vz:{snap['vgz']:+.0f} cm/s  │  "
                f"Last cmd: {self._last_command}"
            )
        else:
            status = "⏳ Waiting for drone connection..."

        self.status_text.set_text(status)

        return [
            self.line_battery, self.line_height,
            self.line_temp,    self.line_accel,
            self.alert_text,   self.status_text
        ]

    def start(self):
        """
        Start the FuncAnimation and show the window.
        This call BLOCKS — it runs the matplotlib event loop.
        Everything else (CoAP client, drone telemetry) must already be
        running in background threads before this is called.

        interval: milliseconds between frames (from config).
        blit=True: only redraws artists returned by _update() — faster.
        cache_frame_data=False: don't cache, always call _update() fresh.
        """
        self._anim = FuncAnimation(
            self.fig,
            self._update,
            interval=DASHBOARD_UPDATE_INTERVAL,
            blit=True,
            cache_frame_data=False
        )
        plt.show()
        logger.info("Dashboard closed.")