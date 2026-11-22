"""Lightweight async HTTP health server so Render Free Web Services stay alive."""

from __future__ import annotations

import logging
from collections.abc import Callable

from aiohttp import web

logger = logging.getLogger(__name__)


class HealthServer:
    """Minimal aiohttp HTTP server binding 0.0.0.0:$PORT for Render health checks & keep-alive."""

    def __init__(
        self,
        port: int,
        status_provider: Callable[[], dict],
        host: str = "0.0.0.0",
    ) -> None:
        self._host = host
        self._port = port
        self._status_provider = status_provider
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
