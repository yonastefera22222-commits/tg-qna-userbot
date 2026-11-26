"""Pin the login request path to the version that demonstrably delivered codes.

Login worked at 6b1f3c7 and stopped after two later commits changed *what the
client sends* while leaving the flow's logic alone:

1. an impersonated device identity ("Pixel 6" / "SDK 33" / "10.2.0 (4300)") baked
   into every ``InitConnectionRequest`` - a combination no real device produces;
2. a hand-built ``auth.SendCodeRequest`` with custom ``CodeSettings``, and an
   automatic ``auth.resendCode`` right after the first request.

Both are gone. These tests exist so neither can quietly come back: they assert on
what actually goes over the wire, not on implementation details.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from qna_bot.userbot.login import make_login_client

LOGIN_MODULE = pathlib.Path(__file__).resolve().parents[1] / "src/qna_bot/userbot/login.py"

#: Transport kwargs the working client has always used.
_WORKING_KEYWORDS = [
    "flood_sleep_threshold",
    "request_retries",
    "connection_retries",
    "retry_delay",
]


def test_the_client_uses_the_identity_telegram_has_accepted() -> None:
    """Pin the device identity to the one visible as a working session.

    The owner's Telegram "Other Devices" list shows "Pixel 6 / userbot 10.2.0"
    (Ashburn = Render) - a session that only exists because a login *succeeded*
    with it. Removing these parameters makes the client look like raw Telethon
    from a datacenter IP instead, which is a different client to Telegram.
    """
    client = make_login_client(12345, "a" * 32)
    init = client._init_request

    assert init.device_model == "Pixel 6"
    assert init.system_version == "SDK 33"
    assert init.app_version == "10.2.0 (4300)"


def test_the_client_keeps_the_documented_transport_options() -> None:
    """The transport kwargs must not be dropped while editing the identity."""
    source = LOGIN_MODULE.read_text()
    tree = ast.parse(source)
    factory = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "make_login_client"
    )
    call = next(
        node
        for node in ast.walk(factory)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "TelegramClient"
    )
    kwargs = [kw.arg for kw in call.keywords]
    for required in _WORKING_KEYWORDS:
        assert required in kwargs


def test_the_first_code_request_uses_telethons_own_helper() -> None:
    """No hand-built auth.sendCode: that was one of the two later changes."""
    source = LOGIN_MODULE.read_text()
    assert "SendCodeRequest(" not in source, "a hand-built auth.sendCode came back"


def test_no_automatic_resend_after_the_first_request() -> None:
    """A second code request right away is the other change that broke delivery."""
    source = LOGIN_MODULE.read_text()
    assert "escalate_after_app_channel" not in source


@pytest.mark.asyncio
async def test_asking_for_a_code_calls_send_code_request_once(monkeypatch) -> None:
    """Drive the real session: exactly one request, no follow-up resend."""
    from telethon.tl.types import auth as T

    from qna_bot.userbot.login import DeliveryChannel, PhoneLoginSession

    class _Client:
        api_id = 12345
        api_hash = "a" * 32

        def __init__(self) -> None:
            self._phone_code_hash: dict[str, str] = {}
            self.send_calls: list[str] = []
            self.other_calls: list[str] = []

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        def is_connected(self) -> bool:
            return True

        async def send_code_request(self, phone: str):
            self.send_calls.append(phone)
            return T.SentCode(
                phone_code_hash="H1",
                type=T.SentCodeTypeApp(length=5),
                next_type=T.CodeTypeSms(),
                timeout=60,
            )

        async def __call__(self, request):
            self.other_calls.append(type(request).__name__)
            raise AssertionError(f"unexpected extra request: {type(request).__name__}")

    client = _Client()
    monkeypatch.setattr("qna_bot.userbot.login.make_login_client", lambda *a, **k: client)

    session = PhoneLoginSession()
    result = await session.request_code(12345, "a" * 32, "+251912345678")

    assert result.delivery is not None
    assert result.delivery.channel is DeliveryChannel.TELEGRAM_APP
    assert client.send_calls == ["+251912345678"]
    assert client.other_calls == [], "nothing else may be sent during the request step"
