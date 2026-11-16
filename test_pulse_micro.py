#!/usr/bin/env python3
"""Micro: does pulsesink connect from Python GI with the env?"""
import time
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

Gst.init(None)
p = Gst.Pipeline.new("t")
src = Gst.ElementFactory.make("audiotestsrc")
src.set_property("is-live", True)
sink = Gst.ElementFactory.make("pulsesink")
sink.set_property("device", "ipad_usb_mic")
sink.set_property("sync", False)
p.add(src)
p.add(src.link(sink) and sink or sink)
ok = src.link(sink)
print("link:", ok)
p.set_state(Gst.State.PLAYING)
time.sleep(2)
_, st, _ = p.get_state(0)
print("state:", Gst.Element.state_get_name(st))
bus = p.get_bus()
msg = bus.pop_filtered(Gst.MessageType.ERROR)
if msg:
    err, dbg = msg.parse_error()
    print("ERROR:", err, "|", dbg[:150])
else:
    print("no bus errors")
p.set_state(Gst.State.NULL)
