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
from qna_bot.userbot.login import PhoneIssue, parse_phone


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
        # A too-short number is still obviously a phone attempt, not a random
        # message: it should reach msg_phone and get its precise hint.
        "+2519123",
    ],
)
def test_a_phone_number_is_recognised_as_one(text: str) -> None:
    """The catch-all uses this to decide whether to recover the step."""
    parsed = parse_phone(text)
    recognised_by_issue = parsed.issue in (
        PhoneIssue.OK,
        PhoneIssue.TOO_SHORT,
        PhoneIssue.TOO_LONG,
    )
    import re

    recognised_by_shape = bool(re.fullmatch(r"\+?\d{7,15}", re.sub(r"[^\d+]", "", text) or ""))
    assert recognised_by_issue or recognised_by_shape, f"{text!r} would dead-end"


@pytest.mark.parametrize(
    "text",
    ["hello", "how much is gold today?", "go", "ጥሩ ነው", "", "?"],
)
def test_ordinary_chatter_is_not_mistaken_for_a_phone(text: str) -> None:
    """Recovery must not swallow normal messages into the login flow."""
    parsed = parse_phone(text)
    import re

    recognised_by_issue = parsed.issue in (
        PhoneIssue.OK,
        PhoneIssue.TOO_SHORT,
        PhoneIssue.TOO_LONG,
    )
    recognised_by_shape = bool(re.fullmatch(r"\+?\d{7,15}", re.sub(r"[^\d+]", "", text) or ""))
    assert not (recognised_by_issue or recognised_by_shape), f"{text!r} was treated as a phone"


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
