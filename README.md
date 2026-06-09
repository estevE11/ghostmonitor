# Ghost Monitor

Capture a macOS display (real or virtual) and stream it to an Android tablet's
browser over your local network — no Android app to install. The tablet becomes
a wireless secondary monitor / viewer.

Uses a low-latency **MJPEG** stream (`multipart/x-mixed-replace`), so the client
is just an `<img>` tag. Works in any modern mobile browser.

Capture uses Apple's **ScreenCaptureKit** (macOS 12.3+): hardware-accelerated,
up to 60 fps, frames emitted only on content change, and the real hardware
cursor composited natively. (The earlier `mss` backend capped at ~17 fps.)

```
ghostmonitor/
├── app/
│   ├── __init__.py
│   ├── cli.py            # CLI parsing + Uvicorn launch + LAN IP discovery
│   ├── capture.py        # ScreenCaptureKit capture + OpenCV JPEG encode
│   ├── server.py         # FastAPI MJPEG stream + serves index.html
│   └── templates/
│       └── index.html    # full-screen web receiver
├── requirements.txt
└── README.md
```

## Install

Python 3.10+ recommended.

```bash
cd ghostmonitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## macOS Screen Recording permission

The first capture triggers a system prompt:
**System Settings → Privacy & Security → Screen Recording** → enable your
terminal (Terminal.app / iTerm) or Python. You may need to restart the terminal
afterward. Without this, frames come back black.

## Usage

List displays:

```bash
python -m app.cli list-displays
```

```
Available displays:
  [0] Built-in Display 2560x1600 at (0, 0)
  [1] Virtual 16:10 1920x1200 at (-1920, 0)
```

> Index `0` is the first display (usually built-in). A **virtual display**
> (BetterDisplay, a dummy HDMI plug, etc.) shows up as its own index with its
> name — pick that index to stream only the virtual monitor.

Start streaming the virtual display at 60 fps:

```bash
python -m app.cli start --display 1 --port 8080 --fps 60 --quality 80
```

```
  Ghost Monitor
  display : 1
  fps     : 60
  quality : 80

  Open this URL on your Android tablet:
      http://192.168.1.50:8080

  (local: http://127.0.0.1:8080)
  Press Ctrl+C to stop.
```

Type that URL into the tablet's browser, tap **⛶ Fullscreen** for an immersive,
chrome-free view.

### Options for `start`

| Flag | Default | Meaning |
|------|---------|---------|
| `--display` | `0` | Display index (see `list-displays`). |
| `--port` | `8080` | Server port. |
| `--fps` | `30` | Target frames/sec. |
| `--quality` | `80` | JPEG quality 1–100. Lower = less bandwidth. |
| `--max-width` | none | Downscale frames wider than N px to save bandwidth. |
| `--host` | `0.0.0.0` | Bind address. |

## Tuning latency / bandwidth

A 4K display at 30 fps is a lot of pixels. If the stream lags on Wi-Fi:

- Drop `--quality` to ~60.
- Add `--max-width 1920` (or `1280`) to downscale.
- Lower `--fps` to 20.

## Endpoints

- `GET /` — the web receiver page.
- `GET /stream` — the MJPEG stream.
- `GET /snapshot.jpg` — single latest frame (handy for debugging).
- `GET /healthz` — JSON status / frame readiness.

## Troubleshooting

- **Black frames** → Screen Recording permission not granted; re-check Settings
  and restart the terminal.
- **Tablet can't connect** → Mac and tablet must be on the same Wi-Fi/LAN; a
  macOS firewall may block the port (System Settings → Network → Firewall).
- **Stream freezes then resumes** → expected on brief Wi-Fi drops; the client
  auto-reconnects.

## Notes

- The capture thread keeps only the newest frame, so slow clients never
  accumulate latency — they just skip ahead.
- Plain HTTP only (LAN use). Don't expose this to the public internet.
