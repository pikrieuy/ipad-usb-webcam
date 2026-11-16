#!/usr/bin/env python3
"""Standalone sim audio pipeline debug with bus error reporting."""
import time
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

Gst.init(None)
el = Gst.ElementFactory.make
p = Gst.Pipeline.new("aud")
src = el("audiotestsrc")
src.set_property("is-live", True)
src.set_property("wave", 0)
conv = el("audioconvert")
rs = el("audioresample")
cf = el("capsfilter")
cf.set_property("caps", Gst.Caps.from_string(
    "audio/x-raw,rate=48000,channels=1,layout=interleaved"))
enc = el("avenc_aac")
enc.set_property("bitrate", 64000)
sink = el("appsink")
sink.set_property("sync", False)
sink.set_property("max-buffers", 8)
sink.set_property("drop", True)
elems = [src, conv, rs, cf, enc, sink]
for i, e in enumerate(elems):
    if e is None:
        print("MISSING element at index", i)
        raise SystemExit(1)
    p.add(e)
print("links:", src.link(conv), conv.link(rs), rs.link(cf),
      cf.link(enc), enc.link(sink))
bus = p.get_bus()
ret = p.set_state(Gst.State.PLAYING)
print("set_state:", ret.value_name)
ok, st, pend = p.get_state(3 * Gst.SECOND)
print("get_state:", ok.value_name, st.value_name, pend.value_name)
while True:
    msg = bus.pop_filtered(Gst.MessageType.ERROR)
    if not msg:
        break
    err, dbg = msg.parse_error()
    print("BUS ERROR from %s: %s | %s" % (msg.src.get_name(), err, dbg[:200]))
# try pulling anyway
s = sink.emit("try-pull-sample", 500 * Gst.MSECOND)
print("sample:", bool(s), s.get_caps().to_string() if s else "")
if s:
    val = s.get_caps().get_structure(0).get_value("codec_data")
    print("codec_data:", bool(val), val.get_size() if val else 0)
p.set_state(Gst.State.NULL)
