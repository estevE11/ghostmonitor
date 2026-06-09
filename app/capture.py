"""macOS screen capture engine (ScreenCaptureKit backend).

Captures a chosen display with Apple's ScreenCaptureKit (``SCStream``), which
delivers hardware-accelerated frames at up to the display's refresh rate and
only when content actually changes. Each frame's ``CVPixelBuffer`` is decoded
to a numpy array and JPEG-encoded with OpenCV.

This replaces the previous ``mss`` backend, whose CoreGraphics grab capped at
~17 fps regardless of the requested rate. ScreenCaptureKit reaches 60 fps and
composites the real hardware cursor for us (``showsCursor``), so no manual
cursor drawing is needed.

Public surface is unchanged: ``list_displays()``, ``Display``, and
``ScreenCapturer`` with ``start``/``stop``/``get_frame``/``wait_for_frame``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import List, Optional

import cv2
import numpy as np
import objc
import libdispatch
import Quartz
from Foundation import NSObject
from ScreenCaptureKit import (
    SCShareableContent,
    SCStreamConfiguration,
    SCContentFilter,
    SCStream,
    SCStreamOutputTypeScreen,
)
from CoreMedia import CMTimeMake, CMSampleBufferGetImageBuffer


# -- display discovery -----------------------------------------------------


@dataclass
class Display:
    """A capturable display as reported by ScreenCaptureKit."""

    index: int
    display_id: int
    width: int
    height: int
    left: int
    top: int
    name: str = ""

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        label = f" {self.name}" if self.name else ""
        return (
            f"[{self.index}]{label} {self.width}x{self.height} "
            f"at ({self.left}, {self.top})"
        )


def _get_shareable_content(timeout: float = 5.0):
    """Synchronously fetch SCShareableContent (the API is async/block-based).

    Raises RuntimeError if Screen Recording permission is missing or the call
    times out.
    """
    result: dict = {}
    done = threading.Event()

    def handler(content, error):
        result["content"] = content
        result["error"] = error
        done.set()

    SCShareableContent.getShareableContentWithCompletionHandler_(handler)
    if not done.wait(timeout):
        raise RuntimeError("Timed out querying displays (ScreenCaptureKit).")
    if result.get("error") is not None:
        raise RuntimeError(
            f"ScreenCaptureKit error: {result['error']}. "
            "Grant Screen Recording permission in System Settings > "
            "Privacy & Security > Screen Recording, then restart the terminal."
        )
    return result["content"]


def _display_names() -> dict:
    """Map CGDirectDisplayID -> friendly name via NSScreen."""
    names: dict = {}
    try:
        from AppKit import NSScreen

        for screen in NSScreen.screens():
            did = int(screen.deviceDescription()["NSScreenNumber"])
            names[did] = str(screen.localizedName())
    except Exception:
        pass
    return names


def list_displays() -> List[Display]:
    """Return the capturable displays, indexed 0..N-1.

    Index 0 is the first display (usually built-in); virtual displays created
    by BetterDisplay/dummy plugs appear as their own index. Use the index with
    ``start --display N``.
    """
    content = _get_shareable_content()
    names = _display_names()
    displays: List[Display] = []
    for i, d in enumerate(content.displays()):
        frame = d.frame()
        did = int(d.displayID())
        displays.append(
            Display(
                index=i,
                display_id=did,
                width=int(d.width()),
                height=int(d.height()),
                left=int(frame.origin.x),
                top=int(frame.origin.y),
                name=names.get(did, ""),
            )
        )
    return displays


# -- stream output delegate ------------------------------------------------


class _FrameOutput(NSObject):
    """SCStreamOutput delegate: forwards each pixel buffer to a Python cb."""

    def initWithCallback_(self, callback):
        self = objc.super(_FrameOutput, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, sctype):
        if sctype != SCStreamOutputTypeScreen:
            return
        pixel_buffer = CMSampleBufferGetImageBuffer(sbuf)
        if pixel_buffer is None:
            return
        try:
            self._callback(pixel_buffer)
        except Exception as exc:  # never let an exception cross into ObjC
            print(f"[capture] frame handler error: {exc!r}")


# -- capturer --------------------------------------------------------------


class ScreenCapturer:
    """Threaded frame provider backed by ScreenCaptureKit.

    Frames arrive on a background dispatch queue; each is JPEG-encoded and kept
    as the single latest frame behind a condition variable, so slow consumers
    skip ahead instead of accumulating latency.
    """

    def __init__(
        self,
        display: int = 0,
        fps: int = 30,
        quality: int = 80,
        max_width: Optional[int] = None,
    ) -> None:
        self.display = display
        self.fps = max(1, fps)
        self.quality = max(1, min(100, quality))
        self.max_width = max_width

        self._frame: Optional[bytes] = None
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._seq = 0
        self._running = False

        # Strong refs so ObjC objects aren't garbage-collected mid-stream.
        self._stream = None
        self._output = None
        self._queue = None
        self._encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        display = self._resolve_display()

        config = SCStreamConfiguration.alloc().init()
        config.setWidth_(display.width)
        config.setHeight_(display.height)
        # Caps the *max* rate; SCK still only emits frames on content change.
        config.setMinimumFrameInterval_(CMTimeMake(1, self.fps))
        config.setShowsCursor_(True)  # composite the real hardware cursor
        config.setPixelFormat_(Quartz.kCVPixelFormatType_32BGRA)
        # Keep this small: a deep queue adds capture-side latency (each buffered
        # frame is ~1 frame of delay). We only ever serve the newest frame.
        config.setQueueDepth_(3)

        sc_display = self._sc_display(display.index)
        content_filter = SCContentFilter.alloc().initWithDisplay_excludingWindows_(
            sc_display, []
        )

        self._output = _FrameOutput.alloc().initWithCallback_(self._on_pixel_buffer)
        self._queue = libdispatch.dispatch_queue_create(b"ghostmonitor.frames", None)
        self._stream = SCStream.alloc().initWithFilter_configuration_delegate_(
            content_filter, config, None
        )
        ok, err = self._stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self._output, SCStreamOutputTypeScreen, self._queue, None
        )
        if not ok:
            raise RuntimeError(f"Failed to attach stream output: {err}")

        started = threading.Event()
        start_err: dict = {}

        def handler(error):
            start_err["error"] = error
            started.set()

        self._stream.startCaptureWithCompletionHandler_(handler)
        if not started.wait(5.0):
            raise RuntimeError("Timed out starting capture.")
        if start_err.get("error") is not None:
            raise RuntimeError(f"startCapture failed: {start_err['error']}")
        self._running = True

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stopCaptureWithCompletionHandler_(lambda e: None)
            except Exception:
                pass
        self._stream = None
        self._output = None
        self._queue = None

    # -- public API --------------------------------------------------------

    def get_frame(self) -> Optional[bytes]:
        with self._lock:
            return self._frame

    def wait_for_frame(self, last_seq: int, timeout: float = 1.0):
        with self._cond:
            self._cond.wait_for(lambda: self._seq != last_seq, timeout=timeout)
            return self._seq, self._frame

    # -- internals ---------------------------------------------------------

    def _resolve_display(self) -> Display:
        displays = list_displays()
        if self.display < 0 or self.display >= len(displays):
            raise ValueError(
                f"Display index {self.display} out of range. "
                f"Valid indexes: 0..{len(displays) - 1}. "
                f"Run 'list-displays' to see options."
            )
        return displays[self.display]

    def _validate_display(self) -> None:
        # Also triggers the Screen Recording permission prompt early.
        self._resolve_display()

    def _sc_display(self, index: int):
        content = _get_shareable_content()
        return content.displays()[index]

    def _on_pixel_buffer(self, pixel_buffer) -> None:
        Quartz.CVPixelBufferLockBaseAddress(pixel_buffer, 1)  # read-only
        try:
            width = Quartz.CVPixelBufferGetWidth(pixel_buffer)
            height = Quartz.CVPixelBufferGetHeight(pixel_buffer)
            bytes_per_row = Quartz.CVPixelBufferGetBytesPerRow(pixel_buffer)
            base = Quartz.CVPixelBufferGetBaseAddress(pixel_buffer)
            if base is None:
                return
            buf = base.as_buffer(bytes_per_row * height)
            # BGRA with possible row padding -> crop to width, drop alpha.
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(
                (height, bytes_per_row // 4, 4)
            )[:, :width, :3]

            if self.max_width and width > self.max_width:
                scale = self.max_width / width
                frame = cv2.resize(
                    frame,
                    (self.max_width, max(1, int(height * scale))),
                    interpolation=cv2.INTER_AREA,
                )

            ok, encoded = cv2.imencode(".jpg", frame, self._encode_params)
            if not ok:
                return
            data = encoded.tobytes()
        finally:
            Quartz.CVPixelBufferUnlockBaseAddress(pixel_buffer, 1)

        with self._cond:
            self._frame = data
            self._seq += 1
            self._cond.notify_all()
