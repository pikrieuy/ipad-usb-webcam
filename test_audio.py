#!/usr/bin/env python3
"""
ipad-usb-webcam audio E2E test.

Opens the WSS endpoint, sends a few seconds of synthetic 48kHz mono S16LE
tone PCM (each chunk prefixed with the 4-byte audio marker the server
expects), and checks that the server spun up the audio pipeline and the
'ipad_usb_mic' PulseAudio/PipeWire sink transitions to RUNNING.

Run from the laptop:
    ~/ipad-usb-webcam/.venv/bin/python ~/ipad-usb-webcam/test_audio.py <ts-hostname> [port]
"""
import asyncio
import json
import math
import struct
import ssl
import sys

import aiohttp

HOST = sys.argv[1] if len(sys.argv) > 1 else "localhost"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8443
WS_URL = "wss://%s:%d/ws" % (HOST, PORT)

RATE = 48000
CHUNK_SECONDS = 0.1          # 100ms of audio per chunk
SAMPLES_PER_CHUNK = int(RATE * CHUNK_SECONDS)
NUM_CHUNKS = 20              # ~2 seconds total
FREQ = 440.0                 # 440Hz test tone


def make_tone_chunk(start_sample: int) -> bytes:
    """Generate one chunk of S16LE mono PCM sine wave."""
    pcm = bytearray()
    for i in range(SAMPLES_PER_CHUNK):
        t = (start_sample + i) / RATE
        s = 0.5 * math.sin(2 * math.pi * FREQ * t)
        pcm += struct.pack("<h", int(max(-1, min(1, s)) * 32767))
    # Prefix with the 4-byte audio marker: 0x01 0x00 0x00 0x00
    return b"\x01\x00\x00\x00" + bytes(pcm)


async def sink_running() -> bool:
    """Check whether the ipad_usb_mic sink is RUNNING via pactl."""
    import subprocess
    try:
        out = subprocess.run(
            ["pactl", "list", "sinks"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        # Find the ipad_usb_mic block and look at its State.
        block = None
        for seg in out.split("Sink #"):
            if "ipad_usb_mic" in seg and "Name: ipad_usb_mic" in seg:
                block = seg
                break
        if block is None:
            print("    sink 'ipad_usb_mic' not found")
            return False
        for line in block.splitlines():
            if "State:" in line:
                st = line.split("State:", 1)[1].strip()
                print("    sink state:", st)
                return st == "RUNNING"
    except Exception as e:
        print("    pactl check failed:", e)
    return False


async def main() -> int:
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    ok = False
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(WS_URL, ssl=ssl_ctx) as ws:
            # Ask the server to start the audio pipeline.
            await ws.send_str(json.dumps({"cmd": "audio_start"}))
            reply = await ws.receive_json()
            print("[audio_start reply]", reply)
            if not reply.get("audio"):
                print("RESULT: FAIL (server did not start audio)")
                return 1

            start = 0
            for n in range(NUM_CHUNKS):
                await ws.send_bytes(make_tone_chunk(start))
                start += SAMPLES_PER_CHUNK
                await asyncio.sleep(CHUNK_SECONDS * 0.5)  # faster than realtime

            # Give pulsesink a moment to consume and move to RUNNING.
            await asyncio.sleep(1.0)
            ok = await sink_running()

            await ws.send_str(json.dumps({"cmd": "audio_stop"}))

    print("RESULT:", "PASS" if ok else "FAIL",
          "(ipad_usb_mic sink %s)" % ("RUNNING" if ok else "not RUNNING"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
