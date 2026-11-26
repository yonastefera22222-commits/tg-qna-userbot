"""The login screen must never die silently after asking Telegram for a code.

Two unbounded/fatal failure paths were found by driving the real handler and the
real dispatcher rather than calling helpers directly:

1. ``safe_edit_text`` retried ``message.answer`` inside its own ``except`` block.
   When that retry failed too, the exception escaped into the handler.
2. ``msg_phone`` edited its "working..." bubble with a bare ``await
   status_msg.edit_text(...)``. Any failure there - message too old to edit, the
   message deleted, a media caption that will not take text, a transient API error -
   killed the handler *after* the code had been requested. The screen stayed on
   "📡 Requesting login code from Telegram..." for ever, which from the owner's
   side is indistinguishable from the bot ignoring the number.
"""

from __future__ import annotations

import pytest

from qna_bot.dashboard.handlers import wizard
from qna_bot.userbot.login import LoginResult, LoginStatus


class _Status:
    """A status bubble whose edits fail, as Telegram's do for old messages."""

    def __init__(self) -> None:
        self.edits = 0
        self.answers = 0

    async def edit_text(self, *a, **k):
        self.edits += 1
        raise RuntimeError("message can't be edited")

    async def answer(self, *a, **k):
        self.answers += 1
        return self


class _Message:
    def __init__(self, status: _Status) -> None:
        self.text = "+251912345678"
        self._status = status

    async def answer(self, *a, **k):
        return self._status

    async def edit_text(self, *a, **k):
        raise RuntimeError("message can't be edited")

    async def delete(self):
        return None


def _ctx(userbot_calls):
    from unittest.mock import AsyncMock, MagicMock

    ctx = MagicMock()
    ctx.env.tg_api_id = 12345
    ctx.env.tg_api_hash = "a" * 32
    ctx.db.get_setting = AsyncMock(return_value=None)
    ctx.db.undecryptable = set()
    ctx.userbot.start_phone_login = AsyncMock(
        side_effect=lambda *a, **k: (
            userbot_calls.append(a),
            LoginResult(LoginStatus.CODE_SENT, "ok"),
        )[1]
    )
    ctx.userbot.email_setup_required = False
    ctx.userbot.pending_phone = "+251912345678"
    ctx.userbot.last_code_delivery = None
    return ctx


def _state():
    from unittest.mock import AsyncMock, MagicMock

    state = MagicMock()
    state.get_data = AsyncMock(return_value={"api_id": 12345, "api_hash": "a" * 32})
    state.update_data = AsyncMock()
    state.set_state = AsyncMock()
    return state


@pytest.mark.asyncio
async def test_the_code_is_requested_even_when_the_screen_cannot_be_updated() -> None:
    """The Telegram request happens first and must never be lost to a UI error."""
    calls: list[tuple] = []
    status = _Status()

    await wizard.msg_phone(_Message(status), _ctx(calls), _state())

    assert calls, "the code request must still reach Telegram"
    assert calls[0][0] == 12345


@pytest.mark.asyncio
async def test_a_failed_edit_falls_back_to_a_new_message_once() -> None:
    """One fallback, never a retry loop."""
    calls: list[tuple] = []
    status = _Status()

    await wizard.msg_phone(_Message(status), _ctx(calls), _state())

    assert status.answers == 1


@pytest.mark.asyncio
async def test_safe_edit_text_never_raises_or_loops() -> None:
    """Both operations failing must end quietly, not recursively."""
    from unittest.mock import MagicMock

    from qna_bot.dashboard.context import safe_edit_text

    calls = {"edit": 0, "answer": 0}
    msg = MagicMock()

    async def fail_edit(*a, **k):
        calls["edit"] += 1
        raise RuntimeError("can't be edited")

    async def fail_answer(*a, **k):
        calls["answer"] += 1
        raise RuntimeError("reply not found")

    msg.edit_text = fail_edit
    msg.answer = fail_answer
    callback = MagicMock()
    callback.message = msg

    await safe_edit_text(callback, "hello")

    assert calls["edit"] == 1
    assert calls["answer"] == 1
