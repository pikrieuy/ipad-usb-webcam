#!/usr/bin/env python3
"""Probe variant: appsrc with BYTE-STREAM (annexb) from dump file."""
import subprocess, sys, time
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GObject

Gst.init(None)
V4L2_DEVICE = "/dev/video10"

data = open("/tmp/dump_annexb.h265", "rb").read()
print("dump bytes:", len(data))

# Split into NALs (start-code separated)
nals = []
i = 0
while i < len(data):
    assert data[i:i+4] == b"\x00\x00\x00\x01", "bad start code at %d" % i
    j = data.find(b"\x00\x00\x00\x01", i + 4)
    end = j if j != -1 else len(data)
    nals.append(data[i+4:end])
    i = end
print("NALs:", len(nals), "first types:", [(n[0] >> 1) & 0x3F for n in nals[:6]])

caps = Gst.Caps.from_string(
    "video/x-h265,stream-format=byte-stream,alignment=au")
el = Gst.ElementFactory.make
p = Gst.Pipeline.new("probe2")
src = el("appsrc", "vsrc")
parse = el("h265parse")
dec = el("avdec_h265")
conv = el("videoconvert")
sink = el("v4l2sink")
for e in (src, parse, dec, conv, sink):
    p.add(e)
print("links:", src.link(parse), parse.link(dec), dec.link(conv),
      conv.link_filtered(sink, Gst.Caps.from_string(
          "video/x-raw,format=YUY2")))
src.set_property("is-live", True)
src.set_property("do-timestamp", True)
src.set_property("format", Gst.Format.TIME)
src.set_property("caps", caps)
src.set_property("max-bytes", 16 * 1024 * 1024)
src.set_property("block", True)
sink.set_property("device", V4L2_DEVICE)
sink.set_property("sync", False)
sink.set_property("qos", False)
ret = p.set_state(Gst.State.PLAYING)
print("set_state:", ret.value_name)

import struct as st
time.sleep(0.2)
t0 = time.monotonic()
n = 0
for nal in nals:
    nal_type = (nal[0] >> 1) & 0x3F
    pts = int((time.monotonic() - t0) * 1e9)
    buf = Gst.Buffer.new_wrapped(b"\x00\x00\x00\x01" + nal)
    buf.pts = pts
    buf.duration = 33333333
    if nal_type in (32, 33, 34, 39, 40):  # VPS/SPS/PPS/SEI -> header
        buf.set_flags(Gst.BufferFlags.HEADER)
    else:  # slice: mark P-frames as delta, IDR = no delta flag
        is_idr = nal_type in (16, 17, 18, 19, 20, 21)
        if not is_idr:
            buf.set_flags(Gst.BufferFlags.DELTA_UNIT)
    ret = src.emit("push-buffer", buf)
    if ret != Gst.FlowReturn.OK:
        print("push fail:", ret)
        break
    n += 1
    if n % 20 == 0:
        _, st, pend = p.get_state(0)
        print("pushed %d NALs (%.1fs) pipeline=%s" % (
            n, time.monotonic() - t0, Gst.Element.state_get_name(st)))
        cur = parse.get_static_pad("src").get_current_caps()
        if cur:
            print("  parse.src caps:", cur.to_string()[:110])
        if st == Gst.State.PLAYING:
            print("*** PLAYING reached at NAL %d ***" % n)
            break
    time.sleep(0.01)

r = subprocess.run(["timeout", "6", "ffmpeg", "-hide_banner",
                    "-loglevel", "error", "-f", "v4l2",
                    "-i", V4L2_DEVICE, "-frames:v", "2",
                    "/tmp/probe2_%d.jpg", "-y"],
                   capture_output=True, text=True)
print("grab rc=%d %s" % (r.returncode, r.stderr.strip()[:100]))
r2 = subprocess.run(["ls", "-la", "/tmp/probe2_*.jpg"],
                    capture_output=True, text=True)
print(r2.stdout.strip() or "no frames")
p.set_state(Gst.State.NULL)
