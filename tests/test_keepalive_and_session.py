"""Keeping a free host awake, and getting the login out of its doomed database.

Render Free sleeps a service after 15 minutes without *inbound* HTTP
(https://render.com/docs/free), and a sleeping service loses its filesystem.

Two consequences the bot did not handle:

1. **Telegram polling is outbound.** The bot could be busy answering hundreds of
   group questions and still be spun down, because none of that is inbound HTTP.
   It has to generate its own traffic - which is what the self-ping does.

2. **The owner could not get the session string from the bot.** The only durable
   channel on a free host is an environment variable, but the value in it had to
   come from somewhere - and generating it needed a terminal. Asking the *running*
   bot for it is what makes the fix usable from a phone.

These tests pin both.
"""

from __future__ import annotations

import asyncio

import pytest

from qna_bot.health import HealthServer


def _server() -> HealthServer:
    return HealthServer(port=0, status_provider=lambda: {"ok": True})


# ---------------------------------------------------------------------------
# Self-ping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_ping_is_skipped_when_disabled() -> None:
    """An operator who sets the interval to 0 must not be second-guessed."""
    server = _server()
    server.start_self_ping("https://bot.example.com", 0)
    assert server._ping_task is None


@pytest.mark.asyncio
async def test_self_ping_uses_the_public_health_url(monkeypatch) -> None:
    """It must hit the *public* URL: that goes through the host's router, so the
    request counts as inbound traffic. A request to localhost would not."""
    seen: list[str] = []

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Session:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url):
            seen.append(url)
            return _Resp()

    monkeypatch.setattr("qna_bot.health.aiohttp.ClientSession", _Session)
    monkeypatch.setattr("qna_bot.health.asyncio.sleep", _fast_sleep)

    server = _server()
    server.start_self_ping("https://bot.example.com/", 1)
    assert server._ping_task is not None
    for _ in range(50):
        if seen:
            break
        await asyncio.sleep(0)

    server._ping_task.cancel()
    try:
        await server._ping_task
    except asyncio.CancelledError:
        pass

    assert seen, "the ping task never made a request"
    assert seen[0] == "https://bot.example.com/health"


_REAL_SLEEP = asyncio.sleep


async def _fast_sleep(_seconds):
    """Skip the real delay but yield so the task still loops.

    ``_REAL_SLEEP`` is captured before the monkeypatch replaces
    ``qna_bot.health.asyncio.sleep`` - which is the same module object, so calling
    ``asyncio.sleep`` here would re-enter this function forever.
    """
    await _REAL_SLEEP(0)


@pytest.mark.asyncio
async def test_a_failing_ping_does_not_kill_the_loop(monkeypatch) -> None:
    """A transient network error must not silently end the keep-alive."""
    attempts: list[str] = []

    class _Session:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url):
            attempts.append(url)
            raise OSError("network down")

    monkeypatch.setattr("qna_bot.health.aiohttp.ClientSession", _Session)
    monkeypatch.setattr("qna_bot.health.asyncio.sleep", _fast_sleep)

    server = _server()
    server.start_self_ping("https://bot.example.com", 1)
    for _ in range(100):
        if len(attempts) >= 2:
            break
        await asyncio.sleep(0)

    server._ping_task.cancel()
    try:
        await server._ping_task
    except asyncio.CancelledError:
        pass

    assert len(attempts) >= 2, "the loop stopped after the first failure"


def test_the_default_interval_beats_the_spin_down_timer() -> None:
    """Render sleeps at 15 minutes; the ping must be comfortably under it."""
    from qna_bot.config import EnvConfig

    field = EnvConfig.__dataclass_fields__["keepalive_ping_seconds"]
    assert field is not None

    # And the value the code falls back to.
    import os

    os.environ.pop("KEEPALIVE_PING_SECONDS", None)
    from qna_bot.config import load_env_config

    os.environ.setdefault("BOT_TOKEN", "1:A")
    os.environ.setdefault("DATA_DIR", "/tmp/keepalive-test")
    assert load_env_config().keepalive_ping_seconds < 15 * 60


# ---------------------------------------------------------------------------
# Session string from the running bot
# ---------------------------------------------------------------------------


class _FakeDB:
    def __init__(self, saved: str | None) -> None:
        self._saved = saved

    async def get_setting(self, key: str, default=None):
        return self._saved if key == "tg_session" else default


def _manager(saved: str | None, *, client=None, connected: bool = False):
    from qna_bot.userbot.manager import UserbotManager

    manager = UserbotManager.__new__(UserbotManager)
    manager._db = _FakeDB(saved)  # type: ignore[assignment]
    manager._client = client
    manager._connected = connected
    return manager


class _LiveClient:
    def __init__(self, value: str) -> None:
        class _S:
            @staticmethod
            def save() -> str:
                return value

        self.session = _S()

    def is_connected(self) -> bool:  # what UserbotManager.is_connected reads
        return True


@pytest.mark.asyncio
async def test_the_live_session_is_exported_when_connected() -> None:
    manager = _manager("stored", client=_LiveClient("live-value"), connected=True)
    assert await manager.current_session_string() == "live-value"


@pytest.mark.asyncio
async def test_the_stored_session_is_the_fallback() -> None:
    """A cold process may not have reconnected yet; the DB copy still counts."""
    manager = _manager("stored-value", client=None, connected=False)
    assert await manager.current_session_string() == "stored-value"


@pytest.mark.asyncio
async def test_the_session_is_offered_as_a_file() -> None:
    """A file avoids the copy/paste spaces that break a 350-char string."""
    manager = _manager("stored-value")
    payload = await manager.session_file_bytes()

    assert payload is not None
    filename, blob = payload
    assert filename.startswith("telegram-session-")
    assert filename.endswith(".txt")
    assert blob.decode().strip() == "stored-value"


@pytest.mark.asyncio
async def test_no_session_means_no_file() -> None:
    manager = _manager(None)
    assert await manager.session_file_bytes() is None


def test_the_dashboard_offers_the_session_button() -> None:
    from qna_bot.dashboard.keyboards import account_menu_kb

    markup = account_menu_kb(is_connected=True)
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "acc:session_string" in callbacks
