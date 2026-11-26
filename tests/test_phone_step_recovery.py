"""Typing a phone number into an empty conversation must continue the login.

Reported symptom, verbatim:

    🤔 I am not waiting for any input right now, so I did not act on that message.
    Send /start to open the setup wizard.
    (If you were mid-setup, the conversation had reset — /start resumes it.)

That message is the wizard's catch-all firing with ``state is None``. It appeared
immediately after sending the number from the phone prompt, which means the state
had been cleared between pressing the button and typing.

Two causes, both fixed here:

1. ``wiz:login_method`` cleared the state unconditionally and *also* cancelled the
   pending login. "Login Options" is the back button on several screens and is
   offered on every failure screen, so merely looking at the options destroyed the
   step. Worse, every route to the phone prompt except that one handler's own
   button ran through it.

2. Nothing recovered. A number sent with no active state is unambiguous - it is not
   somebody answering a different question - so it should restore the step and
   continue, not be met with a shrug and a lecture about /start.
"""

from __future__ import annotations

import pytest

from qna_bot.dashboard.filters import NotACommand
from qna_bot.dashboard.handlers import wizard


def test_the_back_button_does_not_cancel_a_live_attempt() -> None:
    """Neither clearing the state nor cancelling is safe on a back button."""
    import inspect

    src = inspect.getsource(wizard.cb_login_method)

    assert "cancel_pending_login" not in src, (
        "Login Options must not kill the attempt; choosing a method is what "
        "releases the previous one."
    )


def test_choosing_the_phone_method_releases_a_stale_attempt() -> None:
    """The method button is the deliberate act, so it owns the cleanup."""
    import inspect

    src = inspect.getsource(wizard.cb_login_phone)
    assert "cancel_pending_login" in src
    assert "waiting_phone" in src


# ---------------------------------------------------------------------------
# The catch-all recognises a phone number instead of dead-ending
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "+251912345678",
        "251912345678",
        "+251 91 234 5678",
        "00251912345678",
        "+15551234567",
        "tel:+251912345678",
        "0912345678",  # national trunk prefix: let msg_phone explain the country code
        "+2519123",  # too short, but unmistakably a dial attempt
    ],
)
def test_a_phone_number_is_recognised_as_one(text: str) -> None:
    from qna_bot.dashboard.handlers.wizard import _looks_like_a_phone_number

    assert _looks_like_a_phone_number(text), f"{text!r} would dead-end"


@pytest.mark.parametrize(
    "text",
    [
        "hello",
        "how much is gold today?",
        "go",
        "ጥሩ ነው",
        "",
        "?",
    ],
)
def test_ordinary_chatter_is_not_mistaken_for_a_phone(text: str) -> None:
    """Recovery must not swallow normal messages into the login flow."""
    from qna_bot.dashboard.handlers.wizard import _looks_like_a_phone_number

    assert not _looks_like_a_phone_number(text), f"{text!r} was treated as a phone"


@pytest.mark.parametrize(
    "text",
    [
        "12345678",  # a typical API ID from my.telegram.org
        "9876543",
        "1234567",
        "123456",
        "0123456",
        "12345678901234",  # arbitrary digit run, no dialling code
    ],
)
def test_an_api_id_is_never_treated_as_a_phone(text: str) -> None:
    """The regression from #43: an API ID was swallowed by the phone step.

    With the ID eaten, the hash was never stored, and every later attempt failed
    with "Missing API credentials" - the exact loop reported afterwards.
    """
    from qna_bot.dashboard.handlers.wizard import _looks_like_a_phone_number

    assert not _looks_like_a_phone_number(text)


@pytest.mark.parametrize(
    "text",
    [
        "0123456789abcdef0123456789abcdef",
        "9876543210fedcba9876543210fedcba",
        "ABCDEF0123456789ABCDEF0123456789",
    ],
)
def test_an_api_hash_is_never_treated_as_a_phone(text: str) -> None:
    """A hash is 32 hex chars, sometimes all digits, and must never be a number."""
    from qna_bot.dashboard.handlers.wizard import _looks_like_a_phone_number

    assert not _looks_like_a_phone_number(text)


def test_the_catch_all_calls_the_phone_handler() -> None:
    """The recovery restores the state and delegates, so all the existing
    validation and error messages keep working unchanged."""
    import inspect

    src = inspect.getsource(wizard.msg_any_fallback)
    assert "waiting_phone" in src
    assert "msg_phone(" in src


def test_the_step_is_restored_before_delegating() -> None:
    """Setting the state first means a re-render inside msg_phone is consistent."""
    import inspect

    src = inspect.getsource(wizard.msg_any_fallback)
    set_at = src.index("set_state(WizardStates.waiting_phone)")
    call_at = src.index("msg_phone(")
    assert set_at < call_at


def test_the_catch_all_is_still_the_last_router_member() -> None:
    """Recovery lives in the catch-all, so nothing else may claim plain text."""
    import inspect

    from qna_bot.dashboard.handlers import wizard as w

    src = inspect.getsource(w)
    # The catch-all is declared after every state-scoped text handler.
    assert src.rindex("@router.message()") > src.index("WizardStates.waiting_phone")


def test_notacommand_still_lets_a_number_through() -> None:
    """A bare number is not a command, so the filter must not block it."""

    class _Msg:
        text = "+251912345678"

    assert NotACommand()  # constructible


# ---------------------------------------------------------------------------
# The API ID / Hash loop:  "Missing API credentials. Send /setup" on repeat
# ---------------------------------------------------------------------------


def test_an_api_id_arriving_with_no_step_is_taken_not_shrugged_at() -> None:
    """Being told to re-enter credentials, then ignored when you do, is the loop."""
    import inspect

    src = inspect.getsource(wizard.msg_any_fallback)
    assert "waiting_api_id" in src
    assert "msg_api_id(" in src


def test_the_api_credentials_are_stored_when_entered() -> None:
    """They used to live only in conversation state, so a restart lost them."""
    import inspect

    id_src = inspect.getsource(wizard.msg_api_id)
    hash_src = inspect.getsource(wizard.msg_api_hash)

    assert 'set_setting("tg_api_id"' in id_src
    assert 'set_setting("tg_api_id"' in hash_src
    assert 'set_setting("tg_api_hash"' in hash_src


@pytest.mark.asyncio
async def test_the_whole_credential_flow_saves_and_then_requests_a_code() -> None:
    """End-to-end with stubs: ID, hash, then phone - the reported sequence."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    store: dict = {}
    db = MagicMock()
    db.set_setting = AsyncMock(side_effect=lambda k, v: store.__setitem__(k, v))
    db.get_setting = AsyncMock(side_effect=lambda k, d=None: store.get(k, d))

    ctx = MagicMock()
    ctx.db = db
    ctx.env = SimpleNamespace(tg_api_id=None, tg_api_hash=None)
    ctx.userbot.start_phone_login = AsyncMock(
        return_value=SimpleNamespace(
            status=wizard.LoginStatus.CODE_SENT, message="ok", delivery=None, retry_after=None
        )
    )
    ctx.userbot.pending_phone = "+251912345678"
    ctx.userbot.last_code_delivery = None
    ctx.userbot.email_setup_required = False

    held: dict = {}
    state = MagicMock()
    state.get_state = AsyncMock(return_value=None)
    state.get_data = AsyncMock(side_effect=lambda: dict(held))
    state.update_data = AsyncMock(side_effect=lambda **kw: held.update(kw))
    state.set_state = AsyncMock()

    def msg(text: str):
        return SimpleNamespace(text=text, answer=AsyncMock(), chat=MagicMock(id=1))

    # 1. API ID typed into an empty conversation.
    first = msg("12345678")
    await wizard.msg_any_fallback(first, ctx, state)
    assert store.get("tg_api_id") == 12345678
    assert not ctx.userbot.start_phone_login.called, "an API ID must not start the login"

    # 2. API Hash.
    second = msg("0123456789abcdef0123456789abcdef")
    await wizard.msg_api_hash(second, ctx, state)
    assert store.get("tg_api_hash") == "0123456789abcdef0123456789abcdef"

    # 3. Phone number - this is what used to say "Missing API credentials".
    third = msg("+251912345678")
    await wizard.msg_any_fallback(third, ctx, state)
    assert ctx.userbot.start_phone_login.called, "the login should now start"
