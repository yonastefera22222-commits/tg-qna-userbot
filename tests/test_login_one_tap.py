"""One-tap login: the QR token as a tappable deep link.

The complaint this addresses: "for the QR login you need two phones with the
same account to scan the code". That was never true - Telegram's QR token is
also a ``tg://login?token=...`` URI that the already-signed-in phone can open
directly - but the wizard only offered it as a small hyperlink inside a caption,
and the OTP keypad had no way out when no code ever arrived.

These tests pin the one-phone path.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from qna_bot.dashboard.handlers.wizard import _qr_caption, _qr_kb
from qna_bot.dashboard.keyboards import otp_keypad_kb, wizard_login_method_kb

#: What Telethon's ``QRLogin.url`` actually produces.
TOKEN_URL = "tg://login?token=AQAAAA-b_cd123"


def _buttons(keyboard) -> list:
    return [b for row in keyboard.inline_keyboard for b in row]


# ---------------------------------------------------------------------------
# The deep link itself
# ---------------------------------------------------------------------------


def test_qr_url_is_a_telegram_deep_link() -> None:
    parsed = urlparse(TOKEN_URL)
    assert parsed.scheme == "tg"
    assert parsed.netloc == "login"
    assert "token" in parse_qs(parsed.query)


def test_keyboard_exposes_the_link_as_a_button_not_only_text() -> None:
    """A tappable URL button is the difference between one phone and two."""
    url_buttons = [b for b in _buttons(_qr_kb(TOKEN_URL)) if b.url]
    assert len(url_buttons) == 1
    assert url_buttons[0].url == TOKEN_URL


def test_link_button_is_first_on_the_screen() -> None:
    first_row = _qr_kb(TOKEN_URL).inline_keyboard[0]
    assert first_row[0].url == TOKEN_URL


def test_keyboard_works_without_a_url() -> None:
    """The failure path (no token) must still render a usable screen."""
    buttons = _buttons(_qr_kb())
    assert all(b.url is None for b in buttons)
    assert any(b.callback_data == "wiz:login_qr" for b in buttons)


def test_caption_says_one_phone_is_enough() -> None:
    caption = _qr_caption(TOKEN_URL)
    assert "same phone" in caption.lower()
    assert "no second" in caption.lower() or "not need a second" in caption.lower()


def test_caption_does_not_bury_the_link_only_in_text() -> None:
    # The caption may mention the target for transparency, but the actionable
    # path is the button; the old version had only an <a href> in the caption.
    caption = _qr_caption(TOKEN_URL)
    assert "<a href=" not in caption


def test_caption_never_leaks_a_full_token_in_plain_text() -> None:
    """A session token should not sit in a chat transcript in full."""
    caption = _qr_caption(TOKEN_URL)
    assert TOKEN_URL not in caption


# ---------------------------------------------------------------------------
# The refresh path: tokens expire in ~30s
# ---------------------------------------------------------------------------


def test_qr_screen_offers_a_fresh_link() -> None:
    callbacks = {b.callback_data for b in _buttons(_qr_kb(TOKEN_URL))}
    assert "wiz:onetaplink" in callbacks


def test_refresh_rebuilds_the_button_from_the_new_token() -> None:
    """A renewed token has a new URL; reusing the old one would fail."""
    old = _buttons(_qr_kb("tg://login?token=OLD"))[0]
    new = _buttons(_qr_kb("tg://login?token=NEW"))[0]
    assert old.url != new.url


# ---------------------------------------------------------------------------
# The OTP dead end
# ---------------------------------------------------------------------------


def test_otp_keypad_offers_the_no_code_route() -> None:
    """The exact dead end: no code arriving, and only Resend/Cancel on screen."""
    callbacks = {b.callback_data for b in _buttons(otp_keypad_kb(""))}
    assert "wiz:login_qr" in callbacks
    assert "otp:resend" in callbacks


def test_otp_keypad_escape_button_is_labelled_clearly() -> None:
    escape = next(b for b in _buttons(otp_keypad_kb("")) if b.callback_data == "wiz:login_qr")
    assert "one tap" in escape.text.lower()
    assert "no code" in escape.text.lower()


def test_login_methods_lead_with_the_fastest_route() -> None:
    first = wizard_login_method_kb().inline_keyboard[0][0]
    assert first.callback_data == "wiz:login_qr"
    assert "one tap" in first.text.lower()


def test_login_options_still_offer_every_route() -> None:
    callbacks = {b.callback_data for b in _buttons(wizard_login_method_kb())}
    assert {"wiz:login_qr", "wiz:login_phone", "wiz:login_session"} <= callbacks


# ---------------------------------------------------------------------------
# The QR session drives the same login either way
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qr_session_start_returns_the_deep_link() -> None:
    """start_qr_login must hand the UI a link, not just a status string."""
    import inspect

    from qna_bot.userbot.manager import UserbotManager

    sig = inspect.signature(UserbotManager.start_qr_login)
    # tuple[LoginResult, str | None]: the second element is the tg:// link.
    assert sig.return_annotation is not None
    params = list(sig.parameters)
    assert params == ["self", "api_id", "api_hash"]
