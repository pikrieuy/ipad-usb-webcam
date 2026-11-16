#!/usr/bin/env python3
"""
ipad-usb-webcam concurrent E2E — streams continuously in the background while
v4l2-ctl grabs frames simultaneously. Proves /dev/video10 is a real,
live, consumable webcam source (Iron Law: real evidence only).
"""
import asyncio
import io
import subprocess
import sys
import threading
import time

import aiohttp

HOST = "127.0.0.1"
PORT = 8443
WS_URL = "wss://%s:%d/ws" % (HOST, PORT)
STATS_URL = "https://%s:%d/stats" % (HOST, PORT)
V4L2 = "/dev/video10"


def make_frame(i, w=1920, h=1080):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), (30, 120, 200))
    d = ImageDraw.Draw(img)
    # moving bar so consecutive frames differ
    x = (i * 20) % w
    d.rectangle([x, 0, x + 200, h], fill=(255, 200, 0))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


async def fetch_target_res():
    """Read the server's configured capture resolution from /stats."""
    import ssl as _ssl
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(STATS_URL, ssl=ctx) as r:
                data = await r.json()
        res = data.get("resolution", "1920x1080@30")
        wh = res.split("@")[0]
        w, h = wh.split("x")
        return int(w), int(h)
    except Exception:
        return 1920, 1080


async def streamer(stop_evt, result, w, h):
    import ssl as _ssl
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    async with aiohttp.ClientSession() as sess:
        async with sess.ws_connect(WS_URL, ssl=ctx) as ws:
            await ws.send_json({"cmd": "start"})
            await ws.receive(timeout=5)
            i = 0
            while not stop_evt.is_set():
                await ws.send_bytes(make_frame(i, w, h))
                i += 1
                await asyncio.sleep(1 / 30.0)
            result["frames_sent"] = i
            await ws.send_json({"cmd": "stop"})


def v4l2_grabber(result):
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", V4L2, "--stream-mmap",
             "--stream-count=10", "--stream-to=/dev/null"],
            capture_output=True, text=True, timeout=15)
        result["v4l2_rc"] = out.returncode
        result["v4l2_out"] = (out.stdout + out.stderr).strip()
    except subprocess.TimeoutExpired:
        result["v4l2_rc"] = -1
        result["v4l2_out"] = "TIMEOUT (no frames produced)"
    except Exception as e:
        result["v4l2_rc"] = -2
        result["v4l2_out"] = str(e)


async def main():
    stop_evt = asyncio.Event()
    res = {"frames_sent": 0}
    import ssl as _ssl
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE

    stream_task = asyncio.create_task(streamer(stop_evt, res, *await fetch_target_res()))
    await asyncio.sleep(1.5)  # let pipeline reach PLAYING

    # Run v4l2-ctl in a thread WHILE the streamer pushes frames
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, v4l2_grabber, res)

    stop_evt.set()
    await stream_task

    print("[test] frames streamed by client :", res.get("frames_sent"))
    print("[test] v4l2-ctl return code       :", res.get("v4l2_rc"))
    print("[test] v4l2-ctl output            :", repr(res.get("v4l2_out"))[:300])

    async with aiohttp.ClientSession() as sess:
        async with sess.get(STATS_URL, ssl=ctx) as r:
            data = await r.json()
    print("[test] server frames_in           :", data.get("frames_in"))
    print("[test] server gst_running         :", data.get("gst_running"))

    got_frames = (res.get("v4l2_rc") == 0)
    print("\n[test] RESULT:", "PASS — /dev/video10 produced real frames"
          if got_frames else "FAIL — v4l2 did not produce frames")
    return 0 if got_frames else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
