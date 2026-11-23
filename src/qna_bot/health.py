"""Lightweight async HTTP health server so Render Free Web Services stay alive.

It also serves the browser login page (``/login/<id>``), which is how an owner
signs in with a single phone: opening ``tg://login?token=...`` from a web page is
"outside the app", so Telegram handles it and shows its own Approve prompt. A
button inside a Telegram chat cannot do that, because Telegram clients
deliberately ignore that link when it was clicked inside the app.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from aiohttp import web

from .browser_login import BrowserLoginManager, render_login_page

logger = logging.getLogger(__name__)


class HealthServer:
    """Minimal aiohttp HTTP server binding 0.0.0.0:$PORT for Render health checks & keep-alive."""

    def __init__(
        self,
        port: int,
        status_provider: Callable[[], dict],
        host: str = "0.0.0.0",
        login_manager: BrowserLoginManager | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._status_provider = status_provider
        self._login_manager = login_manager
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def bound_port(self) -> int:
        if self._site is not None and self._site._server is not None:
            sockets = self._site._server.sockets
            if sockets:
                return int(sockets[0].getsockname()[1])
        return self._port

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._handle_health)
        app.router.add_get("/health", self._handle_health)
        if self._login_manager is not None:
            app.router.add_get("/login/{attempt_id}", self._handle_login_page)
            app.router.add_get("/login/{attempt_id}/state", self._handle_login_state)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self._host, port=self._port)
        await self._site.start()
        logger.info("Health HTTP server listening on http://%s:%d/health", self._host, self.bound_port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    async def _handle_health(self, _request: web.Request) -> web.Response:
        payload = {"status": "ok", **self._status_provider()}
        return web.json_response(payload, status=200)

    # -- browser login ---------------------------------------------------
    def _no_store(self, response: web.Response) -> web.Response:
        """Login pages and tokens must never be cached or indexed."""
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def _handle_login_page(self, request: web.Request) -> web.Response:
        """Serve the one-tap login page for a live attempt."""
        manager = self._login_manager
        if manager is None:  # pragma: no cover - guarded by router registration
            raise web.HTTPNotFound()

        attempt = manager.find(request.match_info.get("attempt_id", ""))
        if attempt is None:
            # Deliberately identical for "wrong id" and "no attempt": the id is a
            # capability, so distinguishing them leaks whether one exists.
            return self._no_store(
                web.Response(
                    status=404,
                    content_type="text/html",
                    text=(
                        "<!doctype html><meta name=viewport content='width=device-width'>"
                        "<body style=\"font:16px system-ui;padding:32px;text-align:center\">"
                        "<h2>This login link has expired</h2>"
                        "<p>Go back to the Telegram bot and ask for a fresh login link.</p>"
                    ),
                )
            )

        manager.touch(attempt)
        return self._no_store(
            web.Response(status=200, content_type="text/html", text=render_login_page(attempt))
        )

    async def _handle_login_state(self, request: web.Request) -> web.Response:
        """Poll endpoint for the page: status + the *current* token URL."""
        manager = self._login_manager
        if manager is None:  # pragma: no cover - guarded by router registration
            raise web.HTTPNotFound()

        attempt = manager.find(request.match_info.get("attempt_id", ""))
        if attempt is None:
            return self._no_store(web.json_response({"failed": True, "message": "expired"}))

        manager.touch(attempt)
        return self._no_store(
            web.json_response(
                {
                    "approved": attempt.approved,
                    "needs_2fa": attempt.needs_2fa,
                    "failed": attempt.failed,
                    "url": attempt.url,
                    "seconds_left": attempt.seconds_left,
                    "renewals": attempt.renewals,
                }
            )
        )
