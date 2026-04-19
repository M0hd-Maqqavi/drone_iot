# video/stream.py
"""
Video Stream Module — Gateway side.

Responsibilities:
    1. Opens the Tello's H.264 video stream via OpenCV (UDP port 11111).
    2. Decodes frames and serves them as MJPEG over HTTP.
       MJPEG = Motion JPEG: each frame is a JPEG image sent continuously
       over a single HTTP connection. The browser displays it as a live feed
       using a standard <img> tag — no special plugins needed.
    3. Handles image capture — saves the current frame as a timestamped JPEG.
    4. Handles video recording — writes frames to a timestamped MP4 file.

Why MJPEG over HTTP and not CoAP?
    CoAP is designed for small payloads (sensor readings, short JSON).
    A single 720p frame is ~50-100KB. Streaming at 30fps over CoAP would
    require thousands of blockwise transfers per second — completely unsuitable.
    In real IoT deployments (Nest, Ring, etc.), the IoT protocol handles
    control/telemetry while video uses a dedicated media transport.
    MJPEG/HTTP is the simplest, most reliable choice for this project.

Separate machine support:
    The MJPEG server binds to 0.0.0.0 (all interfaces) so it accepts
    connections from any machine on the network — not just localhost.
    The client machine's browser accesses it via http://SERVER_IP:MJPEG_PORT/video_feed.
    MJPEG_HOST and MJPEG_PORT are read from config (which reads from .env).
"""

import io
import cv2
import time
import threading
import logging
import os
from datetime import datetime
from flask import Flask, Response

from config import MJPEG_HOST, MJPEG_PORT

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# GLOBAL STATE
# ─────────────────────────────────────────────

# Latest decoded frame as raw BGR numpy array (OpenCV format).
# Written by _capture_loop(), read by generate_frames() and capture/record.
_latest_frame = None
_frame_lock   = threading.Lock()

# Recording state
_recording        = False
_video_writer     = None
_recording_lock   = threading.Lock()

# Flask app for MJPEG serving — separate from the main web dashboard Flask app.
# Runs on MJPEG_PORT (default 8080).
_video_app = Flask(__name__)


# ─────────────────────────────────────────────
# FRAME CAPTURE LOOP
# ─────────────────────────────────────────────

def _capture_loop():
    """
    Background thread: opens the Tello video stream and continuously
    reads decoded frames into _latest_frame.

    cv2.VideoCapture with the UDP URL:
        - OpenCV handles H.264 decoding internally via FFmpeg.
        - "udp://@0.0.0.0:11111" = listen on all interfaces, port 11111.
        - The Tello streams to whoever sent "streamon" — our gateway machine.

    Frame rate: Tello streams at ~30fps. We read as fast as OpenCV gives us
    frames. Each frame overwrites the previous — only the latest matters for
    the live feed. Recording gets every frame.

    If the stream drops (drone out of range, battery dies), cap.read() returns
    False. We log and wait 2 seconds before retrying.
    """
    global _latest_frame, _recording, _video_writer

    # UDP URL format for OpenCV/FFmpeg to receive Tello's H.264 stream.
    stream_url = "udp://@0.0.0.0:11111"

    while True:
        logger.info("Opening Tello video stream at %s", stream_url)
        cap = cv2.VideoCapture(stream_url, cv2.CAP_FFMPEG)

        if not cap.isOpened():
            logger.warning("Could not open video stream. Retrying in 2s...")
            time.sleep(2)
            continue

        logger.info("Video stream opened successfully.")

        while True:
            ret, frame = cap.read()

            if not ret:
                logger.warning("Video stream lost. Reconnecting...")
                break

            # Write frame to global latest_frame (thread-safe)
            with _frame_lock:
                _latest_frame = frame.copy()

            # If recording, write frame to video file
            with _recording_lock:
                if _recording and _video_writer is not None:
                    _video_writer.write(frame)

        cap.release()
        time.sleep(2)   # wait before reconnect attempt


# ─────────────────────────────────────────────
# MJPEG FRAME GENERATOR
# ─────────────────────────────────────────────

def generate_frames():
    """
    Generator function that yields MJPEG frames for the Flask /video_feed route.

    MJPEG protocol:
        Each frame is sent as a multipart HTTP response chunk:
            --frame\r\n
            Content-Type: image/jpeg\r\n\r\n
            <JPEG bytes>
            \r\n

        The browser receives this as a continuous stream and renders each
        JPEG as the new frame — creating the appearance of video.

    Frame rate:
        We sleep 33ms between frames (~30fps). This is plenty for a drone
        monitoring dashboard and keeps CPU usage low.

    If no frame is available yet (drone stream not started), we send a
    black placeholder frame so the browser doesn't show a broken image.
    """
    # Black placeholder frame — shown before drone stream starts
    placeholder = cv2.imencode(
        ".jpg",
        # 480x360 black frame
        cv2.rectangle(
            __import__("numpy").zeros((360, 480, 3), dtype="uint8"),
            (0, 0), (480, 360), (20, 20, 40), -1
        )
    )[1].tobytes()

    while True:
        with _frame_lock:
            frame = _latest_frame

        if frame is None:
            # No frame yet — send placeholder
            jpeg_bytes = placeholder
        else:
            # Encode current frame as JPEG
            ret, buffer = cv2.imencode(
                ".jpg", frame,
                [cv2.IMWRITE_JPEG_QUALITY, 80]  # 80% quality — good balance
            )
            if not ret:
                continue
            jpeg_bytes = buffer.tobytes()

        # Yield as multipart chunk
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n"
            + jpeg_bytes +
            b"\r\n"
        )

        time.sleep(0.033)   # ~30fps


# ─────────────────────────────────────────────
# FLASK ROUTES (MJPEG SERVER)
# ─────────────────────────────────────────────

@_video_app.route("/video_feed")
def video_feed():
    """
    MJPEG stream endpoint.
    Browser accesses: http://SERVER_IP:8080/video_feed
    Embedded in dashboard as: <img src="http://...">
    """
    return Response(
        generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )


@_video_app.route("/capture")
def capture_image():
    """
    Save the current frame as a timestamped JPEG.
    Called by the dashboard's Capture Image button.
    Files are saved to ./captures/ on the machine running this module
    (the server/gateway machine in a split deployment).
    """
    with _frame_lock:
        frame = _latest_frame

    if frame is None:
        return {"status": "error", "reason": "no frame available"}, 503

    # Create captures directory if it doesn't exist
    os.makedirs("captures", exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename  = f"captures/capture_{timestamp}.jpg"

    success = cv2.imwrite(filename, frame)

    if success:
        logger.info("Image captured: %s", filename)
        return {"status": "ok", "filename": filename}
    else:
        return {"status": "error", "reason": "failed to save image"}, 500


@_video_app.route("/record/start")
def record_start():
    """
    Start recording video to a timestamped MP4 file.
    Uses H.264 codec (mp4v) via OpenCV VideoWriter.
    """
    global _recording, _video_writer

    with _recording_lock:
        if _recording:
            return {"status": "error", "reason": "already recording"}, 400

        os.makedirs("recordings", exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename  = f"recordings/recording_{timestamp}.mp4"

        # Get frame dimensions from latest frame or use Tello default (960x720)
        with _frame_lock:
            frame = _latest_frame
        h, w = frame.shape[:2] if frame is not None else (720, 960)

        # cv2.VideoWriter: filename, codec, fps, frame size
        fourcc       = cv2.VideoWriter_fourcc(*"mp4v")
        _video_writer = cv2.VideoWriter(filename, fourcc, 30.0, (w, h))
        _recording    = True

        logger.info("Recording started: %s", filename)
        return {"status": "ok", "filename": filename}


@_video_app.route("/record/stop")
def record_stop():
    """Stop recording and finalise the MP4 file."""
    global _recording, _video_writer

    with _recording_lock:
        if not _recording:
            return {"status": "error", "reason": "not recording"}, 400

        _recording = False
        if _video_writer is not None:
            _video_writer.release()
            _video_writer = None

        logger.info("Recording stopped.")
        return {"status": "ok"}


# ─────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────

def start():
    """
    Start the video capture loop and MJPEG server.
    Called from main.py — runs both in background daemon threads.

    Two threads:
        1. _capture_loop: reads frames from drone, writes to _latest_frame
        2. _video_app.run: Flask MJPEG server, reads _latest_frame, serves HTTP

    Both are daemon threads — they die when the main process exits.
    """
    # Thread 1: frame capture loop
    capture_thread = threading.Thread(
        target=_capture_loop,
        daemon=True,
        name="VideoCapture"
    )
    capture_thread.start()
    logger.info("Video capture thread started.")

    # Thread 2: MJPEG Flask server
    # use_reloader=False: don't auto-reload (we're in a thread, not main process)
    # threaded=True: handle multiple browser connections simultaneously
    flask_thread = threading.Thread(
        target=lambda: _video_app.run(
            host="0.0.0.0",       # accept from any machine on the network
            port=MJPEG_PORT,
            use_reloader=False,
            threaded=True,
            debug=False
        ),
        daemon=True,
        name="MJPEGServer"
    )
    flask_thread.start()
    logger.info("MJPEG server started on http://0.0.0.0:%d/video_feed", MJPEG_PORT)