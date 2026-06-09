"""CLI entry point.

Usage:
    python -m app.cli list-displays
    python -m app.cli start --display 1 --port 8080 --fps 30 --quality 80
"""

from __future__ import annotations

import socket
from typing import Optional

import typer
import uvicorn

from .capture import ScreenCapturer, list_displays as _list_displays
from .server import create_app

app = typer.Typer(
    add_completion=False,
    help="Stream a macOS display to an Android tablet over the LAN.",
)


def _local_ip() -> str:
    """Best-effort LAN IP. Opening a UDP socket to a public address lets the
    OS pick the right outbound interface without sending any packets.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


@app.command("list-displays")
def list_displays_cmd() -> None:
    """List available monitors and their indexes."""
    displays = _list_displays()
    typer.echo("Available displays:")
    for d in displays:
        typer.echo(f"  {d}")
    typer.echo(
        "\nUse the index with 'start --display N'. "
        "Virtual displays (BetterDisplay / dummy plugs) appear as their own "
        "index — pick that to stream only the virtual monitor."
    )


@app.command("start")
def start(
    display: int = typer.Option(0, "--display", help="Display index to capture."),
    port: int = typer.Option(8080, "--port", help="Port to serve on."),
    fps: int = typer.Option(30, "--fps", help="Target frames per second."),
    quality: int = typer.Option(
        80, "--quality", min=1, max=100, help="JPEG quality 1-100."
    ),
    max_width: Optional[int] = typer.Option(
        None,
        "--max-width",
        help="Downscale frames wider than this (px) to cut bandwidth.",
    ),
    host: str = typer.Option(
        "0.0.0.0", "--host", help="Bind address (default all interfaces)."
    ),
) -> None:
    """Start the streaming server."""
    capturer = ScreenCapturer(
        display=display, fps=fps, quality=quality, max_width=max_width
    )

    # Fail fast on a bad display index (and trigger the macOS permission
    # prompt) before Uvicorn binds the port.
    try:
        capturer._validate_display()  # noqa: SLF001 - intentional pre-check
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    ip = _local_ip()
    url = f"http://{ip}:{port}"

    typer.secho("\n  mac-to-android-stream", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"  display : {display}")
    typer.echo(f"  fps     : {fps}")
    typer.echo(f"  quality : {quality}")
    typer.secho("\n  Open this URL on your Android tablet:", bold=True)
    typer.secho(f"      {url}", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"\n  (local: http://127.0.0.1:{port})")
    typer.echo("  Press Ctrl+C to stop.\n")

    fastapi_app = create_app(capturer)
    try:
        uvicorn.run(fastapi_app, host=host, port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    finally:
        capturer.stop()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
