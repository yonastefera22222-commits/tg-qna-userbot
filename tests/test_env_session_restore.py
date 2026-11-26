"""The only copy of the login that outlives a Render Free restart.

Render Free wipes the filesystem on every restart and sleeps after 15 minutes idle
(https://render.com/docs/free), so ``data/bot.db`` - where a login is stored - is
gone after every deploy. The documented escape is to paste the session string into
the host's Environment tab as ``TG_STRING_SESSION``.

That variable was parsed into ``EnvConfig`` and then never read by the startup
path: ``start_from_storage`` looked only at the database. So pasting it did
nothing, and every deploy silently threw the login away. These tests pin the
environment as a real fallback.
"""

from __future__ import annotations

import base64
import ipaddress
import struct
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.sessions import StringSession

from qna_bot.userbot.manager import UserbotManager

#: A well-formed StringSession: CURRENT_VERSION, dc id, packed IPv4, port, auth key.
_FORMAT = ">B4sH256s"


def _valid_session() -> str:
    raw = struct.pack(
        _FORMAT, 2, ipaddress.ip_address("149.154.167.51").packed, 443, b"\x22" * 256
    )
    value = "1" + base64.urlsafe_b64encode(raw).decode()
    StringSession(value)  # raises now if this is not acceptable to Telethon
    return value


def _manager(db_settings: dict, *, env_session: str | None):
    db = MagicMock()
    db.get_setting = AsyncMock(side_effect=lambda key, *a: db_settings.get(key))
    db.list_groups = AsyncMock(return_value=[])
    db.set_setting = AsyncMock()

    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=True)
    client.session.save = MagicMock(return_value="saved")

    manager = UserbotManager(
        db=db,
        ai_engine=MagicMock(),
        env_api_id=12345,
        env_api_hash="a" * 32,
        env_session=env_session,
    )
    manager._attach_client = AsyncMock()
    manager.reload_cache = AsyncMock()
    manager.drop_stale_pending_login = AsyncMock(return_value=False)
    return manager, db, client


@pytest.mark.asyncio
async def test_a_wiped_database_still_recovers_from_the_environment(monkeypatch) -> None:
    """This is the Render Free case: nothing on disk, session in env vars."""
    session = _valid_session()
    manager, db, client = _manager({}, env_session=session)
    monkeypatch.setattr(
        "qna_bot.userbot.manager.make_login_client", MagicMock(return_value=client)
    )

    assert await manager.start_from_storage() is True
    client.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_recovered_session_is_written_back_to_the_database(monkeypatch) -> None:
    """So the rest of the app, and any later restart with a disk, sees it too."""
    session = _valid_session()
    manager, db, client = _manager({}, env_session=session)
    monkeypatch.setattr(
        "qna_bot.userbot.manager.make_login_client", MagicMock(return_value=client)
    )

    await manager.start_from_storage()

    written = {call.args[0] for call in db.set_setting.await_args_list}
    assert "tg_session" in written


@pytest.mark.asyncio
async def test_the_database_wins_when_it_has_a_session(monkeypatch) -> None:
    """The environment is a fallback, not an override."""
    manager, db, client = _manager(
        {
            "tg_api_id": 999,
            "tg_api_hash": "b" * 32,
            "tg_session": _valid_session(),
        },
        env_session=_valid_session(),
    )
    factory = MagicMock(return_value=client)
    monkeypatch.setattr("qna_bot.userbot.manager.make_login_client", factory)

    await manager.start_from_storage()

    assert factory.call_args.args[0] == 999


@pytest.mark.asyncio
async def test_without_any_session_it_still_waits_for_the_wizard(monkeypatch) -> None:
    manager, _db, client = _manager({}, env_session=None)
    factory = MagicMock(return_value=client)
    monkeypatch.setattr("qna_bot.userbot.manager.make_login_client", factory)

    assert await manager.start_from_storage() is False
    factory.assert_not_called()
