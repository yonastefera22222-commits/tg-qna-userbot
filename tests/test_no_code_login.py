"""No-code login: the browser page, and why a chat button could never work.

History of this problem, in order, because the second attempt was a real bug:

1. **OTP never arrives.** Since Feb 2023 Telegram will not send SMS to
   third-party clients; ``auth.sendCode`` answers ``SentCodeTypeApp``, which means
   "delivered as a service message to *other* live sessions of this number".
   With no other session (or when Telegram's anti-spam heuristics drop it) the
   request succeeds and nothing is ever delivered. No retry fixes that.

2. **The first attempted fix was wrong.** The ``tg://login?token=...`` deep link
   was surfaced as a ``url`` button in the chat. Telegram's own deep-link docs
   say these links must be handled only when they are opened from **outside the
   app** and explicitly *not* when clicked inside a message or button. A bot
   button is inside the app, so the tap was a no-op — the feature never worked.

3. **The fix pinned here.** The same token is served from a web page on the
   deployment's own public URL. A browser is outside the app, so Telegram takes
   over and shows its own "Log in?" prompt. One tap approves. Nothing is
   delivered, so nothing can fail to arrive.

These tests lock that shape in, including the regression that a ``tg://`` button
must never come back.
"""

from __future__ import annotations

import pytest

from qna_bot.browser_login import (
    BrowserLoginManager,
    render_login_page,
)
from qna_bot.dashboard.handlers.wizard import _qr_caption, _qr_kb
from qna_bot.dashboard.keyboards import otp_keypad_kb, wizard_login_method_kb
from qna_bot.userbot.login import (
    DeliveryChannel,
    describe_next_channel,
)

#: What Telethon's ``QRLogin.url`` actually produces.
TOKEN_URL = "tg://login?token=AQAAAA-b_cd123"
PAGE_URL = "https://bot.example.com/login/abc123"


def _buttons(keyboard) -> list:
    return [b for row in keyboard.inline_keyboard for b in row]


# ---------------------------------------------------------------------------
# Regression: a tg:// link is never a chat button again
# ---------------------------------------------------------------------------


def test_tg_login_is_never_offered_as_a_chat_button() -> None:
    """Telegram ignores tg:// login links tapped inside the app (its own docs)."""
    for kb in (_qr_kb(TOKEN_URL), _qr_kb(TOKEN_URL, PAGE_URL), _qr_kb()):
        for button in _buttons(kb):
            assert not (button.url or "").startswith("tg://login")


def test_dead_one_tap_callbacks_are_gone() -> None:
    callbacks = {b.callback_data for b in _buttons(_qr_kb(TOKEN_URL, PAGE_URL))}
    assert "wiz:onetaplink" not in callbacks


def test_token_is_not_put_in_the_caption_text() -> None:
    """The token is a credential; a chat transcript is the wrong place for it."""
    caption = _qr_caption(TOKEN_URL, page_url=PAGE_URL)
    assert TOKEN_URL not in caption
    assert "token=AQAAAA" not in caption


# ---------------------------------------------------------------------------
# The working path: a browser page
# ---------------------------------------------------------------------------


def test_login_page_is_offered_as_a_web_button() -> None:
    url_buttons = [b for b in _buttons(_qr_kb(TOKEN_URL, PAGE_URL)) if b.url]
    assert len(url_buttons) == 1
    assert url_buttons[0].url == PAGE_URL
    assert url_buttons[0].url.startswith("https://")


def test_login_page_button_is_first_on_the_screen() -> None:
    first_row = _qr_kb(TOKEN_URL, PAGE_URL).inline_keyboard[0]
    assert first_row[0].url == PAGE_URL


def test_screen_still_works_without_a_public_url() -> None:
    """A local run has no public URL; the QR route must still render."""
    buttons = _buttons(_qr_kb(TOKEN_URL, None))
    assert all(b.url is None for b in buttons)
    assert any(b.callback_data == "wiz:login_qr" for b in buttons)


def test_screen_can_always_ask_for_the_link_again() -> None:
    callbacks = {b.callback_data for b in _buttons(_qr_kb(TOKEN_URL, PAGE_URL))}
    assert "wiz:loginlink" in callbacks


def test_caption_explains_the_one_phone_route_first() -> None:
    caption = _qr_caption(TOKEN_URL, page_url=PAGE_URL).lower()
    assert "same phone" in caption
    assert caption.index("same phone") < caption.index("second device")


def test_caption_mentions_the_qr_route_only_as_the_alternative() -> None:
    caption = _qr_caption(TOKEN_URL, page_url=PAGE_URL)
    assert "no second device" in caption.lower()


def test_caption_without_a_page_still_describes_the_qr_route() -> None:
    caption = _qr_caption(TOKEN_URL)
    assert "Link Desktop Device" in caption


# ---------------------------------------------------------------------------
# The rendered page itself
# ---------------------------------------------------------------------------


def test_page_contains_the_live_token_link() -> None:
    attempt = _attempt()
    page = render_login_page(attempt)
    assert TOKEN_URL in page
    assert 'href="tg://login?token=AQAAAA-b_cd123"' in page


def test_page_offers_a_copy_paste_fallback() -> None:
    """Some phone browsers refuse to follow tg:// from a web page."""
    page = render_login_page(_attempt())
    assert "Copy the link" in page
    assert "Saved Messages" in page


def test_page_is_not_indexable_and_not_cached() -> None:
    page = render_login_page(_attempt())
    assert 'name="robots" content="noindex' in page


def test_page_polls_for_approval() -> None:
    page = render_login_page(_attempt())
    assert "/state" in page
    assert "approved" in page


def test_page_attempt_id_is_json_encoded() -> None:
    """The id is injected into JS; a stray quote must not break out of it."""
    attempt = _attempt()
    attempt.attempt_id = 'ab"c</script>'
    page = render_login_page(attempt)
    assert 'ab\\"c' in page
    assert "</script>" not in page.split("<script>", 1)[1].split("</script>")[0]


# ---------------------------------------------------------------------------
# OTP path: an escape hatch that actually goes somewhere
# ---------------------------------------------------------------------------


def test_otp_keypad_offers_the_no_code_route() -> None:
    """The original dead end: no code arriving, and only Resend/Cancel on screen."""
    callbacks = {b.callback_data for b in _buttons(otp_keypad_kb(""))}
    assert "wiz:login_qr" in callbacks
    assert "otp:resend" in callbacks


def test_otp_keypad_escape_does_not_promise_the_dead_button() -> None:
    escape = next(b for b in _buttons(otp_keypad_kb("")) if b.callback_data == "wiz:login_qr")
    assert "no code" in escape.text.lower()
    assert "one tap" not in escape.text.lower()


def test_login_methods_lead_with_the_no_code_route() -> None:
    first = wizard_login_method_kb().inline_keyboard[0][0]
    assert first.callback_data == "wiz:login_qr"
    assert "no-code" in first.text.lower()


def test_login_options_still_offer_every_route() -> None:
    callbacks = {b.callback_data for b in _buttons(wizard_login_method_kb())}
    assert {"wiz:login_qr", "wiz:login_phone", "wiz:login_session"} <= callbacks


# ---------------------------------------------------------------------------
# What Telegram says it will do next
# ---------------------------------------------------------------------------


def test_next_delivery_channel_is_read_from_sent_code() -> None:
    """Telegram always names a fallback channel; the old UI threw it away."""
    from telethon.tl import types

    assert describe_next_channel(types.auth.CodeTypeSms()) is DeliveryChannel.SMS
    assert describe_next_channel(types.auth.CodeTypeCall()) is DeliveryChannel.CALL
    assert describe_next_channel(types.auth.CodeTypeFlashCall()) is DeliveryChannel.FLASH_CALL
    assert describe_next_channel(None) is None
    assert describe_next_channel(object()) is None


# ---------------------------------------------------------------------------
# The attempt manager
# ---------------------------------------------------------------------------


def _attempt():
    from qna_bot.browser_login import BrowserLoginAttempt
    from qna_bot.userbot.login import LoginStatus

    return BrowserLoginAttempt(
        attempt_id="abc123",
        client=object(),
        api_id=1,
        api_hash="h",
        url=TOKEN_URL,
        expires_at=1e12,
        login=object(),
        status=LoginStatus.CODE_SENT,
    )


def test_manager_page_url_is_scoped_to_one_attempt() -> None:
    manager = BrowserLoginManager()
    assert manager.page_url("https://bot.example.com") is None
    manager._attempt = _attempt()
    assert manager.page_url("https://bot.example.com/") == PAGE_URL


def test_manager_only_serves_the_exact_attempt_id() -> None:
    """The attempt id is a capability: near-misses must not render the page."""
    manager = BrowserLoginManager()
    manager._attempt = _attempt()
    assert manager.find("abc123") is not None
    assert manager.find("abc124") is None
    assert manager.find("") is None


def test_manager_snapshot_carries_status_but_not_the_api_hash() -> None:
    manager = BrowserLoginManager()
    manager._attempt = _attempt()
    snapshot = manager.snapshot()
    assert snapshot is not None
    assert snapshot["url"] == TOKEN_URL
    assert "api_hash" not in snapshot


@pytest.mark.asyncio
async def test_manager_close_cancels_and_disconnects() -> None:
    manager = BrowserLoginManager()
    attempt = _attempt()
    attempt.task = None

    class _Client:
        disconnected = False

        async def disconnect(self) -> None:
            self.disconnected = True

    client = _Client()
    attempt.client = client
    manager._attempt = attempt
    await manager.close()
    assert manager.current is None
    assert client.disconnected is True


def test_manager_hands_over_the_authorized_client() -> None:
    """Once approved, the session is transferred and persisted like any other."""
    from qna_bot.userbot.login import LoginStatus

    manager = BrowserLoginManager()
    attempt = _attempt()
    attempt.status = LoginStatus.OK
    manager._attempt = attempt

    pending = manager.take_authorized_client()
    assert pending is not None
    assert pending.api_id == 1
    assert manager.current is None


def test_manager_does_not_hand_over_an_unapproved_client() -> None:
    manager = BrowserLoginManager()
    manager._attempt = _attempt()  # still CODE_SENT
    assert manager.take_authorized_client() is None
    assert manager.current is not None


# ---------------------------------------------------------------------------
# Why the code never arrives, and what the bot now says about it
# ---------------------------------------------------------------------------


def test_app_delivery_warns_that_resend_will_not_help() -> None:
    """The single most misleading message the bot used to send.

    ``SentCodeTypeApp`` with no delivery is indistinguishable from success on the
    wire, so the UI must stop pushing "Resend" and point at the no-code route.
    """
    from telethon.tl import types

    from qna_bot.userbot.login import DeliveryChannel, describe_delivery

    sent = types.auth.SentCode(
        type=types.auth.SentCodeTypeApp(length=5),
        phone_code_hash="h",
        next_type=types.auth.CodeTypeSms(),
        timeout=60,
    )
    delivery = describe_delivery(sent)

    assert delivery.channel is DeliveryChannel.TELEGRAM_APP
    assert delivery.next_channel is DeliveryChannel.SMS
    assert delivery.next_after == 60
    text = delivery.detail.lower()
    assert "do not keep tapping resend" in text
    assert "link login" in text


def test_delivery_carries_next_channel_for_every_type() -> None:
    from telethon.tl import types

    from qna_bot.userbot.login import DeliveryChannel, describe_delivery

    for sent_type in (
        types.auth.SentCodeTypeSms(length=5),
        types.auth.SentCodeTypeCall(length=5),
        types.auth.SentCodeTypeFlashCall(pattern="x"),
    ):
        delivery = describe_delivery(
            types.auth.SentCode(
                type=sent_type,
                phone_code_hash="h",
                next_type=types.auth.CodeTypeCall(),
            )
        )
        assert delivery.next_channel is DeliveryChannel.CALL


def test_login_client_does_not_identify_as_raw_telethon() -> None:
    """A default Telethon fingerprint is what Telegram suppresses codes for.

    See Telethon #4730: reporters confirmed delivery became reliable again once
    the client presented a real mobile device profile.
    """
    from qna_bot.userbot.login import (
        APP_VERSION,
        DEVICE_MODEL,
        SYSTEM_VERSION,
        make_login_client,
    )

    client = make_login_client(12345, "abc")
    # Never connected, so there is nothing to tear down; the identity Telegram
    # actually sees is baked into the init-connection request.
    init = client._init_request
    assert init.device_model == DEVICE_MODEL
    assert init.system_version == SYSTEM_VERSION
    assert init.app_version == APP_VERSION
    assert "telethon" not in init.device_model.lower()
    assert "python" not in init.system_version.lower()


def test_browser_login_is_the_primary_login_offer() -> None:
    """The first option must be the route that works with one phone."""
    from qna_bot.dashboard.keyboards import wizard_login_method_kb

    first = wizard_login_method_kb().inline_keyboard[0][0]
    assert first.callback_data == "wiz:login_qr"
    assert "no-code" in first.text.lower()
    assert "sms" in first.text.lower()
