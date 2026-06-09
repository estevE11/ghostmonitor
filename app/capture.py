"""macOS screen capture engine.

Grabs frames from a chosen monitor with ``mss``, optionally resizes them, and
JPEG-encodes them with OpenCV. A background thread runs the capture loop at a
target FPS and keeps only the most recent encoded frame, so slow consumers
never build up latency.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import List, Optional

import cv2
import mss
import numpy as np

# Optional: read the live mouse position so we can paint a cursor onto frames.
# macOS draws the real cursor as a hardware overlay that screen grabs miss.
try:
    from Quartz import CGEventCreate, CGEventGetLocation

    def _global_cursor():
        loc = CGEventGetLocation(CGEventCreate(None))
        return float(loc.x), float(loc.y)

    _CURSOR_AVAILABLE = True
except Exception:  # pragma: no cover - Quartz not installed / not macOS
    def _global_cursor():
        return None

    _CURSOR_AVAILABLE = False


# Classic arrow-pointer silhouette (tip at 0,0), in a ~12x19 unit box.
_CURSOR_SHAPE = np.array(
    [[0, 0], [0, 16], [4, 12], [7, 19], [9, 18], [6, 11], [11, 11]],
    dtype=np.float32,
)


def _draw_cursor(frame: np.ndarray, x: float, y: float, scale: float = 2.0) -> None:
    """Paint a white arrow with a black outline at (x, y) on a BGR frame."""
    pts = (_CURSOR_SHAPE * scale + np.array([x, y])).astype(np.int32)
    cv2.fillPoly(frame, [pts], (255, 255, 255), lineType=cv2.LINE_AA)
    cv2.polylines(frame, [pts], True, (0, 0, 0), 1, lineType=cv2.LINE_AA)


@dataclass
class Display:
    """A capturable monitor as reported by mss."""

    index: int
    left: int
    top: int
    width: int
    height: int

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"[{self.index}] {self.width}x{self.height} "
            f"at ({self.left}, {self.top})"
        )


def list_displays() -> List[Display]:
    """Return the available displays.

    mss exposes ``monitors[0]`` as the union of all monitors ("all displays")
    and ``monitors[1:]`` as the individual physical/virtual monitors. We keep
    the same indexing so ``--display 0`` means "everything" and ``--display 1``
    is the primary monitor, matching mss's own convention.
    """

    with mss.mss() as sct:
        displays: List[Display] = []
        for i, mon in enumerate(sct.monitors):
            displays.append(
                Display(
                    index=i,
                    left=mon["left"],
                    top=mon["top"],
                    width=mon["width"],
                    height=mon["height"],
                )
            )
        return displays


class ScreenCapturer:
    """Threaded frame provider.

    Continuously grabs the selected monitor, encodes to JPEG, and stores the
    latest frame behind a lock. Consumers call :meth:`get_frame` to fetch the
    most recent JPEG bytes.
    """

    def __init__(
        self,
        display: int = 0,
        fps: int = 30,
        quality: int = 80,
        max_width: Optional[int] = None,
        draw_cursor: bool = True,
    ) -> None:
        self.display = display
        self.fps = max(1, fps)
        self.quality = max(1, min(100, quality))
        self.max_width = max_width
        self.draw_cursor = draw_cursor and _CURSOR_AVAILABLE

        self._frame: Optional[bytes] = None
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._seq = 0  # increments on every new frame

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        # Validate display up front so we fail fast (and trigger the macOS
        # Screen Recording permission prompt) before the server starts.
        self._validate_display()
        self._running = True
        self._thread = threading.Thread(
            target=self._capture_loop, name="screen-capture", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # -- public API --------------------------------------------------------

    def get_frame(self) -> Optional[bytes]:
        """Return the latest encoded JPEG frame, or ``None`` if not ready."""
        with self._lock:
            return self._frame

    def wait_for_frame(self, last_seq: int, timeout: float = 1.0):
        """Block until a frame newer than ``last_seq`` is available.

        Returns ``(seq, frame_bytes)``. On timeout returns the current frame
        even if unchanged so callers can re-check liveness.
        """
        with self._cond:
            self._cond.wait_for(lambda: self._seq != last_seq, timeout=timeout)
            return self._seq, self._frame

    # -- internals ---------------------------------------------------------

    def _validate_display(self) -> None:
        with mss.mss() as sct:
            count = len(sct.monitors)
        if self.display < 0 or self.display >= count:
            raise ValueError(
                f"Display index {self.display} out of range. "
                f"Valid indexes: 0..{count - 1}. "
                f"Run 'list-displays' to see options."
            )

    def _encode(self, frame: np.ndarray) -> Optional[bytes]:
        params = [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]
        ok, buf = cv2.imencode(".jpg", frame, params)
        if not ok:
            return None
        return buf.tobytes()

    def _capture_loop(self) -> None:
        frame_interval = 1.0 / self.fps
        # mss instances are not thread-safe across threads; create our own.
        with mss.mss() as sct:
            try:
                monitor = sct.monitors[self.display]
            except IndexError:
                self._running = False
                return

            while self._running:
                start = time.perf_counter()
                try:
                    raw = sct.grab(monitor)
                    # BGRA -> contiguous BGR. cvtColor yields a fresh, writable,
                    # C-contiguous array (required by cv2 draw calls below);
                    # a plain ``[:, :, :3]`` slice is a non-contiguous view and
                    # makes fillPoly raise.
                    frame = cv2.cvtColor(np.asarray(raw), cv2.COLOR_BGRA2BGR)

                    # Paint the cursor at full resolution, before any resize,
                    # so it scales naturally with the rest of the frame.
                    if self.draw_cursor:
                        pos = _global_cursor()
                        if pos is not None:
                            mw = monitor["width"] or frame.shape[1]
                            mh = monitor["height"] or frame.shape[0]
                            sx = frame.shape[1] / mw
                            sy = frame.shape[0] / mh
                            cx = (pos[0] - monitor["left"]) * sx
                            cy = (pos[1] - monitor["top"]) * sy
                            if 0 <= cx < frame.shape[1] and 0 <= cy < frame.shape[0]:
                                _draw_cursor(frame, cx, cy)

                    if self.max_width and frame.shape[1] > self.max_width:
                        scale = self.max_width / frame.shape[1]
                        new_size = (
                            self.max_width,
                            max(1, int(frame.shape[0] * scale)),
                        )
                        frame = cv2.resize(
                            frame, new_size, interpolation=cv2.INTER_AREA
                        )

                    encoded = self._encode(frame)
                    if encoded is not None:
                        with self._cond:
                            self._frame = encoded
                            self._seq += 1
                            self._cond.notify_all()
                except Exception as exc:  # capture must never die silently
                    # Most commonly: display unplugged or resolution change.
                    # Re-resolve the monitor list and keep going.
                    print(f"[capture] frame error: {exc!r}; retrying")
                    time.sleep(0.5)
                    try:
                        sct = mss.mss()
                        monitor = sct.monitors[self.display]
                    except Exception:
                        pass

                # Pace to target FPS.
                elapsed = time.perf_counter() - start
                sleep_for = frame_interval - elapsed
                if sleep_for > 0:
                    time.sleep(sleep_for)
