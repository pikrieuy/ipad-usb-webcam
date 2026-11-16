#!/usr/bin/env python3
"""ipad-usb-webcam resolution-switch test: 1080p -> 720p -> 480p mid-stream.
Verifies the server rebuilds its GStreamer pipeline with matching caps and
that /stats reports the updated resolution."""
import asyncio, io, json, ssl, sys
import aiohttp
from PIL import Image, ImageDraw

WS = 'wss://localhost:8443/ws'
STATS = 'https://localhost:8443/stats'

def frame(w, h, i):
    img = Image.new('RGB', (w, h), (i, 100, 200))
    d = ImageDraw.Draw(img)
    d.rectangle([(i*20) % w, 0, (i*20) % w + 150, h], fill=(255, 200, 0))
    buf = io.BytesIO(); img.save(buf, 'JPEG', quality=82)
    return buf.getvalue()

async def get_res(sess, ctx):
    async with sess.get(STATS, ssl=ctx) as r:
        return (await r.json())['resolution']

async def main():
    ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    results = []
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(WS, ssl=ctx) as ws:
            await ws.send_json({'cmd':'start'}); await ws.receive(timeout=5)
            for (w,h) in [(1920,1080),(1280,720),(854,480)]:
                # stream a few frames at this res
                for i in range(4):
                    await ws.send_bytes(frame(w,h,i))
                    await asyncio.sleep(0.05)
                await asyncio.sleep(0.6)  # let pipeline rebuild settle
                res = await get_res(s, ctx)
                expected = f'{w}x{h}@30'
                ok = res == expected
                results.append(ok)
                print(f'  sent {w}x{h} -> server pipeline: {res}  expected {expected}  [{"OK" if ok else "FAIL"}]')
            await ws.send_json({'cmd':'stop'})
    print('RESULT:', 'PASS' if all(results) else 'FAIL')
    return 0 if all(results) else 1
sys.exit(asyncio.run(main()))
