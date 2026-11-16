#!/usr/bin/env python3
"""ipad-usb-webcam A/V concurrent test: stream JPEG video + PCM audio together."""
import asyncio, json, math, struct, ssl, sys
import aiohttp

HOST = 'localhost'; PORT = 8443
WS = 'wss://%s:%d/ws' % (HOST, PORT)
RATE = 48000; SPC = 4800  # 100ms mono S16LE per chunk

def tone(start):
    pcm = bytearray()
    for i in range(SPC):
        s = 0.4*math.sin(2*math.pi*440*(start+i)/RATE)
        pcm += struct.pack('<h', int(max(-1,min(1,s))*32767))
    return b'\x01\x00\x00\x00' + bytes(pcm)

async def sink_state():
    import subprocess
    out = subprocess.run(['pactl','list','sinks'],capture_output=True,text=True).stdout
    for seg in out.split('Sink #'):
        if 'Name: ipad_usb_mic' in seg:
            for l in seg.splitlines():
                if 'State:' in l: return l.split('State:',1)[1].strip()
    return 'MISSING'

async def main():
    ssl_ctx = ssl.create_default_context(); ssl_ctx.check_hostname=False; ssl_ctx.verify_mode=ssl.CERT_NONE
    frames=0; a_bytes=0
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(WS, ssl=ssl_ctx) as ws:
            await ws.send_str(json.dumps({'cmd':'start'}))
            await ws.send_str(json.dumps({'cmd':'audio_start'}))
            # tiny 320x240 JPEG-ish frame (just valid header bytes)
            jpeg = bytes([0xFF,0xD8]) + b'\x00'*1024 + bytes([0xFF,0xD9])
            start=0
            for n in range(30):
                await ws.send_bytes(jpeg); frames+=1
                await ws.send_bytes(tone(start)); start+=SPC; a_bytes+=SPC*2
                await asyncio.sleep(0.033)
            await asyncio.sleep(0.8)
            st = await sink_state()
            await ws.send_str(json.dumps({'cmd':'audio_stop'}))
            await ws.send_str(json.dumps({'cmd':'stop'}))
    print('frames sent:', frames, '| audio bytes:', a_bytes)
    print('ipad_usb_mic sink state:', st)
    ok = (st=='RUNNING' and frames==30)
    print('RESULT:', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1
sys.exit(asyncio.run(main()))
