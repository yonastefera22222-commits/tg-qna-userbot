"""The request that asks Telegram for a login code must permit delivery.

Telethon's helper sends ``auth.sendCode`` with a bare ``CodeSettings()``, and every
field of that object is ``None``. In TL, an unset flag means **not allowed** - so
the bot was telling Telegram, in as many words, not to use the SMS, FlashCall or
Firebase channels. It then reported "code sent" and waited.

``allow_app_hash`` is the important one. Telethon only attaches the application
hash (derived from ``api_id``/``api_hash``) to ``auth.sendCode`` when that flag is
set, and Telegram's anti-spam uses that hash to recognise a known third-party
client. Leaving it unset made every login look like an unrecognised application.

These tests pin the flags on the wire, not just that a function exists.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from telethon import TelegramClient
from telethon.tl.functions.auth import SendCodeRequest
from telethon.tl.types import auth as auth_types

from qna_bot.userbot.login import _request_login_code, code_settings


def test_app_hash_is_permitted() -> None:
    """Without this Telegram gets no application hash at all."""
    settings = code_settings()
    assert settings is not None
    assert settings.allow_app_hash is True


def test_no_flag_is_left_as_none() -> None:
    """Unset means "not allowed"; the request should state a decision."""
    settings = code_settings()
    assert settings is not None
    for field in (
        "allow_flashcall",
        "current_number",
        "allow_app_hash",
        "allow_missed_call",
        "allow_firebase",
        "unknown_number",
    ):
        assert getattr(settings, field) is not None, f"{field} left unset"


def test_firebase_is_permitted() -> None:
    """Google-service numbers on Android only get codes through this channel."""
    settings = code_settings()
    assert settings is not None
    assert settings.allow_firebase is True


class _Client(TelegramClient):
    """A real TelegramClient subclass, so the direct-request path is exercised."""

    api_id = 12345
    api_hash = "a" * 32

    def __init__(self) -> None:
        self._phone_code_hash: dict[str, str] = {}
        self.request = None

    async def __call__(self, request):
        self.request = request
        return auth_types.SentCode(
            phone_code_hash="HASH-1",
            type=auth_types.SentCodeTypeSms(length=5),
            next_type=None,
            timeout=60,
        )


@pytest.mark.asyncio
async def test_the_wire_request_carries_the_flags() -> None:
    client = _Client()

    sent = await _request_login_code(client, "+251912345678")

    assert isinstance(client.request, SendCodeRequest)
    payload = client.request.to_dict()
    assert payload["settings"]["allow_app_hash"] is True
    assert payload["settings"]["allow_firebase"] is True
    assert payload["phone_number"] == "+251912345678"
    assert payload["api_id"] == 12345
    assert sent.phone_code_hash == "HASH-1"


@pytest.mark.asyncio
async def test_the_hash_is_cached_for_sign_in() -> None:
    """``sign_in`` reads Telethon's own cache; keep it in step."""
    client = _Client()

    await _request_login_code(client, "+251912345678")

    assert client._phone_code_hash["+251912345678"] == "HASH-1"


@pytest.mark.asyncio
async def test_a_silent_login_without_a_code_is_reported() -> None:
    """``SentCodeSuccess`` means no code was needed; never treat it as a code."""

    class _Weird(_Client):
        async def __call__(self, request):
            return auth_types.SentCodeSuccess(authorization=MagicMock())

    with pytest.raises(RuntimeError):
        await _request_login_code(_Weird(), "+251912345678")
