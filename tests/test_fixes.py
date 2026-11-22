"""Tests for the silent-dead-end fixes: persistent FSM state, login-email block, QR login."""

from __future__ import annotations

import asyncio

import pytest
from telethon.tl.types import auth as auth_types

from qna_bot.storage.fsm import SQLiteStorage
from qna_bot.userbot.login import (
    DeliveryChannel,
    LoginStatus,
    PhoneLoginSession,
    QrLoginSession,
    _looks_like_email,
    describe_delivery,
)

# ---------------------------------------------------------------------------
# Persistent FSM storage: a restart must not lose the wizard step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fsm_state_survives_a_process_restart(tmp_path) -> None:
    """MemoryStorage lost the wizard step on every Render restart -> silent drops."""
    from aiogram.fsm.storage.base import StorageKey

    key = StorageKey(bot_id=1, chat_id=42, user_id=42)
    db = tmp_path / "bot.db"

    store1 = SQLiteStorage(str(db))
    await store1.set_state(key, "WizardStates:waiting_phone")
    await store1.set_data(key, {"api_id": 1234, "phone": "+251912345678"})
    await store1.close()

    # Simulate a fresh process (Render restart / duplicate instance).
    store2 = SQLiteStorage(str(db))
    assert await store2.get_state(key) == "WizardStates:waiting_phone"
    data = await store2.get_data(key)
    assert data["api_id"] == 1234
    assert data["phone"] == "+251912345678"
    await store2.close()


@pytest.mark.asyncio
async def test_fsm_update_data_merges_and_state_clear(tmp_path) -> None:
    from aiogram.fsm.storage.base import StorageKey

    key = StorageKey(bot_id=1, chat_id=7, user_id=7)
    store = SQLiteStorage(str(tmp_path / "b.db"))
    await store.set_data(key, {"a": 1})
    await store.update_data(key, {"b": 2})
    assert await store.get_data(key) == {"a": 1, "b": 2}

    await store.set_state(key, None)
    assert await store.get_state(key) is None
    await store.close()


@pytest.mark.asyncio
async def test_fsm_state_is_isolated_per_user(tmp_path) -> None:
    from aiogram.fsm.storage.base import StorageKey

    store = SQLiteStorage(str(tmp_path / "b.db"))
    a = StorageKey(bot_id=1, chat_id=1, user_id=1)
    b = StorageKey(bot_id=1, chat_id=2, user_id=2)
    await store.set_state(a, "s1")
    assert await store.get_state(b) is None
    await store.close()


# ---------------------------------------------------------------------------
# The login-email block: Telegram sends NOTHING until it is satisfied
# ---------------------------------------------------------------------------


def _sent_email_required() -> object:
    return auth_types.SentCode(
        type=auth_types.SentCodeTypeSetUpEmailRequired(), phone_code_hash="hash-x"
    )


@pytest.mark.asyncio
async def test_email_required_is_reported_as_blocked_not_code_sent(monkeypatch) -> None:
    """This is the dead end: Telegram refuses to send any code at all."""
    session = PhoneLoginSession()
    client = _client(_sent_email_required())
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    try:
        res = await session.request_code(1234, "abc", "+251912345678")
        # Must NOT pretend a code was sent.
        assert res.status is LoginStatus.ERROR
        assert "login email" in res.message.lower()
        assert session.pending is not None
        assert session.pending.email_setup_required is True
    finally:
        await session.close()


def test_describe_delivery_marks_email_required_as_action() -> None:
    d = describe_delivery(_sent_email_required())
    assert d.channel is DeliveryChannel.SETUP_REQUIRED
    assert d.action_required is True


@pytest.mark.asyncio
async def test_login_email_setup_then_real_code_is_delivered(monkeypatch) -> None:
    """Full unblock: send email -> verify emailed code -> normal code arrives."""
    session = PhoneLoginSession()
    client = _client(_sent_email_required())

    from telethon.tl.types import account as account_types

    sent_email = account_types.SentEmailCode(email_pattern="y***@gmail.com", length=5)
    # 1) SendVerifyEmailCode -> SentEmailCode
    # 2) VerifyEmail -> ok
    # 3) ResendCode -> real login code
    async def _call(request, *a, **k):
        return _responses.pop(0) if _responses else True

    _responses = [
        sent_email,  # SendVerifyEmailCodeRequest
        True,  # VerifyEmailRequest
        auth_types.SentCode(
            type=auth_types.SentCodeTypeSms(length=5), phone_code_hash="hash-real"
        ),  # ResendCodeRequest
    ]
    client.side_effect = _call
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    try:
        await session.request_code(1234, "abc", "+251912345678")

        res = await session.send_login_email_code("you@gmail.com")
        assert res.status is LoginStatus.CODE_SENT
        assert "y***@gmail.com" in res.message
        assert session.pending.email_setup_length == 5

        res = await session.submit_login_email_code("12345")
        assert res.status is LoginStatus.CODE_SENT
        assert res.delivery.channel is DeliveryChannel.SMS
        # The block is lifted and the new hash is in use.
        assert session.pending.email_setup_required is False
        assert session.pending.phone_code_hash == "hash-real"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_bad_email_is_rejected_before_calling_telegram(monkeypatch) -> None:
    session = PhoneLoginSession()
    client = _client(_sent_email_required())
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)
    try:
        await session.request_code(1234, "abc", "+251912345678")
        res = await session.send_login_email_code("not-an-email")
        assert res.status is LoginStatus.ERROR
        assert "email address" in res.message.lower()
    finally:
        await session.close()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("you@gmail.com", True),
        ("a.b+c@sub.domain.co", True),
        ("nope", False),
        ("a@b", False),
        ("", False),
        ("a b@c.com", False),
    ],
)
def test_email_detection(value: str, expected: bool) -> None:
    assert _looks_like_email(value) is expected


# ---------------------------------------------------------------------------
# QR login: no code delivery involved
# ---------------------------------------------------------------------------


class _FakeQr:
    def __init__(self, *, raise_on_wait: Exception | None = None) -> None:
        self.url = "tg://login?token=abc123"
        self.expires = None
        self._raise = raise_on_wait
        self.waited = False

    async def wait(self, timeout: float | None = None) -> None:
        self.waited = True
        if self._raise is not None:
            raise self._raise


@pytest.mark.asyncio
async def test_qr_login_start_returns_scannable_url(monkeypatch) -> None:
    session = QrLoginSession()
    client = _client(auth_types.SentCode(type=auth_types.SentCodeTypeApp(5), phone_code_hash="h"))
    fake_qr = _FakeQr()
    client.qr_login = _async_return(fake_qr)
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    try:
        res, url = await session.start(1234, "abc")
        assert res.status is LoginStatus.CODE_SENT
        assert url == "tg://login?token=abc123"
        assert session.active
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_qr_login_2fa_is_surfaced(monkeypatch) -> None:
    from telethon.errors import SessionPasswordNeededError

    session = QrLoginSession()
    client = _client(auth_types.SentCode(type=auth_types.SentCodeTypeApp(5), phone_code_hash="h"))
    client.qr_login = _async_return(_FakeQr(raise_on_wait=SessionPasswordNeededError(request=None)))
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    try:
        await session.start(1234, "abc")
        await asyncio.sleep(0.05)  # let the background waiter run
        status, message = session.poll().status, session.poll().message
        assert status is LoginStatus.TWO_FA_REQUIRED
        assert "2FA" in message or "cloud password" in message
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_qr_login_expiry_is_explained(monkeypatch) -> None:
    session = QrLoginSession()
    client = _client(auth_types.SentCode(type=auth_types.SentCodeTypeApp(5), phone_code_hash="h"))
    client.qr_login = _async_return(_FakeQr(raise_on_wait=TimeoutError()))
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    try:
        await session.start(1234, "abc")
        await asyncio.sleep(0.05)
        state = session.poll()
        assert state.status is LoginStatus.ERROR
        assert "expired" in state.message.lower()
    finally:
        await session.close()


# -- helpers ---------------------------------------------------------------
def _client(sent_code: object):
    from unittest.mock import AsyncMock, MagicMock

    client = MagicMock()
    client.is_connected = MagicMock(return_value=True)
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.send_code_request = AsyncMock(return_value=sent_code)
    client.sign_in = AsyncMock()
    client.session = MagicMock()
    client.session.save = MagicMock(return_value="SESSION")
    return client


def _async_return(value):
    from unittest.mock import AsyncMock

    return AsyncMock(return_value=value)
