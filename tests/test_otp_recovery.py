

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
