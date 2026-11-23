"""The owner could not use the bot at all after a failed OpenRouter key test.

Reported: after trying to set up an OpenRouter key, *every* command and request
came back "❌ OpenRouter test failed (12ms): HTTP 401 ... Send another key or
/cancel", three times in a row, and nothing escaped it.

The bot wasn't broken - it was **trapped in a state**. Three faults stacked:

1. ``/cancel`` (and ``/start``, ``/setup``, ``/dashboard``) could not be reached
   from ``waiting_ai_key_update``. The command handlers live in ``wizard_router``,
   which is registered LAST, and the state-only handlers that come before it
   claim every message in that state - including a slash-command. So the escape
   hatch the message itself suggested did not work.
2. ``/cancel`` cleared the state *after* ``await ctx.userbot.cancel_pending_login()``.
   Any exception in between left the state set, so the trap survived the attempt
   to leave it.
3. An unusable key produced a provider-shaped 401 ("Missing Authentication
   header"), which is indistinguishable from "your key is bad" and sent the owner
   round the same loop again.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Chat, Message, User

from qna_bot.ai.providers import PROVIDERS
from qna_bot.dashboard.handlers.ai_settings import (
    api_key_problem,
    describe_key_failure,
    sanitise_api_key,
)

#: Every command that must work from *any* state.
ESCAPE_COMMANDS = ("cancel", "start", "setup", "dashboard", "help")


def _message(text: str, chat_id: int = 42) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=chat_id, is_bot=False, first_name="owner"),
        text=text,
    )


def _bot_stub() -> MagicMock:
    bot = MagicMock()
    bot.me = AsyncMock(return_value=User(id=1, is_bot=True, first_name="b", username="MyBot"))
    return bot


# ---------------------------------------------------------------------------
# 1. Commands must escape every state
# ---------------------------------------------------------------------------


def test_every_state_only_message_handler_refuses_commands() -> None:
    """A state handler that claims a slash-command traps the owner.

    This is the structural check behind the incident: every ``@router.message``
    gated only by a state must also refuse commands, so a slash-command falls
    through to the handler that owns it. Checked against the real decorator text
    (including multi-line ones) rather than a hand-maintained list, so a handler
    added later cannot quietly reintroduce the trap.
    """
    import pathlib
    import re

    handlers_dir = (
        pathlib.Path(__file__).resolve().parents[1] / "src/qna_bot/dashboard/handlers"
    )
    offenders = []
    for path in sorted(handlers_dir.glob("*.py")):
        source = path.read_text()
        # ``[^)]*`` spans newlines so a wrapped decorator is not skipped.
        for match in re.finditer(r"@router\.message\(([^)]*)\)", source, re.S):
            decorator = " ".join(match.group(1).split())
            if "States." in decorator and "NotACommand()" not in decorator:
                offenders.append(f"{path.name}: {decorator}")
    assert not offenders, (
        "these state handlers would swallow /cancel and trap the owner: "
        + "; ".join(offenders)
    )


@pytest.mark.parametrize(
    "text",
    [
        "/cancel",
        "/start",
        "/setup",
        "/dashboard",
        "/diag",  # not in any escape list - the point of the general guard
        "/testanswer",
        "/logindiagnose",
        "/a-command-invented-next-year",
        "/",
        "  /cancel",  # leading whitespace must not smuggle it past
    ],
)
@pytest.mark.asyncio
async def test_commands_escape_a_state_handler(text: str) -> None:
    """Any command at all, not just an enumerated few.

    Enumerating the escape commands was the first attempt and it still ate
    ``/diag``. A general "starts with a slash" guard cannot have that gap.
    """
    from qna_bot.dashboard.filters import NotACommand

    assert not await NotACommand()(_message(text)), text


@pytest.mark.asyncio
async def test_a_real_key_still_reaches_the_key_handler() -> None:
    """The guard must not block the thing the state is waiting for."""
    from qna_bot.dashboard.filters import NotACommand

    for key in ("sk-or-v1-0123456789abcdef", "gsk_abc", "AIzaSyABC"):
        assert await NotACommand()(_message(key)), key


# ---------------------------------------------------------------------------
# 2. The escape hatch must actually clear the state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_clears_the_state_even_if_the_userbot_raises() -> None:
    """Clearing the state last meant a failure kept the owner trapped."""
    from qna_bot.dashboard.handlers.wizard import cmd_cancel

    ctx = MagicMock()
    ctx.userbot.cancel_pending_login = AsyncMock(side_effect=RuntimeError("no client"))
    ctx.is_setup_complete = AsyncMock(return_value=True)
    ctx.db.get_setting = AsyncMock(return_value=True)
    ctx.render_dashboard_text = AsyncMock(return_value="Dashboard")
    state = MagicMock()
    state.clear = AsyncMock()

    message = _message("/cancel")
    # aiogram models are frozen pydantic instances, so assignment is blocked.
    object.__setattr__(message, "answer", AsyncMock())

    await cmd_cancel(message, ctx, state)
    state.clear.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3. A key that cannot work must be caught before it is sent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sk-or-v1-abc", "sk-or-v1-abc"),
        ("  sk-or-v1-abc  ", "sk-or-v1-abc"),
        ('"sk-or-v1-abc"', "sk-or-v1-abc"),
        ("'sk-or-v1-abc'", "sk-or-v1-abc"),
        ("Bearer sk-or-v1-abc", "sk-or-v1-abc"),
        ("bearer sk-or-v1-abc", "sk-or-v1-abc"),
        ("sk-or-v1-abc\n", "sk-or-v1-abc"),
        # An invisible bidi mark copied along with the key.
        ("sk-or-v1-abc\u200e", "sk-or-v1-abc"),
        ("OpenRouter: sk-or-v1-abc", "sk-or-v1-abc"),
    ],
)
def test_pasted_keys_are_sanitised(raw: str, expected: str) -> None:
    """Every one of these pastes produced an unusable header."""
    assert sanitise_api_key(raw) == expected


def test_sanitising_never_invents_a_key() -> None:
    assert sanitise_api_key("") == ""
    assert sanitise_api_key("   \n\t ") == ""


@pytest.mark.parametrize("provider", ["groq", "gemini", "openrouter"])
def test_empty_and_short_keys_are_rejected_locally(provider: str) -> None:
    spec = PROVIDERS[provider]
    assert api_key_problem(spec, "") is not None
    assert api_key_problem(spec, "abc") is not None


def test_a_key_with_spaces_is_rejected_not_sent() -> None:
    """A label pasted alongside the key is the classic cause of this 401."""
    problem = api_key_problem(PROVIDERS["openrouter"], "my key sk-or-v1-0123456789")
    assert problem is not None
    assert "spaces" in problem.lower()


def test_a_wrongly_prefixed_key_is_rejected_with_the_right_prefix() -> None:
    problem = api_key_problem(PROVIDERS["openrouter"], "gsk_0123456789abcdefghij")
    assert problem is not None
    assert "sk-or-" in problem


def test_a_plausible_key_is_not_blocked() -> None:
    for provider, key in (
        ("groq", "gsk_" + "a1b2c3d4" * 6),
        ("openrouter", "sk-or-v1-" + "b" * 40),
        ("gemini", "AIza" + "c" * 35),
    ):
        assert api_key_problem(PROVIDERS[provider], key) is None, provider


# ---------------------------------------------------------------------------
# 4. The 401 must be explained, not echoed
# ---------------------------------------------------------------------------


def test_missing_auth_header_is_explained_as_an_empty_paste() -> None:
    """"Missing Authentication header" is not a rejected key - nothing was sent."""
    text = describe_key_failure(
        PROVIDERS["openrouter"],
        "sk-or-v1-abcdefghijklmnop",
        'HTTP 401: {"error": {"message": "Missing Authentication header", "code": 401}}',
    )
    assert "without a usable key" in text
    assert "nothing was actually checked" in text


def test_a_rejected_key_is_explained_as_rejected() -> None:
    text = describe_key_failure(
        PROVIDERS["openrouter"], "sk-or-v1-x", 'HTTP 401: {"error":"invalid_api_key"}'
    )
    assert "rejected this key" in text


def test_rate_limiting_is_not_reported_as_a_bad_key() -> None:
    """A 429 means the key is fine; saying otherwise makes the owner replace it."""
    text = describe_key_failure(PROVIDERS["groq"], "gsk_x", "HTTP 429: rate limit exceeded")
    assert "rate-limit" in text.lower() or "rate limit" in text.lower()
    assert "key itself is fine" in text


def test_timeouts_get_a_neutral_message() -> None:
    text = describe_key_failure(PROVIDERS["groq"], "gsk_x", "timed out")
    assert "Send another key" in text


# ---------------------------------------------------------------------------
# 5. Structure: the routers resolve commands before state handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_command_handlers_resolve_before_state_handlers() -> None:
    """The actual dispatch order that made /cancel unreachable.

    aiogram tries handlers in registration order and the first match wins, so a
    state-only handler registered earlier consumes the update. This walks the
    real dispatcher rather than trusting the source layout.
    """
    os.environ["BOT_TOKEN"] = "123456:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    os.environ["DB_PATH"] = tempfile.mkdtemp() + "/b.db"

    from qna_bot.dashboard.bot import create_dashboard_bot

    ctx = MagicMock()
    ctx.env.bot_token = os.environ["BOT_TOKEN"]
    ctx.env.db_path = os.environ["DB_PATH"]
    bot, dp = create_dashboard_bot(ctx)
    try:
        order: list[tuple[str, str, list]] = []

        def walk(router, path: str) -> None:
            for handler in getattr(getattr(router, "message", None), "handlers", []) or []:
                order.append((path, getattr(handler.callback, "__name__", "?"), handler.filters))
            for sub in getattr(router, "sub_routers", []):
                walk(sub, sub.name or "router")

        walk(dp, "Dispatcher")
        names = [name for _p, name, _f in order]
        assert "cmd_cancel" in names

        cancel_at = names.index("cmd_cancel")
        before = order[:cancel_at]
        unconditioned_states = [
            name
            for _path, name, filters in before
            if any("State" in str(f.callback) for f in filters)
            and not any("Command" in str(f.callback) for f in filters)
        ]
        assert not unconditioned_states, (
            "these handlers would swallow /cancel before it is reached: "
            + ", ".join(unconditioned_states)
        )
    finally:
        await bot.session.close()
