"""Generative HMI — AI-powered industrial control screens.

Renders live HMI dashboards from OPC UA tag data with auto-generated
widgets, SVG schematics, and real-time polling.

Start with:  generative-hmi [--port 8100] [--no-browser]
"""

from __future__ import annotations

import webbrowser
from pathlib import Path
from types import SimpleNamespace

import click
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).resolve().parent


class _StubClient:
    """No-op stand-in for the TIA daemon client (not needed in standalone mode)."""

    def send(self, *_args, **_kwargs):
        return SimpleNamespace(ok=False, data={})


# Shared singletons
client: object = _StubClient()
templates: Jinja2Templates = Jinja2Templates(directory=str(_HERE / "templates"))


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        # Shutdown: disconnect OPC UA clients
        try:
            from tools.gui.opcua_client import disconnect_all
            await disconnect_all()
        except Exception:
            pass

    app = FastAPI(title="Generative HMI", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    # Register route modules
    from tools.gui.hmi import router as hmi_router

    app.include_router(hmi_router)
    return app


@click.command("generative-hmi")
@click.option("--port", default=8100, help="Port to listen on", show_default=True)
@click.option("--host", default="127.0.0.1", help="Host to bind to (use 0.0.0.0 for LAN access)", show_default=True)
@click.option("--no-browser", is_flag=True, help="Don't open browser automatically")
def main(port: int, host: str, no_browser: bool) -> None:
    """Launch the Generative HMI web GUI."""
    import uvicorn

    app = create_app()
    if not no_browser:
        import threading

        def _open():
            import time
            time.sleep(1.2)
            webbrowser.open(f"http://127.0.0.1:{port}")

        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(app, host=host, port=port, log_level="info")
