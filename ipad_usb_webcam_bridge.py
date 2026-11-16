#!/usr/bin/env python3
"""ipad-usb-webcam Pro USB Bridge — IUCM v1.2 receiver.

Pulls hardware-encoded HEVC video + AAC audio from the TetherCam iOS/iPadOS app
(free, App Store) over the USB cable via usbmuxd, decodes with GStreamer, and
renders into the existing ipad-usb-webcam virtual devices:
  video -> /dev/video10 (ipad-usb-webcam-HD)   via v4l2sink
  audio -> null-sink ipad_usb_mic       via pulsesink (source: ipad-usb-webcam_Mic)

Protocol: IUCM v1.2 per Kanevry/tethercam protocol/PROTOCOL.md (MIT/GPL).
This is an independent receiver implementation written from the spec.

Modes:
  default      : usbmuxd tunnel -> device TCP port 7878 (iPad over USB cable)
  IPAD_USB_BRIDGE_TCP=host:port : plain TCP (offline tests against the sim)

Run as vickr on the laptop:
  ~/ipad-usb-webcam/.venv/bin/python ~/ipad-usb-webcam/usb/ipad_usb_bridge.py
"""
import os
import queue
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GObject

# ---------------------------------------------------------------------------
# Config (env-overridable, same style as ipad_usb_server.py)
# ---------------------------------------------------------------------------
V4L2_DEVICE = os.environ.get("IPAD_USB_DEVICE", "/dev/video10")
WIDTH = int(os.environ.get("IPAD_USB_WIDTH", "1920"))
HEIGHT = int(os.environ.get("IPAD_USB_HEIGHT", "1080"))
FPS = int(os.environ.get("IPAD_USB_FPS", "30"))
FLIP_METHOD = os.environ.get("IPAD_USB_FLIP", "180").lower()
# Per-camera flip (device orientation differs 180° between front/back sensors).
# IPAD_USB_FLIP_BACK / IPAD_USB_FLIP_FRONT override; legacy IPAD_USB_FLIP sets both.
_flip_legacy = os.environ.get("IPAD_USB_FLIP", "180").lower()
FLIP_BACK = os.environ.get("IPAD_USB_FLIP_BACK", _flip_legacy).lower()
FLIP_FRONT = os.environ.get("IPAD_USB_FLIP_FRONT", _flip_legacy).lower()
# GstVideoOrientationMethod ints: 0=identity 1=90cw 2=180 3=90ccw 4=hflip
# 5=vflip 10=auto (gravity-based, only sane with app autoRotation).
FLIP_METHODS = {"none": None, "": None, "identity": 0, "90": 1, "90cw": 1,
                "180": 2, "90ccw": 3, "270": 3, "hflip": 4, "mirror": 4,
                "vflip": 5, "auto": 10}
BITRATE_KBPS = int(os.environ.get("IPAD_USB_BITRATE", "12000"))
APP_PORT = int(os.environ.get("IPAD_USB_APP_PORT", "7878"))
AUDIO_SINK = os.environ.get("IPAD_USB_AUDIO_SINK", "ipad_usb_mic")
DIRECT_TCP = os.environ.get("IPAD_USB_BRIDGE_TCP", "")  # host:port (offline test)
DUMP_H265 = os.environ.get("IPAD_USB_DUMP_H265", "")  # debug: dump raw AU data
DUMP_AAC = os.environ.get("IPAD_USB_DUMP_AAC", "")  # debug: dump raw AAC frames
USBMUXD_PATH = "/var/run/usbmuxd"

MAGIC = b"IUCM"
MAX_PAYLOAD = 8 * 1024 * 1024

# IUCM message types
T_HELLO = 0x01
T_START = 0x02
T_STOP = 0x03
T_CLIENT_INFO = 0x04
T_STATS = 0x12
T_CONFIG = 0x10
T_VIDEO = 0x11
T_AUDIO_CONFIG = 0x13
T_AUDIO = 0x14
T_PING = 0x20
T_PONG = 0x21
T_ERROR = 0x30

Gst.init(None)


def log(msg):
    print("[bridge %s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------------------
# usbmuxd client (per PROTOCOL.md §6)
# ---------------------------------------------------------------------------
def usbmux_connect(device_id=None, port=APP_PORT):
    """Open a raw tunnel to device:port through /var/run/usbmuxd."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(USBMUXD_PATH)
    tag = [0]

    def recv_exact(n):
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("usbmuxd closed the socket")
            buf += chunk
        return buf

    def send_recv_plist(payload):
        tag[0] += 1
        body = plistlib_dumps(payload)
        hdr = struct.pack("<IIII", 16 + len(body), 1, 8, tag[0])
        s.sendall(hdr + body)
        raw = recv_exact(16)
        length, _ver, _msg, _rtag = struct.unpack("<IIII", raw)
        if length < 16 or length > 1_000_000:
            raise RuntimeError("usbmuxd: implausible plist length %d" % length)
        return plistlib_loads(recv_exact(length - 16))

    base = {
        "ClientVersionString": "ipad-usb-bridge-1.0",
        "ProgName": "ipad_usb_bridge",
        "kLibUSBMuxVersion": 3,
    }
    resp = send_recv_plist({**base, "MessageType": "ListDevices"})
    usb_devs = [
        d for d in resp.get("DeviceList", [])
        if d.get("Properties", {}).get("ConnectionType") == "USB"
    ]
    if not usb_devs:
        s.close()
        raise RuntimeError(
            "no iPad found on USB via usbmuxd (plug the cable / trust the laptop)")
    if device_id is None:
        device_id = usb_devs[0]["DeviceID"]

    # GOTCHA (§6.3): PortNumber travels in NETWORK byte order (htons).
    # Wrong order fails with Number 3 — same as a closed port.
    resp = send_recv_plist({**base, "MessageType": "Connect",
                            "DeviceID": device_id,
                            "PortNumber": socket.htons(port)})
    if resp.get("Number") != 0:
        s.close()
        raise RuntimeError(
            "usbmuxd Connect refused (Number=%s) — is TetherCam open on the "
            "iPad in the foreground?" % resp.get("Number"))
    log("usbmuxd tunnel open -> device %s port %d" % (device_id, port))
    return s


def plistlib_dumps(obj):
    import plistlib
    return plistlib.dumps(obj, fmt=plistlib.FMT_XML)


def plistlib_loads(data):
    import plistlib
    return plistlib.loads(data)


# ---------------------------------------------------------------------------
# IUCM framing (§2)
# ---------------------------------------------------------------------------
class IucmReader:
    def __init__(self, sock):
        self.s = sock
        self.buf = b""

    def _fill(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(1 << 16)
            if not chunk:
                raise ConnectionError("device closed the connection")
            self.buf += chunk

    def read_message(self):
        # Resync (§2): scan forward to the next 'I' if the magic is off.
        while self.buf[:4] != MAGIC:
            idx = self.buf.find(b"I", 1)
            self.buf = self.buf[idx:] if idx > 0 else b""
            self._fill(4 if len(self.buf) < 4 else 12)
        self._fill(12)
        mtype = self.buf[4]
        flags = self.buf[5]
        length = struct.unpack("<I", self.buf[8:12])[0]
        if length > MAX_PAYLOAD:
            raise ValueError("oversize IUCM frame: %d bytes" % length)
        self._fill(12 + length)
        payload = self.buf[12:12 + length]
        self.buf = self.buf[12 + length:]
        return mtype, flags, payload


def iucm_frame(mtype, flags, payload=b""):
    return MAGIC + bytes([mtype, flags]) + b"\x00\x00" \
        + struct.pack("<I", len(payload)) + payload


def parse_hello(p):
    version = struct.unpack("<H", p[0:2])[0]
    major, minor = version >> 8, version & 0xFF
    i = 2
    nlen = p[i]; i += 1
    name = p[i:i + nlen].decode("utf-8", "replace"); i += nlen
    alen = p[i]; i += 1
    appver = p[i:i + alen].decode("utf-8", "replace"); i += alen
    ccount = p[i]; i += 1
    cams = []
    for _ in range(ccount):
        cid, pos, nl = p[i], p[i + 1], p[i + 2]; i += 3
        cname = p[i:i + nl].decode("utf-8", "replace"); i += nl
        cams.append({"id": cid, "position": "front" if pos == 1 else "back",
                     "name": cname})
    return major, minor, name, appver, cams


def parse_config(p):
    w, h, fps = struct.unpack("<HHH", p[0:6])
    hvcc_len = struct.unpack("<I", p[6:10])[0]
    return w, h, fps, p[10:10 + hvcc_len]


def parse_audio_config(p):
    rate, = struct.unpack("<I", p[0:4])
    channels, codec = p[4], p[5]
    asc_len = struct.unpack("<H", p[6:8])[0]
    return rate, channels, codec, p[8:8 + asc_len]


def parse_stats(p):
    if len(p) < 22:
        return {}
    cont = struct.unpack("<h", p[0:2])[0] / 10.0
    return {
        "continuous_deg": cont,
        "sector": struct.unpack("<H", p[2:4])[0],
        "residual_deg": struct.unpack("<h", p[4:6])[0] / 10.0,
        "gravity_m": struct.unpack("<H", p[6:8])[0] / 1000.0,
        "dropped_frames": struct.unpack("<H", p[10:12])[0],
        "source": "%dx%d" % struct.unpack("<HH", p[12:16]),
        "output": "%dx%d" % struct.unpack("<HH", p[16:20]),
        "flags": p[20],
        "auto_rotation": bool(p[20] & 0x01),
        "horizon_leveling": bool(p[20] & 0x02),
        "flat_hold": bool(p[20] & 0x08),
        "audio_active": bool(p[20] & 0x10),
        "audio_muted": bool(p[20] & 0x20),
        "camera_id": p[21],
    }


def parse_error(p):
    code = struct.unpack("<H", p[0:2])[0]
    tlen = struct.unpack("<H", p[2:4])[0]
    return code, p[4:4 + tlen].decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# GStreamer pipelines
# ---------------------------------------------------------------------------
def hvc1_to_annexb(data):
    """Convert one hvc1 access unit ([len][NAL]...) to Annex-B start codes."""
    out = bytearray()
    i, n = 0, len(data)
    while i + 4 <= n:
        ln = struct.unpack(">I", data[i:i+4])[0]
        i += 4
        if ln == 0 or i + ln > n:
            break  # best effort — keep what we have
        out += b"\x00\x00\x00\x01" + data[i:i+ln]
        i += ln
    return bytes(out)


def hvcC_param_nals(hvcC):
    """Extract VPS/SPS/PPS NAL bytes from an HEVCDecoderConfigurationRecord.

    TetherCam (real iPad) sends parameter sets ONLY here — never in-band —
    so the decoder starves unless we inject them before the first AU.
    """
    try:
        if not hvcC or hvcC[0] != 1 or len(hvcC) < 23:
            return b""
        num_arrays = hvcC[22]
        pos = 23
        out = bytearray()
        for _ in range(num_arrays):
            arr_hdr = hvcC[pos]
            pos += 1
            nal_type = arr_hdr & 0x3F
            num_nalus = struct.unpack(">H", hvcC[pos:pos+2])[0]
            pos += 2
            for _ in range(num_nalus):
                nlen = struct.unpack(">H", hvcC[pos:pos+2])[0]
                pos += 2
                if nal_type in (32, 33, 34) and pos + nlen <= len(hvcC):
                    out += b"\x00\x00\x00\x01" + hvcC[pos:pos+nlen]
                pos += nlen
        return bytes(out)
    except Exception:
        return b""


class VideoPipeline:
    """appsrc(hvc1 HEVC) -> h265parse -> avdec_h265 -> YUY2 -> /dev/video10."""

    def __init__(self, hvcC):
        # NOTE: pygobject DROPS codec_data GValues crossing appsrc's caps
        # property (tested: direct Buffer, init+set_boxed, 2-arg Value — all
        # silently stripped), which stalls the parse forever. So we feed
        # Annex-B byte-stream instead; VPS/SPS/PPS arrive in-band with IDRs
        # (hvcC kept only as metadata). Verified working via filesrc probe.
        caps = Gst.Caps.from_string(
            "video/x-h265,stream-format=byte-stream,alignment=au")
        el = Gst.ElementFactory.make
        self.pipeline = Gst.Pipeline.new("ipad_usb-usb-video")
        try:
            self.src = el("appsrc", "vsrc")
            parse = el("h265parse")
            dec = el("avdec_h265")
            conv = el("videoconvert")
            sink = el("v4l2sink")
            chain = [self.src, parse, dec, conv]
            # Always create videoflip so per-camera runtime switching works;
            # FLIP_BACK=="none"/"" maps to identity (0).
            flip = el("videoflip", "vflip_el")
            flip_int = FLIP_METHODS.get(FLIP_BACK)
            if flip_int is None:
                flip_int = 0
            flip.set_property("method", flip_int)
            log("videoflip: back=%s (%d)" % (FLIP_BACK, flip_int))
            chain.append(flip)
            self.flip_el = flip
            for e in chain:
                self.pipeline.add(e)
            for a, b in zip(chain, chain[1:]):
                a.link(b)
            self.pipeline.add(sink)
            chain[-1].link_filtered(
                sink, Gst.Caps.from_string("video/x-raw,format=YUY2"))
            self.src.set_property("is-live", True)
            self.src.set_property("do-timestamp", True)
            self.src.set_property("format", Gst.Format.TIME)
            self.src.set_property("caps", caps)
            self.src.set_property("max-bytes", 0)  # frames are 4MB — no cap
            sink.set_property("device", V4L2_DEVICE)
            sink.set_property("sync", False)
            sink.set_property("qos", False)
            # Parameter sets (VPS/SPS/PPS) from the hvcC — injected before
            # the first AU because TetherCam never sends them in-band.
            self._params = hvcC_param_nals(hvcC)
            log("hvcC param NALs extracted: %d bytes"
                % len(self._params))
        except Exception:
            self.pipeline.set_state(Gst.State.NULL)
            raise
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("video pipeline failed to start")
        # NOTE: a sink pipeline stays PAUSED until the first buffer prerolls
        # — state is verified in push() after real frames arrive.
        self._frames_pushed = 0
        self._state_warned = False
        log("video pipeline starting (PLAYING on preroll) -> %s" % V4L2_DEVICE)

    def push(self, data):
        if DUMP_H265 and self._frames_pushed < 30:
            with open(DUMP_H265, "ab") as f:
                f.write(data)
        payload = hvc1_to_annexb(data)
        if self._params and self._frames_pushed == 0:
            payload = self._params + payload  # VPS/SPS/PPS before AU #1
        buf = Gst.Buffer.new_wrapped(payload)
        buf.pts = self._frames_pushed * 33333333  # 30fps PTS, monotonic
        buf.duration = 33333333
        ret = self.src.emit("push-buffer", buf)
        if ret != Gst.FlowReturn.OK:
            raise RuntimeError("video appsrc push: %s" % ret)
        self._frames_pushed += 1
        if self._frames_pushed == 10 and not self._state_warned:
            _, st, _ = self.pipeline.get_state(2 * Gst.SECOND)
            if st == Gst.State.PLAYING:
                log("video pipeline PLAYING -> %s (hvc1 HEVC decode)"
                    % V4L2_DEVICE)
            else:
                bus = self.pipeline.get_bus()
                msg = bus.timed_pop_filtered(
                    300 * Gst.MSECOND,
                    Gst.MessageType.ERROR | Gst.MessageType.WARNING)
                detail = ""
                if msg:
                    err, dbg = msg.parse_error()
                    detail = " | %s: %s (%s)" % (
                        Gst.message_type_get_name(msg.type), err, dbg)
                self._state_warned = True
                log("WARN: video pipeline still %s after 10 frames%s"
                    % (Gst.Element.state_get_name(st), detail))

    def set_flip(self, method_int, tag=""):
        """Switch videoflip at runtime (per-camera orientation)."""
        if getattr(self, "flip_el", None) is None:
            return
        self.flip_el.set_property("method", method_int)
        log("videoflip switched: %s (%d)%s"
            % (next(k for k, v in FLIP_METHODS.items() if v == method_int),
               method_int, tag))

    def teardown(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)


def adts_header(payload_len, profile, sfidx, ch):
    """7-byte ADTS header (no CRC) for one raw AAC frame.

    Bit layout: sync(12) ID layer prot(4) | profile(2) sfidx(4) priv(1)
    ch-high(1) | ch-low(2) orig home cp cp-start(4) len[12:11](2) |
    len[10:3](8) | len[2:0](3) fullness[10:6](5) | fullness[5:0](6)
    blocks(2). Length field is 13 bits SPANNING the tail of byte3!
    """
    flen = payload_len + 7
    return bytes([
        0xFF,
        0xF1,  # MPEG-4, layer 0, protection_absent=1
        ((profile & 3) << 6) | ((sfidx & 15) << 2),
        ((ch & 3) << 6) | 0x3C | ((flen >> 11) & 3),
        (flen >> 3) & 0xFF,
        ((flen & 7) << 5) | 0x1F,  # fullness high 5 of 0x7FF (VBR)
        0xFC,  # fullness low 6 bits (0x3F) << 2 | blocks-1 = 0
    ])


ASC_FREQ_IDX = {96000: 0, 88200: 1, 64000: 2, 48000: 3, 44100: 4, 32000: 5,
                24000: 6, 22050: 7, 16000: 8, 12000: 9, 11025: 10, 8000: 11}


def parse_asc(asc, rate=48000, channels=1):
    """AudioSpecificConfig -> (audioObjectType, samplingFreqIndex, channels).

    TetherCam sends a 39-byte Apple kAudioConverterCompressionMagicCookie
    here, NOT a clean ISO ASC — parsing it naively yields garbage. The
    AUDIO_CONFIG fields (sample_rate, channels) are authoritative, so build
    the frequency index from those instead.
    """
    sfidx = ASC_FREQ_IDX.get(int(rate), 3)
    ch = int(channels) or 1
    # Audio object type: codec 1 == AAC-LC (AOT 2) per §4.9.
    aot = 2
    if asc and len(asc) >= 2 and (asc[0] >> 3) in (1, 2, 5, 29):
        aot = asc[0] >> 3  # trust the cookie only when it looks like an ASC
    return aot, sfidx, ch


class AudioPipeline:
    """appsrc(ADTS AAC) -> aacparse -> avdec_aac -> pulsesink(null-sink).

    NOTE: raw AAC would need codec_data on the appsrc caps, but pygobject
    silently strips GValue Buffer fields crossing set_property (same bug
    that broke HEVC). ADTS framing needs no codec_data at all.
    """

    def __init__(self, rate, channels, asc):
        aot, sfidx, ch = parse_asc(asc, rate, channels)
        self._profile = max(0, aot - 1)
        self._sfidx = sfidx
        self._ch = ch
        caps = Gst.Caps.from_string(
            "audio/mpeg,mpegversion=4,stream-format=adts,rate=%d,channels=%d"
            % (rate, channels))
        el = Gst.ElementFactory.make
        self.pipeline = Gst.Pipeline.new("ipad_usb-usb-audio")
        self.src = el("appsrc", "asrc")
        parse = el("aacparse")
        dec = el("avdec_aac")
        conv = el("audioconvert")
        res = el("audioresample")
        sink = el("pulsesink")
        for e in (self.src, parse, dec, conv, res, sink):
            self.pipeline.add(e)
        for a, b, tag in ((self.src, parse, "src->parse"),
                          (parse, dec, "parse->dec"),
                          (dec, conv, "dec->conv"),
                          (conv, res, "conv->res"),
                          (res, sink, "res->sink")):
            if not a.link(b):
                raise RuntimeError("audio link FAILED: %s" % tag)
        log("audio chain links OK: src->parse->dec->conv->res->sink")
        self.src.set_property("is-live", True)
        self.src.set_property("do-timestamp", True)
        self.src.set_property("format", Gst.Format.TIME)
        self.src.set_property("caps", caps)
        sink.set_property("device", AUDIO_SINK)
        sink.set_property("sync", False)
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("audio pipeline failed to start")
        log("audio pipeline PLAYING -> sink '%s' (%d Hz, %d ch, AAC-LC)"
            % (AUDIO_SINK, rate, channels))

    def wrap_adts(self, data):
        """One raw AAC frame -> ADTS-framed bytes (what we push downstream)."""
        return adts_header(len(data), self._profile, self._sfidx,
                           self._ch) + data

    def push(self, data):
        buf = Gst.Buffer.new_wrapped(self.wrap_adts(data))
        buf.pts = Gst.CLOCK_TIME_NONE  # do-timestamp stamps it
        ret = self.src.emit("push-buffer", buf)
        if ret != Gst.FlowReturn.OK:
            raise RuntimeError("audio appsrc push: %s" % ret)

    def teardown(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)


def ensure_virtual_mic():
    """Idempotently create the ipad_usb_mic null-sink (same as ipad-usb-webcam server)."""
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
    try:
        out = subprocess.run(["pactl", "list", "short", "sinks"],
                             capture_output=True, text=True, env=env,
                             timeout=5).stdout
        if AUDIO_SINK in out:
            return
        subprocess.run(
            ["pactl", "load-module", "module-null-sink",
             "sink_name=%s" % AUDIO_SINK,
             "sink_properties=device.description=ipad-usb-webcam_Mic"],
            env=env, capture_output=True, timeout=5)
        log("created null-sink '%s'" % AUDIO_SINK)
    except Exception as e:
        log("WARN: virtual mic setup skipped: %s" % e)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
def reader_thread(sock, out_q):
    try:
        r = IucmReader(sock)
        n_audio = 0
        while True:
            mtype, flags, payload = r.read_message()
            if mtype == 0x14:
                n_audio += 1
                if n_audio % 50 == 1:
                    log("reader: audio frame #%d (len=%d)"
                        % (n_audio, len(payload)))
            out_q.put((mtype, flags, payload))
    except Exception as e:
        out_q.put(e)


def session_loop():
    if DIRECT_TCP:
        host, port = DIRECT_TCP.rsplit(":", 1)
        sock = socket.create_connection((host, int(port)), timeout=5)
        log("direct TCP test mode -> %s:%s" % (host, port))
    else:
        sock = usbmux_connect()

    q = queue.Queue()
    threading.Thread(target=reader_thread, args=(sock, q), daemon=True).start()

    vpipe = apipe = None
    hello_minor = 0
    last_pong = time.monotonic()
    last_ping = time.monotonic()  # first PING only after the handshake settles
    stats_count = 0
    cur_cam = 0  # active camera id from STATS (drives per-camera flip)
    frames = 0
    audio_rx = 0
    audio_seen = 0
    seen_types = {}
    t0 = time.monotonic()

    def send_stop():
        try:
            sock.sendall(iucm_frame(T_STOP, 0))
        except Exception:
            pass

    try:
        while True:
            # Keepalive: PING every 2s; reconnect if no PONG for ~6s (§4.3).
            now = time.monotonic()
            if now - last_ping >= 2.0:
                sock.sendall(iucm_frame(
                    T_PING, 0, struct.pack("<Q", int(now * 1e6))))
                last_ping = now
            if now - last_pong > 8.0:
                raise ConnectionError("keepalive timeout (no PONG ~6s)")

            try:
                item = q.get(timeout=0.2)
            except queue.Empty:
                continue
            if isinstance(item, Exception):
                raise item
            mtype, flags, payload = item

            if mtype == T_HELLO:
                major, hello_minor, devname, appver, cams = parse_hello(payload)
                if major != 1:
                    raise RuntimeError("unsupported protocol major %d" % major)
                log("HELLO: %s | app %s | IUCM %d.%d | cameras: %s"
                    % (devname, appver, major, hello_minor, cams))
                cam_id = cams[0]["id"] if cams else 0
                # CLIENT_INFO (§4.11): identify as tool, once, before START.
                name = b"ipad-usb-webcam Pro Bridge"
                ver = b"1.0.0"
                # kind: 1=obsPlugin 2=macApp (§4.11) — some app builds gate
                # audio delivery on the receiver kind; pose as obsPlugin.
                sock.sendall(iucm_frame(T_CLIENT_INFO, 0,
                                        bytes([1, len(name)]) + name
                                        + bytes([len(ver)]) + ver))
                # START (§4.2): 11-byte form vs 1.0, +flags(audio) vs >=1.1.
                start = struct.pack("<BHHHI", cam_id, WIDTH, HEIGHT, FPS,
                                    BITRATE_KBPS)
                if hello_minor >= 1:
                    start += bytes([0x01])  # bit 0: audio requested
                sock.sendall(iucm_frame(T_START, 0, start))
                log("START sent: %dx%d@%d @ %d kbps, audio=%s"
                    % (WIDTH, HEIGHT, FPS, BITRATE_KBPS, hello_minor >= 1))

            elif mtype == T_CONFIG:
                w, h, fps, hvcC = parse_config(payload)
                log("CONFIG: active %dx%d@%d, hvcC %d bytes"
                    % (w, h, fps, len(hvcC)))
                if vpipe is None:
                    vpipe = VideoPipeline(hvcC)

            elif mtype == T_AUDIO_CONFIG:
                rate, ch, codec, asc = parse_audio_config(payload)
                if codec != 1:
                    log("WARN: unknown audio codec %d — audio skipped" % codec)
                elif apipe is None:
                    apipe = AudioPipeline(rate, ch, asc)

            elif mtype == T_VIDEO:
                if vpipe and len(payload) > 8:
                    vpipe.push(payload[8:])
                    frames += 1

            elif mtype == T_AUDIO:
                audio_seen += 1
                if audio_seen % 50 == 1:
                    log("T_AUDIO seen=%d apipe=%s plen=%d"
                        % (audio_seen, apipe is not None, len(payload)))
                if apipe and len(payload) > 8:
                    if DUMP_AAC:
                        with open(DUMP_AAC, "ab") as f:
                            f.write(payload[8:])
                    hdr = apipe.wrap_adts(payload[8:])
                    if DUMP_AAC:
                        with open(DUMP_AAC + ".adts", "ab") as f:
                            f.write(hdr)
                    apipe.push(payload[8:])
                    audio_rx += 1
                    if audio_rx % 100 == 0:
                        states = []
                        for nm in ("asrc", "aacparse0", "avdec_aac0"):
                            e = apipe.pipeline.get_by_name(nm)
                            if e:
                                _, st, _ = e.get_state(0)
                                states.append("%s=%s" % (
                                    nm, Gst.Element.state_get_name(st)))
                        log("audio rx: %d [%s]" % (audio_rx, " ".join(states)))

            elif mtype == T_PONG:
                last_pong = time.monotonic()

            elif mtype == T_STATS:
                stats_count += 1
                st = parse_stats(payload)
                if vpipe is not None:
                    cam = st.get("camera_id", 0)
                    if cam != cur_cam:
                        cur_cam = cam
                        m = FLIP_FRONT if cam == 1 else FLIP_BACK
                        vpipe.set_flip(FLIP_METHODS.get(m, 0),
                                       " (cam %d -> %s)" % (cam, m))
                if stats_count % 10 == 1:
                    rate = frames / max(time.monotonic() - t0, 1e-6)
                    log("stats: dropped=%s src=%s out=%s audio=%s muted=%s flags=0x%02X | rx %.1f fps"
                        % (st.get("dropped_frames"), st.get("source"),
                           st.get("output"), st.get("audio_active"),
                           st.get("audio_muted"), st.get("flags"), rate))
                    log("stats-orient: continuous=%.1f° sector=%d residual=%.1f° "
                        "autoRot=%s leveling=%s flatHold=%s cam=%d"
                        % (st.get("continuous_deg", 0), st.get("sector", 0),
                           st.get("residual_deg", 0), st.get("auto_rotation"),
                           st.get("horizon_leveling"), st.get("flat_hold"),
                           st.get("camera_id", 0)))

            elif mtype == T_ERROR:
                code, text = parse_error(payload)
                if code == 6:  # MIC_DENIED — non-fatal, video continues
                    log("WARN: mic denied on iPad — video only")
                else:
                    raise RuntimeError("device ERROR %d: %s" % (code, text))

            elif mtype == T_PING:
                ts = struct.unpack("<Q", payload[:8])[0]
                sock.sendall(iucm_frame(T_PONG, 0, struct.pack("<Q", ts)))

            seen_types[mtype] = seen_types.get(mtype, 0) + 1
            if mtype not in (0x11, 0x12, 0x20, 0x21) and seen_types[mtype] <= 3:
                log("MSG type=0x%02X flags=0x%02X len=%d head=%s"
                    % (mtype, flags, len(payload),
                       payload[:24].hex()))
    finally:
        send_stop()
        if vpipe:
            vpipe.teardown()
        if apipe:
            apipe.teardown()
        try:
            sock.close()
        except Exception:
            pass


def main():
    ensure_virtual_mic()
    running = {"flag": True}

    def on_term(_sig, _frm):
        running["flag"] = False
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_term)
    while running["flag"]:
        try:
            log("connecting…")
            session_loop()
            log("session ended")
        except KeyboardInterrupt:
            log("bye")
            return 0
        except Exception as e:
            log("session failed: %s" % e)
        time.sleep(2.0)  # reconnect backoff
    return 0


if __name__ == "__main__":
    sys.exit(main())
