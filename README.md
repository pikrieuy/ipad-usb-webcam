# ipad-usb-webcam

Jadikan iPad Anda webcam native di Linux, lewat kabel USB. Tanpa WiFi, tanpa plugin browser.

iPad terdeteksi sebagai perangkat kamera beneran (video device virtual via `v4l2loopback`), dan mikrofonnya masuk ke PulseAudio sebagai audio input. Zoom, Meet, OBS, dan Discord langsung mengenalinya.

## Cara kerja

1. App **TetherCam** (gratis, App Store) di iPad mengirim video HEVC + audio AAC ke komputer lewat protokol `usbmuxd` (protokol USB yang sama dipakai iTunes/Finder).
2. Bridge Python di repo ini menerima stream tersebut lewat tunnel usbmuxd.
3. Video didecode dengan GStreamer dan disajikan ke `v4l2loopback` sebagai perangkat kamera (`/dev/video10`).
4. Audio didecode dan dirender ke PulseAudio sebagai source mikrofon.
5. Service systemd menjaga koneksi: colok kabel, buka app, kamera nyala. Kalau kabel lepas, dia nyambung sendiri lagi.

## Struktur

```
ipad_usb_webcam_bridge.py   Bridge utama: tunnel usbmuxd -> GStreamer -> v4l2 + PulseAudio
ipad_usb_webcam_server.py   Server status & kontrol (untuk web UI)
web/index.html              Dashboard status sederhana
systemd/                    Unit service untuk auto-start & auto-reconnect
test_*.py                   Test suite (E2E, concurrent, caps boxing, audio, simulasi IUCM)
probe_*.py                  Utilitas probing protokol & device
```

## Persyaratan

- Linux dengan:
  - `v4l2loopback` (modul kernel, perangkat kamera virtual)
  - GStreamer 1.x + plugin (`gstreamer1.0-plugins-{base,good,bad}`, plugin `v4l2sink`, decoder HEVC)
  - PulseAudio
  - `usbmuxd` (biasanya sudah ada)
- Python 3 dengan PyGObject (`gi`), Pillow
- iPad dengan app TetherCam + kabel USB

## Debugging note (bagian tersulit)

Pipeline pertama gagal diam-diam tanpa error. Penyebab: parameter H.265 (VPS/SPS/PPS) dari iPad hanya ada di dalam box `hvcC` dan tidak ikut mengalir dalam stream. Solusi di `ipad_usb_webcam_bridge.py`: parse parameter NAL dari `hvcC`, sisipkan sebelum frame pertama, dan selalu kirim video dalam bentuk Annex B.

## Catatan pengembangan

Project ini dibangun dengan bantuan AI agent (Hermes), dengan arah riset, debugging, dan keputusan desain tetap dari manusia. Protokol IUCM mengikuti spesifikasi publik TetherCam.
