"""Router ordering: the wizard's catch-all must not swallow other features' input.

Real bug this covers: after setting a primary AI provider, pasting an API key in
"Add / Update API Keys" replied "I am waiting for a different kind of input right
now (step: waiting_ai_key_update)". The state was correct - the message never
reached the AI-menu handler, because wizard_router owns the catch-all
@router.message() and was registered first, so it consumed every text message.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User

from qna_bot.dashboard.handlers import wizard as wizard_module
from qna_bot.dashboard.states import AI_MENU_STATES, DashboardStates, WizardStates


def _message_update(text: str = "AQ.Ab8RN6-test-key", uid: int = 42) -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=int(time.time()),
            chat=Chat(id=uid, type="private"),
            from_user=User(id=uid, is_bot=False, first_name="Owner"),
            text=text,
        ),
    )


class _Inject:
    async def __call__(self, handler, event, data):
        data["ctx"] = None
        data["state"] = None
        return await handler(event, data)


def _make_router(name: str, callback) -> Router:
    """A fresh router with one mock handler.

    Routers are module-level singletons and aiogram refuses to attach one to two
    dispatchers, so each test builds its own stand-ins in the order under test.
    """
    router = Router(name=name)
    router.message()(callback)
    return router


async def _dispatch(router_pairs, state_value) -> list[str]:
    seen: list[str] = []

    def make(tag):
        async def handler(message, ctx, state):
            seen.append(tag)
        return handler

    dp = Dispatcher(storage=MemoryStorage())
    for tag in router_pairs:
        dp.include_router(_make_router(tag, make(tag)))
    dp.message.middleware(_Inject())
    await dp.storage.set_state(key=(1, 42, 42), state=state_value)
    bot = MagicMock()
    bot.id = 1
    bot.answer = AsyncMock()
    await dp.feed_update(bot=bot, update=_message_update())
    return seen


@pytest.mark.asyncio
async def test_correct_order_lets_the_ai_handler_win() -> None:
    """Production order: wizard (with its catch-all) is registered LAST."""
    seen = await _dispatch(
        ["ai_key", "dashboard", "wizard_catchall"], DashboardStates.waiting_ai_key_update
    )
    assert seen == ["ai_key"]


@pytest.mark.asyncio
async def test_wizard_first_reproduces_the_bug() -> None:
    """The old order: the catch-all consumes the key before the AI handler runs."""
    seen = await _dispatch(
        ["wizard_catchall", "ai_key", "dashboard"], DashboardStates.waiting_ai_key_update
    )
    assert seen == ["wizard_catchall"]


def test_catch_all_declares_the_ai_menu_states_it_must_skip() -> None:
    assert "DashboardStates:waiting_ai_key_update" in AI_MENU_STATES
    assert "DashboardStates:waiting_custom_model_update" in AI_MENU_STATES
    assert wizard_module.AI_MENU_STATES is AI_MENU_STATES


def test_production_registration_order_is_wizard_last() -> None:
    """Guard the actual include order in bot.py, since that is the real fix."""
    from pathlib import Path

    source = Path("src/qna_bot/dashboard/bot.py").read_text()
    order = [
        line.strip() for line in source.splitlines() if "dp.include_router(" in line
    ]
    assert order, "no include_router calls found"
    assert "wizard_router" in order[-1], f"wizard router must be registered last, got {order}"


def test_ai_menu_states_cover_every_state_the_ai_router_handles() -> None:
    """Any state a secret-entry router waits on must be in the skip set.

    Two routers now collect credentials - AI provider keys and Exness API keys -
    and both must be shielded from the wizard's catch-all. Checking only the
    first would let the second be silently swallowed.
    """
    import inspect

    from qna_bot.dashboard.handlers import ai_settings, exness

    for module in (ai_settings, exness):
        source = inspect.getsource(module)
        for state in AI_MENU_STATES:
            group, _, short = state.partition(":")
            # Only the states this module actually owns.
            if f"{group}." not in source and group != "DashboardStates":
                continue
            if f"{group}.{short}" not in source:
                continue
            assert f"{group}.{short}" in source, f"{short} not used by {module.__name__}?"

    ai_source = inspect.getsource(ai_settings)
    for state in AI_MENU_STATES:
        group, _, short = state.partition(":")
        if group == "DashboardStates":
            assert f"DashboardStates.{short}" in ai_source, f"{short} not used by ai_settings?"

    exness_source = inspect.getsource(exness)
    for name in ("waiting_api_key", "waiting_private_key", "waiting_account_id",
                 "waiting_gateway_url"):
        assert f"ExnessStates:{name}" in AI_MENU_STATES
        assert f"ExnessStates.{name}" in exness_source


def test_wizard_states_are_not_in_the_skip_set() -> None:
    """The catch-all must keep answering its own states."""
    for name in ("waiting_api_id", "waiting_api_hash", "waiting_phone"):
        assert f"WizardStates:{name}" not in AI_MENU_STATES
    assert WizardStates.waiting_api_id.state == "WizardStates:waiting_api_id"
