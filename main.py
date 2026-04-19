# main.py
"""
Entry Point — wires all modules together and manages startup/shutdown.

Startup order (order matters):
    1. Logging configured first — everything below uses it.
    2. TelloBridge created and connected to drone (SDK mode + telemetry thread).
    3. CoAP server started in asyncio event loop (background thread).
    4. CoAP client started — runs auth handshake, subscribes to telemetry.
    5. Dashboard started on main thread — blocks until window is closed.
    6. On exit: graceful shutdown of client → server → drone bridge.

Threading model:
    ┌─────────────────────────────────────────────────────────┐
    │  MAIN THREAD                                            │
    │    - matplotlib dashboard (FuncAnimation)               │
    │    - keyboard event handler                             │
    └────────────────────────────┬────────────────────────────┘
                                 │ reads ClientState
    ┌────────────────────────────▼────────────────────────────┐
    │  ASYNCIO THREAD (background)                            │
    │    - aiocoap CoAP server (gateway)                      │
    │    - aiocoap CoAP client + auth handshake               │
    │    - Observe subscription tasks (7 concurrent)          │
    └────────────────────────────┬────────────────────────────┘
                                 │ reads shared.state
    ┌────────────────────────────▼────────────────────────────┐
    │  TELEMETRY THREAD (daemon, background)                  │
    │    - TelloBridge._telemetry_loop()                      │
    │    - Listens on UDP port 8890                           │
    │    - Writes to shared.state every ~100ms                │
    └─────────────────────────────────────────────────────────┘

Why run asyncio in a background thread?
    matplotlib's plt.show() must run on the MAIN thread on macOS — this
    is an OS-level requirement for GUI frameworks. asyncio and matplotlib
    both want the main thread. Solution: asyncio gets a background thread,
    matplotlib keeps the main thread.
    asyncio.run_coroutine_threadsafe() bridges commands from main → async thread.
"""

import asyncio
import logging
import sys
import threading
import time

from tello.drone import TelloBridge
from coap.server import create_server
from coap.client import DroneCoAPClient
from dashboard.plot import Dashboard

# ─────────────────────────────────────────────
# LOGGING SETUP
# ─────────────────────────────────────────────

def setup_logging():
    """
    Configure logging for the entire application.

    Format: timestamp | level | module | message
    Level: INFO shows all important events without debug noise.
           Change to DEBUG to see every CoAP message and telemetry packet.

    Both console (StreamHandler) and file (FileHandler) outputs are set up.
    The file log is useful for reviewing what happened during a flight.
    """
    fmt = "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s"
    datefmt = "%H:%M:%S"

    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt=datefmt,
        handlers=[
            logging.StreamHandler(sys.stdout),          # print to terminal
            logging.FileHandler("flight_log.txt", mode="w")  # write to file
        ]
    )
    # Suppress noisy aiocoap internal logs — we only want our own INFO+
    logging.getLogger("coap").setLevel(logging.WARNING)
    logging.getLogger("aiocoap").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# ASYNC MAIN (runs in background thread)
# ─────────────────────────────────────────────

async def async_main(bridge: TelloBridge):
    """
    Starts the CoAP server and client inside the asyncio event loop.
    Runs in a background thread — matplotlib runs on the main thread.

    Steps:
        1. Start CoAP server (create_server returns when server is listening).
        2. Start CoAP client (runs auth handshake + subscribes to telemetry).
        3. Keep running with asyncio.Event().wait() — stays alive until
           the shutdown event is set (triggered by main thread on window close).

    Args:
        bridge: TelloBridge instance (already connected to drone).

    Returns:
        DroneCoAPClient — passed back to the main thread for the dashboard.
    """
    logger.info("Starting CoAP server...")
    server_ctx = await create_server(bridge)
    logger.info("CoAP server running.")

    logger.info("Starting CoAP client...")
    client = DroneCoAPClient()
    await client.start()
    logger.info("CoAP client authenticated and subscribed.")

    return client, server_ctx


# ─────────────────────────────────────────────
# BACKGROUND THREAD RUNNER
# ─────────────────────────────────────────────

def run_async_loop(loop: asyncio.AbstractEventLoop, bridge: TelloBridge,
                   result_container: list, ready_event: threading.Event):
    """
    Runs in a background thread.
    Sets up and runs the asyncio event loop, then signals ready.

    Args:
        loop:             asyncio event loop (created in main, passed here).
        bridge:           TelloBridge instance.
        result_container: list of length 1 — used to pass client back to main thread.
                          Lists are mutable so the main thread sees the update.
        ready_event:      threading.Event — set when client is ready.
                          Main thread waits on this before starting dashboard.
    """
    asyncio.set_event_loop(loop)

    async def _run():
        try:
            client, server_ctx = await async_main(bridge)
            # Store client so main thread can pass it to Dashboard.
            result_container.append(client)
            result_container.append(server_ctx)
            # Signal main thread that setup is complete.
            ready_event.set()
            # Keep the event loop alive indefinitely.
            # It will be stopped by loop.call_soon_threadsafe(loop.stop)
            # when the main thread exits (dashboard window closed).
            await asyncio.Event().wait()
        except Exception as e:
            logger.error("Async main failed: %s", e)
            ready_event.set()   # unblock main thread even on failure

    loop.run_until_complete(_run())


# ─────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────

def main():
    setup_logging()
    logger.info("=" * 60)
    logger.info("Drone IoT System starting up")
    logger.info("=" * 60)

    # ── Step 1: Connect to drone ──────────────────────────────────────
    # This must happen BEFORE the asyncio thread starts because
    # TelloBridge.connect() is synchronous and starts its own thread.
    bridge = TelloBridge()
    try:
        bridge.connect()
        logger.info("Drone connected successfully.")

        # Short wait to let telemetry start flowing before the server
        # starts serving requests — avoids serving all-zero data initially.
        time.sleep(1.5)

        # Optional pre-flight battery check.
        battery = bridge.get_battery()
        if battery < 20:
            logger.warning("Battery is LOW (%d%%) — consider charging before flight.", battery)
        else:
            logger.info("Battery: %d%%", battery)

    except ConnectionError as e:
        logger.error("Could not connect to drone: %s", e)
        logger.error("Make sure you are connected to the drone's Wi-Fi (TELLO-XXXXXX).")
        sys.exit(1)

    # ── Step 2: Start asyncio loop in background thread ───────────────
    # result_container[0] = DroneCoAPClient (set inside run_async_loop)
    # result_container[1] = server_ctx
    result_container = []
    ready_event = threading.Event()

    loop = asyncio.new_event_loop()
    # new_event_loop() creates a fresh loop — does NOT set it as current.
    # run_async_loop() calls asyncio.set_event_loop(loop) inside the thread.

    async_thread = threading.Thread(
        target=run_async_loop,
        args=(loop, bridge, result_container, ready_event),
        daemon=True,
        name="AsyncIOThread"
    )
    async_thread.start()
    logger.info("Asyncio thread started.")

    # ── Step 3: Wait for CoAP client to be ready ─────────────────────
    # ready_event is set inside run_async_loop after auth completes.
    # Timeout of 30s — if auth hangs longer than this, something is wrong.
    logger.info("Waiting for CoAP auth handshake to complete...")
    ready = ready_event.wait(timeout=30.0)

    if not ready or not result_container:
        logger.error("CoAP client did not start within 30 seconds. Exiting.")
        bridge.disconnect()
        sys.exit(1)

    client = result_container[0]
    logger.info("CoAP client ready. Starting dashboard...")

    # ── Step 4: Start dashboard on main thread ────────────────────────
    # Dashboard.__init__ builds the figure.
    # Dashboard.start() calls plt.show() — BLOCKS until window is closed.
    try:
        dashboard = Dashboard(client, loop)
        dashboard.start()   # ← blocks here until user closes the window
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received.")
    finally:
        # ── Step 5: Graceful shutdown ─────────────────────────────────
        logger.info("Shutting down...")

        # Stop the asyncio event loop — this causes run_until_complete to return.
        # call_soon_threadsafe is the correct way to stop a loop from another thread.
        loop.call_soon_threadsafe(loop.stop)

        # Wait for the async thread to finish (max 5 seconds).
        async_thread.join(timeout=5.0)

        # Disconnect from drone — releases sockets cleanly.
        bridge.disconnect()

        logger.info("Shutdown complete. Goodbye.")


if __name__ == "__main__":
    main()