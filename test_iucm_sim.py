#!/usr/bin/env python3
"""IUCM app-side simulator — offline test partner for ipad_usb_bridge.py.

Implements the APP role of the IUCM v1.2 protocol (per Kanevry/tethercam
protocol/PROTOCOL.md) with REAL hardware-spec HEVC encoding via x265enc and
optional AAC audio via avenc_aac. The bridge must not be able to tell this
apart from a real iPad (except the device name).

Usage (on the laptop):
  ~/ipad-usb-webcam/.venv/bin/python ~/ipad-usb-webcam/test/test_iucm_sim.py [--port 7878]
Then run the bridge with IPAD_USB_BRIDGE_TCP=127.0.0.1:7878.
"""
import argparse
import socket
import struct
import sys
import threading
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

MAGIC = b"IUCM"
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


def log(m):
    print("[sim %s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def frame(mtype, flags, payload=b""):
    return MAGIC + bytes([mtype, flags]) + b"\x00\x00" \
        + struct.pack("<I", len(payload)) + payload


def recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        c = s.recv(n - len(buf))
        if not c:
            raise ConnectionError("receiver closed")
        buf += c
    return buf


def read_message(s):
    while True:
        hdr = recv_exact(s, 12)
        if hdr[:4] != MAGIC:
            idx = hdr.find(b"I", 1)
            log("resync: bad hdr hex=%s" % hdr.hex())
            if idx > 0:
                s.recv(4096)  # resync heuristics — test client, be lenient
            continue
        mtype, flags = hdr[4], hdr[5]
        length = struct.unpack("<I", hdr[8:12])[0]
        return mtype, flags, recv_exact(s, length) if length else b""


def make_video_pipeline(w, h, fps, callback):
    """videotestsrc -> x265enc -> h265parse(hvc1,au) -> appsink."""
    el = Gst.ElementFactory.make
    p = Gst.Pipeline.new("sim-video")
    src = el("videotestsrc")
    src.set_property("is-live", True)
    src.set_property("pattern", "smpte")
    cf = el("capsfilter")
    cf.set_property("caps", Gst.Caps.from_string(
        "video/x-raw,width=%d,height=%d,framerate=%d/1" % (w, h, fps)))
    enc = el("x265enc")
    enc.set_property("tune", "zerolatency")
    enc.set_property("bitrate", 2000)
    enc.set_property("speed-preset", "ultrafast")
    enc.set_property("key-int-max", 30)  # IDR every ~1s (P-only start is undecodable)
    parse = el("h265parse")
    parse.set_property("config-interval", -1)  # VPS/SPS/PPS with every IDR
    capf = el("capsfilter")
    capf.set_property("caps", Gst.Caps.from_string(
        "video/x-h265,stream-format=hvc1,alignment=au"))
    sink = el("appsink")
    sink.set_property("sync", False)
    sink.set_property("max-buffers", 2)
    sink.set_property("drop", True)
    for e in (src, cf, enc, parse, capf, sink):
        p.add(e)
    src.link(cf)
    cf.link(enc)
    enc.link(parse)
    parse.link(capf)
    capf.link(sink)
    p.set_state(Gst.State.PLAYING)
    # Pull codec_data (hvcC) from the FIRST sample — but KEEP its data so
    # the receiver gets the IDR (params!) as video frame #1.
    hvcC = b""
    first_data = b""
    deadline = time.time() + 10
    while time.time() < deadline and not hvcC:
        sample = sink.emit("try-pull-sample", 100 * Gst.MSECOND)
        if not sample:
            time.sleep(0.05)
            continue
        caps = sample.get_caps()
        if caps:
            val = caps.get_structure(0).get_value("codec_data")
            if val:
                hvcC = bytes(val.extract_dup(0, val.get_size()))
        buf = sample.get_buffer()
        first_data = bytes(buf.extract_dup(0, buf.get_size()))
        callback(sample)
        break
    return p, sink, hvcC, first_data


def make_audio_pipeline(fps_cb):
    """audiotestsrc 48k mono -> avenc_aac (raw AAC) -> appsink."""
    try:
        el = Gst.ElementFactory.make
        p = Gst.Pipeline.new("sim-audio")
        src = el("audiotestsrc")
        src.set_property("is-live", True)
        src.set_property("wave", 0)  # sine
        conv = el("audioconvert")
        rs = el("audioresample")
        cf = el("capsfilter")
        # NOTE: avenc_aac (ffmpeg native) requires FLTP input — do NOT pin
        # S16LE here or the capsfilter<->enc link FAILS (measured).
        cf.set_property("caps", Gst.Caps.from_string(
            "audio/x-raw,rate=48000,channels=1,layout=interleaved"))
        enc = el("avenc_aac")
        enc.set_property("bitrate", 64000)
        sink = el("appsink")
        sink.set_property("sync", False)
        sink.set_property("max-buffers", 8)
        sink.set_property("drop", True)
        for e in (src, conv, rs, cf, enc, sink):
            if e is None:
                raise RuntimeError("missing element (avenc_aac?)")
            p.add(e)
        src.link(conv)
        conv.link(rs)
        rs.link(cf)
        cf.link(enc)
        enc.link(sink)
        p.set_state(Gst.State.PLAYING)
        ok, _, _ = p.get_state(3 * Gst.SECOND)
        if ok != Gst.StateChangeReturn.SUCCESS:
            p.set_state(Gst.State.NULL)
            log("audio pipeline failed to reach PLAYING")
            return None, None, b"", 0, 0
        sample = None
        deadline = time.time() + 5
        while time.time() < deadline and not sample:
            sample = sink.emit("try-pull-sample", 100 * Gst.MSECOND)
            if not sample:
                time.sleep(0.05)
        if not sample:
            p.set_state(Gst.State.NULL)
            log("audio: no AAC sample in 5s")
            return None, None, b"", 0, 0
        caps = sample.get_caps()
        asc = b""
        if caps:
            val = caps.get_structure(0).get_value("codec_data")
            if val:
                asc = bytes(val.extract_dup(0, val.get_size()))
        return p, sink, asc, 48000, 1
    except Exception as e:
        log("audio sim unavailable: %s" % e)
        return None, None, b"", 0, 0


def handle_client(conn):
    conn.settimeout(None)
    # 1. HELLO — protocol 1.2, one back camera.
    name = b"iPad Simulator (ipad-usb-webcam)"
    appver = b"0.4.0-sim"
    cam_name = b"Back Wide"
    hello = struct.pack("<H", 0x0102) + bytes([len(name)]) + name \
        + bytes([len(appver)]) + appver + bytes([1]) \
        + bytes([0, 0, len(cam_name)]) + cam_name
    conn.sendall(frame(T_HELLO, 0, hello))
    log("HELLO sent (proto 1.2)")

    # 2. Expect CLIENT_INFO (optional) and START.
    want_audio = False
    start = None
    for _ in range(6):
        mtype, _fl, p = read_message(conn)
        log("rx msg type=0x%02X len=%d hdr_hex=%s"
            % (mtype, len(p), p[:16].hex() if p else ""))
        if mtype == T_CLIENT_INFO:
            kind = p[0]
            log("CLIENT_INFO: kind=%d" % kind)
        elif mtype == T_START:
            start = p
            break
        elif mtype in (T_PING, T_PONG, T_STATS):
            log("ignoring keepalive type 0x%02X during handshake" % mtype)
        else:
            log("UNEXPECTED msg type 0x%02X — hexdump: %s"
                % (mtype, p[:64].hex()))
    if start is None:
        log("no START received — closing")
        return
    cam_id, w, h, fps, kbps = struct.unpack("<BHHHI", start[:11])
    want_audio = len(start) >= 12 and bool(start[11] & 0x01)
    log("START: cam=%d %dx%d@%d %d kbps audio=%s"
        % (cam_id, w, h, fps, kbps, want_audio))

    # 3. CONFIG with a real hvcC.
    frames_out = [0]
    audio_out = [0]
    vp, vsink, hvcC, first_data = make_video_pipeline(w, h, fps,
                                                      lambda s: None)
    if not hvcC:
        log("no hvcC from encoder — aborting")
        return
    conn.sendall(frame(T_CONFIG, 0,
                       struct.pack("<HHHI", w, h, fps, len(hvcC)) + hvcC))
    log("CONFIG sent (%dx%d@%d, hvcC %d bytes)" % (w, h, fps, len(hvcC)))

    ap = asink = None
    if want_audio:
        ap, asink, asc, rate, ch = make_audio_pipeline(None)
        if ap:
            conn.sendall(frame(T_AUDIO_CONFIG, 0,
                               struct.pack("<IBBH", rate, ch, 1, len(asc)) + asc))
            log("AUDIO_CONFIG sent (%d Hz, %d ch, ASC %d bytes)"
                % (rate, ch, len(asc)))

    stop = {"flag": False}

    def stats_loop():
        n = 0
        while not stop["flag"]:
            time.sleep(1.0)
            n += 1
            try:
                conn.sendall(frame(T_STATS, 0, bytes(22)))
            except Exception:
                return

    threading.Thread(target=stats_loop, daemon=True).start()

    def keepalive_loop():
        # Answer PINGs while the main thread streams video (§4.3).
        try:
            while not stop["flag"]:
                mtype, _fl, p = read_message(conn)
                if mtype == T_PING:
                    conn.sendall(frame(T_PONG, 0, p))
                elif mtype == T_START:
                    pass  # device re-asserting config — ignore
                elif mtype is None:
                    return
        except Exception:
            stop["flag"] = True

    threading.Thread(target=keepalive_loop, daemon=True).start()

    # Frame #1 = the IDR consumed for hvcC extraction — send it so the
    # receiver's decoder starts IMMEDIATELY (no waiting for keyint).
    if first_data:
        conn.sendall(frame(T_VIDEO, 0x01,
                           struct.pack("<Q", 0) + first_data))
        frames_out[0] += 1

    pts = 0
    pts_step = int(1e6 / fps)
    try:
        while True:
            sample = vsink.emit("try-pull-sample", 100 * Gst.MSECOND)
            if sample:
                buf = sample.get_buffer()
                data = bytes(buf.extract_dup(0, buf.get_size()))
                pts += pts_step
                conn.sendall(frame(T_VIDEO, 0x01 if (frames_out[0] % 60 == 0)
                                   else 0x00,
                                   struct.pack("<Q", pts) + data))
                frames_out[0] += 1
                if frames_out[0] % 90 == 0:
                    log("video frames sent: %d" % frames_out[0])
            if asink:
                s = asink.emit("try-pull-sample", 100 * Gst.MSECOND)
                if s:
                    b2 = s.get_buffer()
                    a = bytes(b2.extract_dup(0, b2.get_size()))
                    pts_a = pts  # same clock
                    conn.sendall(frame(T_AUDIO, 0,
                                       struct.pack("<Q", pts_a) + a))
                    audio_out[0] += 1
                    if audio_out[0] % 100 == 0:
                        log("audio frames sent: %d" % audio_out[0])
            if not sample and not (asink and s):
                time.sleep(0.01)  # nothing ready — brief yield
    except (BrokenPipeError, ConnectionResetError):
        log("receiver gone after %d video frames" % frames_out[0])
    finally:
        stop["flag"] = True
        vp.set_state(Gst.State.NULL)
        if ap:
            ap.set_state(Gst.State.NULL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7878)
    args = ap.parse_args()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", args.port))
    srv.listen(1)
    log("sim listening on 127.0.0.1:%d (one receiver at a time, BUSY after)"
        % args.port)
    while True:
        conn, addr = srv.accept()
        log("receiver connected: %s" % (addr,))
        try:
            handle_client(conn)
        except Exception as e:
            log("session error: %s" % e)
        finally:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
