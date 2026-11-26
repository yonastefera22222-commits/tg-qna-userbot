"""Shared application context and helper renderers for Dashboard Bot handlers."""

from __future__ import annotations

import html
import logging
import secrets
from dataclasses import dataclass, field

from aiogram.types import CallbackQuery, Message

from ..ai.engine import AIEngine
from ..ai.providers import PROVIDERS
from ..browser_login import BrowserLoginManager
from ..config import EnvConfig
from ..storage.database import Database
from ..userbot.manager import UserbotManager

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    env: EnvConfig
    db: Database
    ai: AIEngine
    userbot: UserbotManager
    #: Serves the browser login page (``/login/<id>``) and owns its attempt.
    #: Optional so tests and CLI paths can build a context without a web server.
    browser_login: BrowserLoginManager | None = None
    pairing_code: str = field(default_factory=lambda: secrets.token_hex(3).upper())

    async def get_owner_id(self) -> int | None:
        if self.env.owner_id is not None:
            return self.env.owner_id
        saved = await self.db.get_setting("owner_id")
        return int(saved) if saved is not None else None

    async def claim_owner(self, user_id: int) -> None:
        await self.db.set_setting("owner_id", int(user_id))

    async def is_setup_complete(self) -> bool:
        keys = await self.db.get_all_provider_keys()
        has_session = bool(await self.db.get_setting("tg_session"))
        marked = bool(await self.db.get_setting("setup_completed", False))
        return marked and has_session and bool(keys)

    async def render_dashboard_text(self) -> str:
        global_on = bool(await self.db.get_setting("global_enabled", True))
        notify_on = bool(await self.db.get_setting("notify_on_answer", False))
        groups = await self.db.list_groups()
        enabled_groups = [g for g in groups if g.enabled]
        stats = await self.db.get_stats()

        primary_p = str(await self.db.get_setting("primary_provider", "groq"))
        spec = PROVIDERS.get(primary_p, PROVIDERS["groq"])
        primary_m = str(await self.db.get_setting("primary_model", spec.default_model))

        fb_p = await self.db.get_setting("fallback_provider")
        fb_m = await self.db.get_setting("fallback_model")
        if fb_p and fb_m:
            fb_spec = PROVIDERS.get(str(fb_p))
            fb_label = f"{fb_spec.emoji if fb_spec else ''} {fb_p} · <code>{html.escape(str(fb_m))}</code>"
        else:
            fb_label = "None"

        ub_status = (
            f"🟢 Online — <b>{html.escape(self.userbot.account_display)}</b>"
            if self.userbot.is_connected
            else "🔴 Offline (Reconnect in Account menu)"
        )
        responder_status = "🟢 ACTIVE" if global_on else "⏸ PAUSED"
        notify_status = "🔔 ON" if notify_on else "🔕 OFF"

        return (
            "🎛 <b>Q&amp;A Userbot Dashboard</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Account:</b> {ub_status}\n"
            f"⚡ <b>Responder:</b> {responder_status}\n"
            f"👥 <b>Active Groups:</b> <b>{len(enabled_groups)}</b> / {len(groups)} synced\n"
            f"🤖 <b>Primary AI:</b> {spec.emoji} {spec.name} · <code>{html.escape(primary_m)}</code>\n"
            f"🛡 <b>Fallback AI:</b> {fb_label}\n"
            f"📣 <b>Answer Alerts:</b> {notify_status}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Today:</b> {stats['today']} answered  |  "
            f"<b>Total:</b> {stats['total']}\n"
            f"⏱ <b>Avg Speed:</b> {stats['avg_latency_ms']}ms  |  "
            f"🧮 <b>Instant Math:</b> {stats['local_math_hits']}"
        )


async def safe_delete_message(message: Message) -> None:
    """Best-effort delete a message containing sensitive secrets (API keys, 2FA, etc.)."""
    try:
        await message.delete()
    except Exception:
        pass


async def safe_edit_text(
    callback: CallbackQuery,
    text: str,
    reply_markup=None,
) -> None:
    """Edit callback message safely, ignoring 'message is not modified' errors.

    When editing is impossible (the message is too old to edit, was already
    deleted, or the caption/photo branch will not take text) the text is sent as a
    new message instead. That fallback is best effort: it must never raise, and it
    must never loop. It used to be an unguarded ``await`` inside the ``except``
    block, which turned any transient send failure into an unbounded
    answer/fail/answer cycle - the whole wizard handler hung and the screen simply
    never changed, which looks exactly like "nothing happened" to the owner.
    """
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(
            text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        return
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return
        logger.debug("Could not edit the message, posting a new one instead: %s", exc)
    try:
        await callback.message.answer(
            text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception as exc:  # pragma: no cover - network dependent
        # Nothing more to do: the handler has done its work, and retrying here can
        # only loop. Log it so the reason is visible in the host's logs.
        logger.warning("Could not deliver the update text to the owner: %s", exc)
