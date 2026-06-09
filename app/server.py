"""FastAPI app: MJPEG stream + static web receiver.

The browser loads ``index.html`` which points an ``<img>`` at ``/stream``.
``/stream`` is a ``multipart/x-mixed-replace`` response — the classic MJPEG
trick that every browser can render with zero client-side JavaScript decoding.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, Response

from .capture import ScreenCapturer

TEMPLATES_DIR = Path(__file__).parent / "templates"
BOUNDARY = "frame"


def create_app(capturer: ScreenCapturer) -> FastAPI:
    app = FastAPI(title="mac-to-android-stream")

    @app.on_event("startup")
    async def _startup() -> None:
        capturer.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        capturer.stop()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(content=html)

    @app.get("/healthz")
    async def healthz() -> dict:
        ready = capturer.get_frame() is not None
        return {"status": "ok", "frame_ready": ready, "fps": capturer.fps}

    @app.get("/snapshot.jpg")
    async def snapshot() -> Response:
        frame = capturer.get_frame()
        if frame is None:
            return Response(status_code=503, content=b"no frame yet")
        return Response(content=frame, media_type="image/jpeg")

    @app.get("/stream")
    async def stream(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _mjpeg_generator(capturer, request),
            media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Connection": "close",
            },
        )

    return app


async def _mjpeg_generator(capturer: ScreenCapturer, request: Request):
    """Yield multipart JPEG chunks until the client disconnects."""
    last_seq = -1
    frame_interval = 1.0 / capturer.fps
    boundary = f"--{BOUNDARY}\r\n".encode()

    while True:
        # Bail out cleanly if the tablet closes the tab / loses Wi-Fi.
        if await request.is_disconnected():
            break

        # wait_for_frame is blocking; run it off the event loop.
        seq, frame = await asyncio.to_thread(
            capturer.wait_for_frame, last_seq, frame_interval
        )

        if frame is None:
            await asyncio.sleep(0.05)
            continue

        last_seq = seq
        header = (
            boundary
            + b"Content-Type: image/jpeg\r\n"
            + f"Content-Length: {len(frame)}\r\n\r\n".encode()
        )
        try:
            yield header + frame + b"\r\n"
        except (asyncio.CancelledError, GeneratorExit):
            break
