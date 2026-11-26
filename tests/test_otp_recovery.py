

# ---------------------------------------------------------------------------
# "There is no QR code showing" - the button never sent an image
# ---------------------------------------------------------------------------


def test_the_qr_button_sends_an_image_not_a_link() -> None:
    """It used to show a text screen with a web link, so nothing was scannable.

    The image existed behind a second button ("Make a QR Code") that was easy to
    miss, and the browser page it preferred contains no QR image at all.
    """
    import inspect

    from qna_bot.dashboard.handlers import wizard

    src = inspect.getsource(wizard.cb_login_qr)
    assert "_show_qr_image(" in src
    assert "answer_photo" in inspect.getsource(wizard._show_qr_image)


def test_the_keypad_offers_qr_login() -> None:
    """The screen that says "no code arriving?" must offer the route with no code."""
    from qna_bot.dashboard.keyboards import otp_keypad_kb

    kb = otp_keypad_kb("")
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "wiz:login_qr" in callbacks


def test_the_otp_prompt_names_the_guaranteed_route() -> None:
    import inspect

    from qna_bot.dashboard.handlers import wizard

    src = inspect.getsource(wizard.msg_phone)
    assert "QR" in src


def test_a_failed_qr_start_explains_itself() -> None:
    """No token must not mean an empty screen."""
    import inspect

    from qna_bot.dashboard.handlers import wizard

    src = inspect.getsource(wizard._show_qr_image)
    assert "did not issue a QR token" in src


# ---------------------------------------------------------------------------
# "Still no QR code" - the handler started two logins at once
# ---------------------------------------------------------------------------


def test_the_qr_button_starts_exactly_one_login() -> None:
    """Starting the browser flow *and* a QR login made Telegram cancel the token.

    Two live connections for one login attempt: Telegram rejects the second
    ``auth.exportLoginToken``, ``start_qr_login`` returns ``url=None``, and the
    screen says "Telegram did not issue a QR token" instead of showing an image.
    """
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from qna_bot.dashboard.handlers import wizard
    from qna_bot.userbot.login import LoginResult, LoginStatus

    browser = SimpleNamespace(
        start=AsyncMock(side_effect=AssertionError("browser login must not be started")),
        page_url=MagicMock(return_value="https://example.test/login/abc"),
    )
    userbot = SimpleNamespace(
        start_qr_login=AsyncMock(
            return_value=(
                LoginResult(LoginStatus.CODE_SENT, "Scan this QR code."),
                "tg://login?token=abc",
            )
        ),
        qr_code=MagicMock(return_value=b"\x89PNG\r\n\x1a\npayload"),
    )
    ctx = SimpleNamespace(
        browser_login=browser,
        userbot=userbot,
        env=SimpleNamespace(public_url="https://example.test"),
        db=SimpleNamespace(get_setting=AsyncMock(return_value=None)),
    )
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"api_id": 12345, "api_hash": "a" * 32}),
        set_state=AsyncMock(),
        update_data=AsyncMock(),
    )
    message = SimpleNamespace(
        answer_photo=AsyncMock(), edit_text=AsyncMock(), answer=AsyncMock()
    )
    callback = SimpleNamespace(
        answer=AsyncMock(),
        message=message,
        bot=SimpleNamespace(edit_message_text=AsyncMock()),
    )

    asyncio.run(wizard.cb_login_qr(callback, ctx, state))

    browser.start.assert_not_awaited()
    userbot.start_qr_login.assert_awaited_once()
    message.answer_photo.assert_awaited_once()
    assert userbot.qr_code.called
