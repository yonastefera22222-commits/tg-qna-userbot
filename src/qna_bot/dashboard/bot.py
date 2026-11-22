"""aiogram v3 Dashboard Bot setup and strict Owner Authentication middleware."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, CallbackQuery, Message, TelegramObject

from ..storage.fsm import SQLiteStorage
from .context import AppContext
from .handlers import ai_router, dashboard_router, groups_router, wizard_router

logger = logging.getLogger(__name__)


class OwnerAuthMiddleware(BaseMiddleware):
    """Restricts Dashboard Bot access strictly to OWNER_ID (or one-time console pairing code)."""

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["ctx"] = self._ctx

        user = getattr(event, "from_user", None)
        if user is None:
            return None

        # Only allow private chats with the dashboard bot
        if isinstance(event, Message) and event.chat.type != "private":
            return None

        owner_id = await self._ctx.get_owner_id()
        if owner_id is not None:
            if user.id != owner_id:
                logger.warning("Blocked unauthorized dashboard access from user_id=%s", user.id)
                if isinstance(event, CallbackQuery):
                    await event.answer("⛔ Unauthorized.", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("⛔ This dashboard bot is locked to its owner.")
                return None
            return await handler(event, data)

        # No OWNER_ID configured yet -> require one-time pairing code printed in server logs
        if isinstance(event, Message):
            text = (event.text or "").strip()
            parts = text.split()
            supplied = parts[1].upper() if (len(parts) >= 2 and parts[0].startswith("/")) else text.upper()
            if supplied == self._ctx.pairing_code:
                await self._ctx.claim_owner(user.id)
                logger.info("Owner claimed by user_id=%s (@%s)", user.id, user.username)
                await event.answer(
                    f"✅ <b>Paired successfully!</b>\n"
                    f"Locked dashboard to your Telegram ID: <code>{user.id}</code>\n\n"
                    "Send /start now to begin the guided setup wizard!"
                )
                return None

            await event.answer(
                "🔐 <b>First-Time Owner Pairing Required</b>\n\n"
                "No <code>OWNER_ID</code> was set in <code>.env</code>.\n"
                "Check your server / Docker logs for the 6-character pairing code and send it here "
                "(e.g. <code>A1B2C3</code>) to claim ownership of this dashboard bot."
            )
            return None

        if isinstance(event, CallbackQuery):
            await event.answer("Pair ownership first by sending the code from server logs.", show_alert=True)
        return None


def create_dashboard_bot(ctx: AppContext) -> tuple[Bot, Dispatcher]:
    bot = Bot(
        token=ctx.env.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # Persistent FSM storage: a Render restart (free tier sleeps after 15 min)
    # or a duplicate instance must not silently swallow the wizard's current step.
    dp = Dispatcher(storage=SQLiteStorage(str(ctx.env.db_path)))

    auth_mw = OwnerAuthMiddleware(ctx)
    dp.message.middleware(auth_mw)
    dp.callback_query.middleware(auth_mw)

    # Register routers (wizard first so /start & /cancel always take priority)
    dp.include_router(wizard_router)
    dp.include_router(groups_router)
    dp.include_router(ai_router)
    dp.include_router(dashboard_router)

    async def notify_owner(html_text: str) -> None:
        owner_id = await ctx.get_owner_id()
        if owner_id is None:
            return
        try:
            await bot.send_message(owner_id, html_text, disable_web_page_preview=True)
        except Exception as exc:
            logger.debug("Failed sending owner notification: %s", exc)

    ctx.userbot.set_notify_callback(notify_owner)

    @dp.startup()
    async def on_startup() -> None:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Open setup wizard or dashboard"),
                BotCommand(command="dashboard", description="Open Q&A control dashboard"),
                BotCommand(command="setup", description="Re-run guided setup wizard"),
                BotCommand(command="cancel", description="Cancel current input step"),
                BotCommand(command="logindiagnose", description="Diagnose why no login code arrives"),
            ]
        )
        me = await bot.get_me()
        logger.info("Dashboard bot online as @%s (id=%s)", me.username, me.id)

    return bot, dp
