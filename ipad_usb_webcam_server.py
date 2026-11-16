#!/usr/bin/env python3
"""
ipad-usb-webcam — iOS/iPadOS Camera → Linux Native Webcam Bridge
=========================================================
Streams the iPad/iPhone camera to a Linux v4l2loopback device
(/dev/video10) so Zoom, Google Meet, OBS, Discord, Teams all see
it as a real webcam. HTTPS via Tailscale cert so iOS Safari
grants getUserMedia access.

Architecture:
  iPad Safari (getUserMedia) --WebSocket--> ipad-usb-webcam Server (appsrc) --> /dev/video10

Each JPEG frame received over WebSocket is pushed into a GStreamer
appsrc as a discrete buffer, so jpegdec always sees clean frame
boundaries (no pipe-stream ambiguity).
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gst, GLib  # noqa: E402

from aiohttp import web, WSMsgType  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HOST = "0.0.0.0"
PORT = int(os.environ.get("IPAD_USB_PORT", "8443"))
V4L2_DEVICE = os.environ.get("IPAD_USB_DEVICE", "/dev/video10")
# Resolution + quality are env-configurable. Defaults bumped to 1080p for the
# sharp iPad cameras. The v4l2loopback module must be loaded with
# max_width/max_height >= these (default unit uses 1920x1080).
V4L2_WIDTH = int(os.environ.get("IPAD_USB_WIDTH", "1920"))
V4L2_HEIGHT = int(os.environ.get("IPAD_USB_HEIGHT", "1080"))
V4L2_FPS = int(os.environ.get("IPAD_USB_FPS", "30"))
CERT = Path(__file__).parent.parent / "certs" / "ipad_usb.crt"
KEY = Path(__file__).parent.parent / "certs" / "ipad_usb.key"
WEB = Path(__file__).parent.parent / "web"

# --- Audio: virtual microphone --------------------------------------------
# The iPad captures its mic via getUserMedia, downmixes to mono 48kHz S16LE
# PCM in an AudioWorklet, and streams chunks over the same WebSocket (framed
# with a tiny header). The server decodes nothing (already raw PCM) and feeds
# an appsrc that renders into a PulseAudio/PipeWire null-sink whose monitor is
# exposed as the 'ipad-usb-webcam_Mic' recording source. Zoom/Meet select that source.
AUD_SINK_NAME = "ipad_usb_mic"
AUD_SOURCE_NAME = AUD_SINK_NAME + ".monitor"
AUD_RATE = 48000
AUD_CHANNELS = 1
AUD_FORMAT = "S16LE"

# Audio frame envelope sent from the client:
#   1 byte  0x01  = audio marker (so server can distinguish from JPEG video)
#   3 bytes reserved (little-endian)
#   rest   = raw S16LE mono PCM bytes
AUD_HEADER = b"\x01\x00\x00\x00"

GST_AUD_PIPELINE = (
    "appsrc name=asrc is-live=true do-timestamp=true "
    "format=time caps=audio/x-raw,format=%s,rate=%d,channels=%d,layout=interleaved "
    "! audioconvert "
    "! audioresample "
    "! pulsesink device=%s"
    % (AUD_FORMAT, AUD_RATE, AUD_CHANNELS, AUD_SINK_NAME)
)


def parse_jpeg_size(data: bytes):
    """Return (width, height) parsed from the JPEG SOF marker, or None.

    Walks the JPEG marker segments (starting after SOI 0xFFD8) until it finds
    an SOF0/SOF1/SOF2 marker, which carries the frame's real dimensions. This
    lets the server adapt its GStreamer caps to whatever resolution the client
    is currently sending (e.g. after a 1080p -> 720p switch in the web UI).
    """
    n = len(data)
    if n < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return None
    i = 2
    while i + 4 <= n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:          # fill byte before marker
            i += 1
            continue
        if 0xD0 <= marker <= 0xD9 or marker == 0x01:   # standalone, no length
            i += 2
            continue
        seg_len = (data[i + 2] << 8) | data[i + 3]
        if seg_len < 2:
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            # SOF: [len:2][precision:1][height:2][width:2]
            if i + 9 <= n:
                height = (data[i + 5] << 8) | data[i + 6]
                width = (data[i + 7] << 8) | data[i + 8]
                return width, height
            return None
        i += 2 + seg_len
    return None


def ensure_virtual_mic():
    """Create the PulseAudio/PipeWire null-sink if it isn't already present.

    Returns True if the sink exists (or was created) and is usable. The sink's
    .monitor becomes the 'ipad-usb-webcam_Mic' recording source apps can select.

    When running under a systemd *system* service there is no session env, so
    pactl cannot locate the user's audio socket. We derive XDG_RUNTIME_DIR and
    PULSE_SERVER from the invoking user's /run/user/<uid> so the sink lands in
    the interactive session's audio server (where Zoom/Meet look for mics).
    """
    import shutil
    import subprocess

    if shutil.which("pactl") is None:
        print("[ipad-usb-webcam] pactl not found; audio bridge disabled", file=sys.stderr)
        return False

    # Point pactl at the interactive user's PipeWire/Pulse socket.
    env = dict(os.environ)
    runtime = env.get("XDG_RUNTIME_DIR")
    if not runtime or not os.path.isdir(runtime):
        loginctl_uid = None
        try:
            # Resolve the uid of the service user (SUDO_USER > USER > current).
            import pwd
            name = os.environ.get("SUDO_USER") or os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
            loginctl_uid = pwd.getpwnam(name).pw_uid
        except Exception:
            loginctl_uid = os.getuid()
        candidate = "/run/user/%d" % loginctl_uid
        if os.path.isdir(candidate):
            runtime = candidate
    if runtime:
        env["XDG_RUNTIME_DIR"] = runtime
        pulse_native = os.path.join(runtime, "pulse", "native")
        if os.path.exists(pulse_native):
            env["PULSE_SERVER"] = "unix:" + pulse_native

    try:
        out = subprocess.run(
            ["pactl", "list", "short", "sinks"],
            capture_output=True, text=True, timeout=5, env=env,
        ).stdout
        if AUD_SINK_NAME in out:
            return True
        # Load a null-sink; its monitor is the mic source.
        subprocess.run(
            ["pactl", "load-module", "module-null-sink",
             "sink_name=%s" % AUD_SINK_NAME,
             "sink_properties=device.description=ipad-usb-webcam_Mic"],
            capture_output=True, text=True, timeout=5, check=True, env=env,
        )
        print("[ipad-usb-webcam] Created virtual mic sink '%s'" % AUD_SINK_NAME)
        return True
    except Exception as e:
        print("[ipad-usb-webcam] ensure_virtual_mic failed:", e,
              "| stderr:", getattr(e, "stderr", ""), file=sys.stderr)
        return False

# ---------------------------------------------------------------------------
# GStreamer engine (runs on its own GLib main loop thread)
# ---------------------------------------------------------------------------
class GstEngine:
    def __init__(self):
        self.pipeline = None
        self.appsrc = None
        self.running = False
        # Current pipeline dimensions. Updated when the client switches
        # resolution and the pipeline is rebuilt with matching caps.
        self.width = V4L2_WIDTH
        self.height = V4L2_HEIGHT

    def start(self, width=None, height=None):
        if self.running:
            return True
        if not os.path.exists(V4L2_DEVICE):
            print("[ipad-usb-webcam] %s missing; load v4l2loopback first" % V4L2_DEVICE,
                  file=sys.stderr)
            return False
        w = int(width or V4L2_WIDTH)
        h = int(height or V4L2_HEIGHT)
        pipeline_str = (
            "appsrc name=src is-live=true do-timestamp=true "
            "format=time caps=image/jpeg,width=%d,height=%d,framerate=%d/1 "
            "! jpegdec "
            "! videoconvert "
            "! video/x-raw,format=YUY2 "
            # leaky=downstream: if v4l2sink is slow, drop OLD frames instead of
            # buffering them (which would add latency). max-size-buffers=1 keeps
            # at most 1 frame in flight.
            "! queue leaky=downstream max-size-buffers=1 max-size-time=0 max-size-bytes=0 "
            "! v4l2sink device=%s sync=false qos=false"
            % (w, h, V4L2_FPS, V4L2_DEVICE)
        )
        try:
            self.pipeline = Gst.parse_launch(pipeline_str)
            self.appsrc = self.pipeline.get_by_name("src")
            # Watch the bus so GStreamer errors/warnings actually surface
            # instead of being swallowed silently.
            bus = self.pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message::error", self._on_bus_error)
            bus.connect("message::warning", self._on_bus_warning)
            ret = self.pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                print("[ipad-usb-webcam] set_state(PLAYING) FAILED", file=sys.stderr)
                self.pipeline = None
                self.appsrc = None
                return False
            self.width, self.height = w, h
            self.running = True
            print("[ipad-usb-webcam] GStreamer pipeline PLAYING (%dx%d) -> %s"
                  % (w, h, V4L2_DEVICE))
            return True
        except Exception as e:
            print("[ipad-usb-webcam] Failed to start pipeline:", e, file=sys.stderr)
            self.pipeline = None
            self.appsrc = None
            return False

    def _on_bus_error(self, bus, msg):
        err, dbg = msg.parse_error()
        print("[ipad-usb-webcam] GST ERROR (%s): %s | %s" % (
            msg.src.get_name() if msg.src else "?", err.message, dbg),
            file=sys.stderr)

    def _on_bus_warning(self, bus, msg):
        err, dbg = msg.parse_warning()
        print("[ipad-usb-webcam] GST WARN (%s): %s" % (
            msg.src.get_name() if msg.src else "?", err.message),
            file=sys.stderr)

    def push(self, data: bytes):
        if not self.running or not self.appsrc:
            return
        buf = Gst.Buffer.new_wrapped(data)
        buf.pts = Gst.CLOCK_TIME_NONE  # rely on do-timestamp
        flow = self.appsrc.emit("push-buffer", buf)
        if flow != Gst.FlowReturn.OK:
            print("[ipad-usb-webcam] push-buffer flow:", flow, file=sys.stderr)

    def stop(self):
        if self.pipeline:
            try:
                self.pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
        self.pipeline = None
        self.appsrc = None
        self.running = False
        print("[ipad-usb-webcam] GStreamer pipeline stopped")

    @property
    def active(self):
        return self.running


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
class AudEngine:
    """Renders client mic PCM into the 'ipad-usb-webcam_Mic' virtual source."""

    def __init__(self):
        self.pipeline = None
        self.appsrc = None
        self.running = False

    def start(self):
        if self.running:
            return True
        try:
            self.pipeline = Gst.parse_launch(GST_AUD_PIPELINE)
            self.appsrc = self.pipeline.get_by_name("asrc")
            bus = self.pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message::error", self._on_error)
            ret = self.pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                print("[ipad-usb-webcam] audio set_state(PLAYING) FAILED", file=sys.stderr)
                self.pipeline = None
                self.appsrc = None
                return False
            self.running = True
            print("[ipad-usb-webcam] Audio pipeline PLAYING -> %s" % AUD_SINK_NAME)
            return True
        except Exception as e:
            print("[ipad-usb-webcam] Failed to start audio pipeline:", e, file=sys.stderr)
            self.pipeline = None
            self.appsrc = None
            return False

    def _on_error(self, bus, msg):
        err, dbg = msg.parse_error()
        print("[ipad-usb-webcam] AUDIO ERROR: %s | %s" % (err.message, dbg), file=sys.stderr)

    def push(self, data: bytes):
        if not self.running or not self.appsrc:
            return
        buf = Gst.Buffer.new_wrapped(data)
        buf.pts = Gst.CLOCK_TIME_NONE
        flow = self.appsrc.emit("push-buffer", buf)
        if flow != Gst.FlowReturn.OK:
            print("[ipad-usb-webcam] audio push flow:", flow, file=sys.stderr)

    def stop(self):
        if self.pipeline:
            try:
                self.pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
        self.pipeline = None
        self.appsrc = None
        self.running = False

    @property
    def active(self):
        return self.running


class AuraState:
    def __init__(self):
        self.engine = GstEngine()
        self.aud = AudEngine()
        self.clients: set = set()
        self.active_client = None
        self.frames_in = 0
        self.bytes_in = 0
        self.audio_in = 0
        self.started_at = asyncio.get_event_loop().time()


state = AuraState()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
async def index(req: web.Request) -> web.Response:
    # no-store: Safari on the iPad caches aggressively; without this the device
    # can keep serving a stale UI after we deploy a new index.html.
    resp = web.FileResponse(WEB / "index.html")
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


async def stats(req: web.Request) -> web.Response:
    uptime = asyncio.get_event_loop().time() - state.started_at
    return web.json_response({
        "v4l2_device": V4L2_DEVICE,
        # Actual pipeline dimensions (change when the client switches res),
        # not the static defaults.
        "resolution": "%dx%d@%d" % (state.engine.width, state.engine.height, V4L2_FPS),
        "gst_running": state.engine.active,
        "audio_running": state.aud.active,
        "audio_source": AUD_SOURCE_NAME,
        "clients_connected": len(state.clients),
        "active_streamer": state.active_client is not None,
        "frames_in": state.frames_in,
        "bytes_in": state.bytes_in,
        "audio_bytes_in": state.audio_in,
        "uptime_sec": round(uptime, 1),
    })


async def reset(req: web.Request) -> web.Response:
    state.engine.stop()
    state.aud.stop()
    state.frames_in = 0
    state.bytes_in = 0
    state.audio_in = 0
    state.active_client = None
    return web.json_response({"ok": True, "msg": "ipad-usb-webcam pipeline reset"})


async def stream_ws(req: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024)
    await ws.prepare(req)
    state.clients.add(ws)
    print("[ipad-usb-webcam] WS client connected. Total:", len(state.clients))

    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                raw = msg.data
                # Distinguish audio PCM (leading 0x01 marker) from JPEG video
                # (which always starts with 0xFF 0xD8). A 4-byte envelope is
                # only stripped for audio; video frames are pushed as-is.
                if len(raw) >= 4 and raw[0] == 0x01 and raw[1:4] == b"\x00\x00\x00":
                    pcm = raw[4:]
                    if not state.aud.active and not state.aud.start():
                        await ws.send_json({"err": "audio device not available"})
                        continue
                    state.aud.push(pcm)
                    state.audio_in += len(pcm)
                    continue
                # Video JPEG frame
                dims = parse_jpeg_size(raw)
                if state.active_client in (None, ws):
                    state.active_client = ws
                # If a frame's real dimensions differ from the running
                # pipeline (client switched resolution), rebuild the pipeline
                # with matching caps. parse_jpeg_size returns None if it can't
                # find a SOF marker, in which case we just push as-is.
                if dims and state.engine.active and (
                        dims[0] != state.engine.width or
                        dims[1] != state.engine.height):
                    print("[ipad-usb-webcam] Resolution change %dx%d -> %dx%d; "
                          "rebuilding pipeline" % (state.engine.width,
                          state.engine.height, dims[0], dims[1]),
                          file=sys.stderr)
                    state.engine.stop()
                if not state.engine.active:
                    w = dims[0] if dims else V4L2_WIDTH
                    h = dims[1] if dims else V4L2_HEIGHT
                    if not state.engine.start(width=w, height=h):
                        await ws.send_json({"err": "v4l2 device not available"})
                        continue
                state.engine.push(raw)
                state.frames_in += 1
                state.bytes_in += len(raw)
            elif msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if data.get("cmd") == "start":
                        state.active_client = ws
                        ok = state.engine.active or state.engine.start()
                        await ws.send_json({
                            "ok": ok,
                            "device": V4L2_DEVICE,
                            "resolution": "%dx%d@%d" % (
                                state.engine.width, state.engine.height,
                                V4L2_FPS),
                        })
                    elif data.get("cmd") == "stop":
                        state.active_client = None
                        state.engine.stop()
                        await ws.send_json({"ok": True})
                    elif data.get("cmd") == "audio_start":
                        ok = state.aud.active or state.aud.start()
                        await ws.send_json({
                            "ok": ok, "audio": ok, "source": AUD_SOURCE_NAME,
                        })
                    elif data.get("cmd") == "audio_stop":
                        state.aud.stop()
                        await ws.send_json({"ok": True, "audio": False})
                except Exception as e:
                    print("[ipad-usb-webcam] Bad control msg:", e, file=sys.stderr)
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        state.clients.discard(ws)
        if state.active_client is ws:
            state.active_client = None
            state.engine.stop()
        print("[ipad-usb-webcam] WS client gone. Total:", len(state.clients))
    return ws


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def make_app() -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/stats", stats)
    app.router.add_post("/reset", reset)
    app.router.add_get("/ws", stream_ws)
    if WEB.exists():
        app.router.add_static("/static", WEB)
    return app


async def on_startup(app: web.Application):
    # Initialize GStreamer once (it manages its own threads internally).
    Gst.init(None)
    # Start a GLib main loop on a background thread so GStreamer callbacks
    # (bus messages, etc.) have a context to run in.
    loop = GLib.MainLoop()
    import threading
    t = threading.Thread(target=loop.run, daemon=True)
    t.start()
    # Provision the virtual microphone sink so the iPad's mic can be routed
    # into a real PulseAudio/PipeWire recording source ('ipad-usb-webcam_Mic').
    ensure_virtual_mic()


async def on_cleanup(app: web.Application):
    state.engine.stop()
    state.aud.stop()


def main():
    import ssl

    if not CERT.exists() or not KEY.exists():
        print("[ipad-usb-webcam] Missing TLS cert/key. Run:", file=sys.stderr)
        print("  sudo tailscale cert --cert-file certs/ipad_usb.crt "
              "--key-file certs/ipad_usb.key <your-ts-name>", file=sys.stderr)
        sys.exit(1)

    app = make_app()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(certfile=str(CERT), keyfile=str(KEY))

    print("[ipad-usb-webcam] HTTPS on https://0.0.0.0:%d" % PORT)
    print("[ipad-usb-webcam] iPad URL: https://pikrowave-ideapad-5-14aba7.tail41a6ad.ts.net:%d" % PORT)
    print("[ipad-usb-webcam] Virtual webcam: %s (label 'ipad-usb-webcam-HD')" % V4L2_DEVICE)
    web.run_app(app, host=HOST, port=PORT, ssl_context=ssl_ctx)


if __name__ == "__main__":
    main()
