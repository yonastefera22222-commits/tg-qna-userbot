"""Custom aiogram filters for the dashboard bot."""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import Message


class NotACommand(BaseFilter):
    """Matches any private-chat message that is *not* a slash-command.

    Every state handler (waiting for an API key, a phone number, an OTP code, a
    group search term) must use this. Without it, a state-only handler claims a
    slash-command typed while that state is active, and the owner is trapped:
    the AI menu's key prompt used to answer ``/cancel`` with "not a valid key",
    which left no way out of the step *it itself* told them to cancel.

    A general "starts with a slash" test rather than a list of known commands,
    so a command added later cannot silently reintroduce the trap.
    """

    async def __call__(self, message: Message) -> bool:
        text = (message.text or "").lstrip()
        return not text.startswith("/")
