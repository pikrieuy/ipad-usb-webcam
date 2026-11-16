#!/usr/bin/env python3
"""Which boxing method keeps codec_data in caps?"""
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GObject
Gst.init(None)

data = bytes(range(120))  # fake hvcC

# Method A: direct set_value with Gst.Buffer
caps_a = Gst.Caps.from_string("video/x-h265,stream-format=hvc1,alignment=au")
try:
    caps_a.get_structure(0).set_value("codec_data",
                                      Gst.Buffer.new_wrapped(data))
    print("A:", caps_a.get_structure(0).to_string()[:110])
except Exception as e:
    print("A failed:", e)

# Method B: GObject.Value init+set_boxed
caps_b = Gst.Caps.from_string("video/x-h265,stream-format=hvc1,alignment=au")
try:
    cval = GObject.Value()
    cval.init(Gst.Buffer.__gtype__)
    cval.set_boxed(Gst.Buffer.new_wrapped(data))
    caps_b.get_structure(0).set_value("codec_data", cval)
    print("B:", caps_b.get_structure(0).to_string()[:110])
except Exception as e:
    print("B failed:", e)

# Method C: caps_from_string can't carry binary — use Caps.new_full style
caps_c = Gst.Caps.new_empty_simple("video/x-h265")
s = caps_c.get_structure(0)
s.set_value("stream-format", "hvc1")
s.set_value("alignment", "au")
try:
    cval = GObject.Value(Gst.Buffer.__gtype__, Gst.Buffer.new_wrapped(data))
    s.set_value("codec_data", cval)
    print("C:", s.to_string()[:110])
except Exception as e:
    print("C failed:", e)
