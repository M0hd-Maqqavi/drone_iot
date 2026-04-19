# main.py
"""
Entry Point — wires all modules together and manages startup/shutdown.

Startup order:
    1. Logging configured.
    2. TelloBridge connects to drone — SDK mode + telemetry + video stream.
    3. CoAP server starts in asyncio background thread.
    4. CoAP client authenticates + subscribes to telemetry.
    5. Video stream starts (MJPEG server on port 8080).
    6. Web dashboard starts (Flask on port 5000).
    7. Browser opens automatically at http://localhost:5000.
    8. Main thread waits for Ctrl+C.
    9. Graceful shutdown.

Threading model:
    Main thread        — startup sequence, waits on Ctrl+C
    AsyncIO thread     — CoAP server + client + auth + Observe
    Telemetry thread   — TelloBridge UDP 8890 listener (daemon)
    Video capture      — cv2.VideoCapture UDP 11111 (daemon)
    MJPEG server       — Flask on port 8080 (daemon)
    Web dashboard      — Flask on port 5000 (daemon)
"""

import asyncio
import logging
import sys
import threading
import time
import webbrowser

from tello.drone   import TelloBridge
from coap.server   import create_server
from coap.client   import DroneCoAPClient
import video.stream as video_stream
import web.app      as web_app
from config        import WEB_PORT, MJPEG_PORT


def setup_logging():
    fmt     = "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s"
    datefmt = "%H:%M:%S"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt=datefmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("flight_log.txt", mode="w")
        ]
    )
    logging.getLogger("aiocoap").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


async def async_main(bridge):
    """Start CoAP server and client inside the asyncio event loop."""
    logger.info("Starting CoAP server...")
    server_ctx = await create_server(bridge)
    logger.info("CoAP server running.")

    logger.info("Starting CoAP client...")
    client = DroneCoAPClient()
    await client.start()
    logger.info("CoAP client authenticated and subscribed.")

    return client, server_ctx


def run_async_loop(loop, bridge, result_container, ready_event):
    """Runs asyncio event loop in background thread."""
    asyncio.set_event_loop(loop)

    async def _run():
        try:
            client, server_ctx = await async_main(bridge)
            result_container.append(client)
            result_container.append(server_ctx)
            ready_event.set()
            await asyncio.Event().wait()
        except Exception as e:
            logger.error("Async main failed: %s", e)
            ready_event.set()

    loop.run_until_complete(_run())


def main():
    setup_logging()
    logger.info("=" * 60)
    logger.info("Drone IoT System starting up")
    logger.info("=" * 60)

    # ── Step 1: Connect to drone ──────────────────────────────────
    bridge = TelloBridge()
    try:
        bridge.connect()
        logger.info("Drone connected.")
        time.sleep(1.5)

        battery = bridge.get_battery()
        if battery < 20:
            logger.warning("Battery LOW: %d%% — consider charging.", battery)
        else:
            logger.info("Battery: %d%%", battery)

        # Enable H.264 video stream from drone
        resp = bridge.send_command("streamon")
        if resp == "ok":
            logger.info("Video stream enabled.")
        else:
            logger.warning("streamon returned: %s — video may not work.", resp)

    except ConnectionError as e:
        logger.error("Could not connect to drone: %s", e)
        sys.exit(1)

    # ── Step 2: Start CoAP in asyncio thread ──────────────────────
    result_container = []
    ready_event      = threading.Event()
    loop             = asyncio.new_event_loop()

    async_thread = threading.Thread(
        target=run_async_loop,
        args=(loop, bridge, result_container, ready_event),
        daemon=True,
        name="AsyncIOThread"
    )
    async_thread.start()

    # ── Step 3: Wait for auth ─────────────────────────────────────
    logger.info("Waiting for CoAP auth handshake...")
    ready = ready_event.wait(timeout=30.0)

    if not ready or not result_container:
        logger.error("CoAP client did not start within 30s. Exiting.")
        bridge.disconnect()
        sys.exit(1)

    client = result_container[0]

    # ── Step 4: Start video stream (MJPEG on port 8080) ───────────
    video_stream.start()
    logger.info("MJPEG stream at http://0.0.0.0:%d/video_feed", MJPEG_PORT)

    # ── Step 5: Start web dashboard (Flask on port 5000) ──────────
    # Pass CoAP client + loop so Flask can dispatch drone commands.
    web_app.set_coap_context(loop, client)
    web_app.start()
    logger.info("Web dashboard at http://0.0.0.0:%d", WEB_PORT)

    # ── Step 6: Open browser ──────────────────────────────────────
    time.sleep(1.5)   # give Flask time to bind
    url = f"http://localhost:{WEB_PORT}"
    logger.info("Opening browser: %s", url)
    webbrowser.open(url)

    # ── Step 7: Keep main thread alive ────────────────────────────
    logger.info("System running. Press Ctrl+C to shut down.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutdown requested.")
    finally:
        logger.info("Shutting down...")
        loop.call_soon_threadsafe(loop.stop)
        async_thread.join(timeout=5.0)
        bridge.disconnect()
        logger.info("Shutdown complete. Goodbye.")


if __name__ == "__main__":
    main()