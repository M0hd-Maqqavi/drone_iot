# web/app.py
"""
Web Dashboard Server — Client side.

Responsibilities:
    1. Serves the HTML dashboard at http://localhost:WEB_PORT/
    2. Pushes live telemetry to the browser via Server-Sent Events (SSE).
    3. Receives flight commands from the browser and forwards them to
       the CoAP client (which sends them to the CoAP server → drone).
    4. Proxies capture/record requests to the MJPEG server.

What is SSE (Server-Sent Events)?
    SSE is a lightweight HTTP protocol where the server pushes data to the
    browser over a single long-lived HTTP connection. The browser uses the
    EventSource API to listen. Unlike WebSockets, SSE is one-way
    (server → browser only) which is all we need for telemetry.
    It's simpler than WebSockets and works over plain HTTP.

    Flow:
        Browser opens GET /telemetry_stream
        Server responds with Content-Type: text/event-stream
        Server sends "data: {...json...}\n\n" every 500ms forever
        Browser's EventSource receives each chunk and calls onmessage handler
        JavaScript updates the charts and status bar

Why Flask on the CLIENT machine?
    The browser needs to talk to something local. Flask reads from
    client_state (populated by CoAP Observe notifications) and serves
    it to the browser. Commands go browser → Flask → CoAP client → CoAP server → drone.

Separate machine support:
    Flask binds to 0.0.0.0 so it can be reached from any browser on the LAN.
    The video feed URL is constructed from MJPEG_HOST:MJPEG_PORT (from config/env)
    so the browser knows where to find the video regardless of machine layout.
"""

import json
import time
import asyncio
import logging
import threading

from flask import Flask, Response, render_template, request, jsonify

from config import WEB_PORT, MJPEG_HOST, MJPEG_PORT
from coap.client import client_state

logger = logging.getLogger(__name__)

app = Flask(__name__)

# Reference to the asyncio event loop running the CoAP client.
# Set by main.py after the async thread starts.
# Used to schedule async CoAP commands from the sync Flask context.
_coap_loop   = None
_coap_client = None


def set_coap_context(loop, client):
    """
    Called from main.py once the CoAP client is authenticated.
    Stores the loop and client so Flask routes can dispatch commands.
    """
    global _coap_loop, _coap_client
    _coap_loop   = loop
    _coap_client = client


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.route("/")
def index():
    """
    Serve the main dashboard HTML page.
    Passes MJPEG video URL so the template knows where to find the stream.
    This URL is correct whether server and client are on the same machine
    or different machines — it reads from config which reads from .env.
    """
    video_url = f"http://{MJPEG_HOST}:{MJPEG_PORT}/video_feed"
    return render_template("index.html", video_url=video_url)


@app.route("/telemetry_stream")
def telemetry_stream():
    """
    Server-Sent Events endpoint.
    Browser opens this once and receives JSON telemetry every 500ms forever.

    SSE format (each event):
        data: {"battery": 87, "height": 120, ...}\n\n

    The double newline \n\n signals the end of one event to the browser.
    EventSource in the browser fires onmessage for each complete event.
    """
    def generate():
        while True:
            snap = client_state.snapshot()

            # Also include rolling histories for chart updates.
            # We read these separately under the lock.
            with client_state._lock:
                battery_hist = list(client_state.battery_history)
                height_hist  = list(client_state.height_history)
                temp_hist    = list(client_state.temp_history)
                accel_hist   = list(client_state.accel_history)

            snap["battery_history"] = battery_hist
            snap["height_history"]  = height_hist
            snap["temp_history"]    = temp_hist
            snap["accel_history"]   = accel_hist

            # SSE format: "data: <json>\n\n"
            yield f"data: {json.dumps(snap)}\n\n"
            time.sleep(0.5)   # 500ms — matches DASHBOARD_UPDATE_INTERVAL

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            # Disable caching — we always want fresh data
            "Cache-Control": "no-cache",
            # Keep connection alive
            "X-Accel-Buffering": "no"
        }
    )


@app.route("/command", methods=["POST"])
def command():
    """
    Receive a flight command from the browser and forward to CoAP client.

    Expected JSON body:
        {"action": "takeoff"}
        {"action": "land"}
        {"action": "emergency"}
        {"action": "move", "direction": "up", "distance": 30}

    Uses asyncio.run_coroutine_threadsafe() to call the async CoAP client
    from this sync Flask route — same pattern as the keyboard handler in plot.py.
    """
    if _coap_client is None or _coap_loop is None:
        return jsonify({"status": "error", "reason": "CoAP client not ready"}), 503

    data   = request.get_json()
    action = data.get("action")

    try:
        if action == "takeoff":
            future = asyncio.run_coroutine_threadsafe(
                _coap_client.takeoff(), _coap_loop
            )
        elif action == "land":
            future = asyncio.run_coroutine_threadsafe(
                _coap_client.land(), _coap_loop
            )
        elif action == "emergency":
            future = asyncio.run_coroutine_threadsafe(
                _coap_client.emergency(), _coap_loop
            )
        elif action == "move":
            direction = data.get("direction")
            distance  = int(data.get("distance", 30))
            future = asyncio.run_coroutine_threadsafe(
                _coap_client.move(direction, distance), _coap_loop
            )
        else:
            return jsonify({"status": "error", "reason": f"unknown action: {action}"}), 400

        # Wait up to 6 seconds for drone response
        result = future.result(timeout=6.0)
        return jsonify({"status": "ok", "result": result})

    except Exception as e:
        logger.error("Command error: %s", e)
        return jsonify({"status": "error", "reason": str(e)}), 500


@app.route("/capture")
def capture():
    """Proxy capture request to MJPEG server."""
    import requests as req
    try:
        r = req.get(
            f"http://{MJPEG_HOST}:{MJPEG_PORT}/capture",
            timeout=3
        )
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"status": "error", "reason": str(e)}), 500


@app.route("/record/start")
def record_start():
    """Proxy record start to MJPEG server."""
    import requests as req
    try:
        r = req.get(
            f"http://{MJPEG_HOST}:{MJPEG_PORT}/record/start",
            timeout=3
        )
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"status": "error", "reason": str(e)}), 500


@app.route("/record/stop")
def record_stop():
    """Proxy record stop to MJPEG server."""
    import requests as req
    try:
        r = req.get(
            f"http://{MJPEG_HOST}:{MJPEG_PORT}/record/stop",
            timeout=3
        )
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"status": "error", "reason": str(e)}), 500


# ─────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────

def start():
    """
    Start the Flask web dashboard server in a background daemon thread.
    Called from main.py after CoAP client is authenticated.

    Binds to 0.0.0.0 so it's reachable from any browser on the LAN,
    not just localhost — supports separate machine deployment.
    """
    flask_thread = threading.Thread(
        target=lambda: app.run(
            host="0.0.0.0",
            port=WEB_PORT,
            use_reloader=False,
            threaded=True,
            debug=False
        ),
        daemon=True,
        name="WebDashboard"
    )
    flask_thread.start()
    logger.info("Web dashboard started at http://0.0.0.0:%d", WEB_PORT)