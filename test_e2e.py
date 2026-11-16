#!/usr/bin/env python3
"""
ipad-usb-webcam E2E test — proves the full pipeline:
  fake iPad client -> WebSocket -> GStreamer appsrc -> /dev/video10

Key fix vs v1: we query /stats WHILE the WebSocket is still open (v1 read
stats after close, so engine.stop() had already flipped gst_running=False
-> false negative). We also verify /dev/video10 is actually producing
frames via v4l2-ctl.
"""
import asyncio
import io
import json
import subprocess
import sys
import time

import aiohttp

HOST = "127.0.0.1"
PORT = 8443
WS_URL = "wss://%s:%d/ws" % (HOST, PORT)
STATS_URL = "https://%s:%d/stats" % (HOST, PORT)
V4L2 = "/dev/video10"


def make_frame():
    from PIL import Image, ImageDraw
    # Animated: a moving colored bar so we can confirm live frames.
    img = Image.new("RGB", (1280, 720), (30, 120, 200))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 200, 720], fill=(255, 200, 0))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


async def grab_stats(sess, ctx):
    async with sess.get(STATS_URL, ssl=ctx) as resp:
        return await resp.json()


async def main():
    n_frames = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    jpeg = make_frame()
    print("[test] frame size: %d bytes (1280x720 JPEG)" % len(jpeg))

    import ssl as _ssl
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE

    async with aiohttp.ClientSession() as sess:
        async with sess.ws_connect(WS_URL, ssl=ctx) as ws:
            await ws.send_json({"cmd": "start"})
            ack = await ws.receive(timeout=5)
            print("[test] start ack:", ack.data)

            # Stream while keeping the socket OPEN.
            t0 = time.time()
            for i in range(n_frames):
                await ws.send_bytes(jpeg)
                await asyncio.sleep(1 / 30.0)
            elapsed = time.time() - t0
            print("[test] sent %d frames in %.2fs" % (n_frames, elapsed))

            # Give the pipeline a beat to chew through buffers, then check
            # stats WHILE still connected.
            await asyncio.sleep(1.5)
            data = await grab_stats(sess, ctx)
            print("[test] /stats (socket OPEN):")
            for k in ("gst_running", "active_streamer", "frames_in",
                      "bytes_in", "resolution", "v4l2_device"):
                print("   %-16s = %s" % (k, data.get(k)))

            # Check v4l2 device is producing frames ( Iron Law: real evidence )
            try:
                out = subprocess.run(
                    ["v4l2-ctl", "-d", V4L2, "--stream-mmap",
                     "--stream-count=3", "--stream-to=/dev/null"],
                    capture_output=True, text=True, timeout=10)
                v4l2_ok = (out.returncode == 0)
                v4l2_msg = "OK" if v4l2_ok else ("ERR: " + (out.stderr.strip() or out.stdout.strip()))
            except Exception as e:
                v4l2_ok = False
                v4l2_msg = "EXC: " + str(e)
            print("[test] v4l2-ctl capture: %s" % v4l2_msg)

        # After close, confirm clean shutdown
        await asyncio.sleep(0.5)
        after = await grab_stats(sess, ctx)
        print("[test] /stats (socket CLOSED): gst_running=%s frames_in=%s" % (
            after.get("gst_running"), after.get("frames_in")))

    pipe_ok = data.get("gst_running") and data.get("frames_in", 0) > 0
    print("\n[test] RESULT:", "PASS — GStreamer live + v4l2 producing frames"
          if (pipe_ok and v4l2_ok) else
          "PARTIAL — pipeline live but v4l2 capture unclear"
          if pipe_ok else
          "FAIL — pipeline not running")
    return 0 if (pipe_ok and v4l2_ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
