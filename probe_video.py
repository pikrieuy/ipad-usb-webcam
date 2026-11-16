#!/usr/bin/env python3
"""Isolate the bridge video pipeline: real IUCM stream -> appsrc -> /dev/video10.
Logs per-element state + GST warnings. Laptop-only diagnostic."""
import socket, struct, subprocess, sys, time
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GObject

Gst.init(None)
MAGIC = b"IUCM"
V4L2_DEVICE = "/dev/video10"


def recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("EOF")
        buf += chunk
    return buf


def read_msg(s):
    while True:
        hdr = recv_exact(s, 12)
        if hdr[:4] != MAGIC:
            continue
        mtype, flags = hdr[4], hdr[5]
        length = struct.unpack("<I", hdr[8:12])[0]
        return mtype, flags, recv_exact(s, length) if length else b""


def frame(mtype, flags, payload=b""):
    return MAGIC + bytes([mtype, flags]) + b"\x00\x00" \
        + struct.pack("<I", len(payload)) + payload


def make_pipeline(hvcC, width=1920, height=1080):
    caps = Gst.Caps.from_string(
        "video/x-h265,stream-format=hvc1,alignment=au")
    caps.get_structure(0).set_value(
        "codec_data", GObject.Value(Gst.Buffer.__gtype__,
                                    Gst.Buffer.new_wrapped(hvcC)))
    el = Gst.ElementFactory.make
    p = Gst.Pipeline.new("probe")
    src = el("appsrc", "vsrc")
    parse = el("h265parse")
    dec = el("avdec_h265")
    conv = el("videoconvert")
    sink = el("v4l2sink")
    for e in (src, parse, dec, conv, sink):
        p.add(e)
    print("link src->parse:", src.link(parse))
    print("link parse->dec:", parse.link(dec))
    print("link dec->conv:", dec.link(conv))
    print("link conv->sink:", conv.link_filtered(
        sink, Gst.Caps.from_string("video/x-raw,format=YUY2")))
    src.set_property("is-live", True)
    src.set_property("do-timestamp", False)  # manual PTS below
    src.set_property("format", Gst.Format.TIME)
    src.set_property("caps", caps)
    src.set_property("max-bytes", 16 * 1024 * 1024)
    src.set_property("block", True)
    sink.set_property("device", V4L2_DEVICE)
    sink.set_property("sync", False)
    sink.set_property("qos", False)
    bus = p.get_bus()
    bus.add_signal_watch_full(0)
    # GLib main loop-less: poll bus manually later.
    ret = p.set_state(Gst.State.PLAYING)
    print("set_state:", ret.value_name)
    return p, src, parse, dec, conv, sink, bus


def dump_states(p, src, parse, dec, conv, sink):
    for name, e in (("pipeline", p), ("appsrc", src), ("h265parse", parse),
                    ("avdec_h265", dec), ("videoconvert", conv),
                    ("v4l2sink", sink)):
        _, st, pend = e.get_state(0)
        print("  %-12s %s (pending %s)" % (
            name, Gst.Element.state_get_name(st),
            Gst.Element.state_get_name(pend)))
    for name, e, pad in (("appsrc.src", src, "src"),
                         ("parse.src", parse, "src"),
                         ("dec.src", dec, "src"),
                         ("conv.src", conv, "src"),
                         ("sink.sink", sink, "sink")):
        pl = e.get_static_pad(pad)
        caps = pl.get_current_caps() or pl.query_caps(None)
        print("  %-10s %s" % (name, caps.to_string()[:140]))


def main():
    s = socket.create_connection(("127.0.0.1", 7878), timeout=5)
    s.settimeout(None)
    mtype, _f, p_hello = read_msg(s)
    print("HELLO type=0x%02X len=%d" % (mtype, len(p_hello)))
    name = b"probe"
    s.sendall(frame(0x04, 0, bytes([3, len(name)]) + name
                    + bytes([2]) + b"1.0"))
    start = struct.pack("<BHHHI", 0, 1920, 1080, 30, 12000)
    s.sendall(frame(0x02, 0x01, start))  # flags=0x01 audio wanted
    hvcC = None
    deadline = time.time() + 10
    while time.time() < deadline and hvcC is None:
        mtype, _f, p = read_msg(s)
        if mtype == 0x10:  # CONFIG
            w, h, fps, hlen = struct.unpack("<HHHI", p[:10])
            hvcC = p[10:10 + hlen]
            print("CONFIG %dx%d@%d hvcC=%d bytes" % (w, h, fps, len(hvcC)))
    if hvcC is None:
        print("NO CONFIG — abort"); return 1
    p, src, parse, dec, conv, sink, bus = make_pipeline(hvcC)
    n = 0
    t0 = time.monotonic()
    while n < 90:
        mtype, _f, pay = read_msg(s)
        if mtype == 0x20:  # PING
            s.sendall(frame(0x21, 0, pay))
            continue
        if mtype != 0x11:  # VIDEO
            continue
        buf = Gst.Buffer.new_wrapped(pay[8:])
        buf.pts = n * 33333333  # manual 30fps PTS
        buf.duration = 33333333
        ret = src.emit("push-buffer", buf)
        if ret != Gst.FlowReturn.OK:
            print("push-buffer:", ret); break
        n += 1
        if n in (5, 15, 30, 60, 90):
            _, st, pend = p.get_state(0)
            print("frame %d (%.1fs) pipeline=%s pend=%s" % (
                n, time.monotonic() - t0,
                Gst.Element.state_get_name(st),
                Gst.Element.state_get_name(pend)))
            if n == 30:
                print("per-element states:")
                dump_states(p, src, parse, dec, conv, sink)
                msg = bus.timed_pop_filtered(
                    0, Gst.MessageType.ERROR | Gst.MessageType.WARNING)
                while msg:
                    if msg.type == Gst.MessageType.ERROR:
                        err, dbg = msg.parse_error()
                        print("BUS ERROR: %s | %s" % (err, dbg))
                    else:
                        err = msg.parse_warning()
                        print("BUS WARN:", err)
                    msg = bus.timed_pop_filtered(
                        0, Gst.MessageType.ERROR | Gst.MessageType.WARNING)
    # grab attempt
    r = subprocess.run(["timeout", "6", "ffmpeg", "-hide_banner",
                        "-loglevel", "error", "-f", "v4l2",
                        "-i", V4L2_DEVICE, "-frames:v", "2",
                        "/tmp/probe_%d.jpg", "-y"],
                       capture_output=True, text=True)
    print("grab rc=%d %s" % (r.returncode, r.stderr.strip()[:120]))
    r2 = subprocess.run(["ls", "-la", "/tmp/probe_*.jpg"],
                        capture_output=True, text=True)
    print(r2.stdout.strip() or "no frames grabbed")
    p.set_state(Gst.State.NULL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
