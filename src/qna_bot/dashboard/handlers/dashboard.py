"""Main Dashboard actions: kill switch, Q&A simulator, stats/logs, alerts, account management."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ...ai.providers import PROVIDERS
from ...storage.database import GroupConfig
from ...userbot.filters import should_evaluate_message
from ..context import AppContext, safe_edit_text
from ..keyboards import account_menu_kb, cancel_to_dashboard_kb, main_dashboard_kb
from ..states import DashboardStates

router = Router(name="dashboard")


async def _show_home(target: CallbackQuery | Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    global_on = bool(await ctx.db.get_setting("global_enabled", True))
    notify_on = bool(await ctx.db.get_setting("notify_on_answer", False))
    text = await ctx.render_dashboard_text()
    kb = main_dashboard_kb(global_on, notify_on)
    if isinstance(target, CallbackQuery):
        await safe_edit_text(target, text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.message(Command("dashboard"))
async def cmd_dashboard(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await _show_home(message, ctx, state)


@router.callback_query(F.data == "dash:home")
async def cb_dash_home(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await callback.answer()
    await _show_home(callback, ctx, state)


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "dash:toggle_global")
async def cb_toggle_global(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    current = bool(await ctx.db.get_setting("global_enabled", True))
    new_val = not current
    await ctx.db.set_setting("global_enabled", new_val)
    await ctx.userbot.reload_cache()
    await callback.answer("▶️ Responder RESUMED" if new_val else "⏸ Responder PAUSED")
    await _show_home(callback, ctx, state)


@router.callback_query(F.data == "dash:toggle_notify")
async def cb_toggle_notify(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    current = bool(await ctx.db.get_setting("notify_on_answer", False))
    new_val = not current
    await ctx.db.set_setting("notify_on_answer", new_val)
    await callback.answer("🔔 Answer alerts ON" if new_val else "🔕 Answer alerts OFF")
    await _show_home(callback, ctx, state)


# ----------------------------------------------------------------------
# Live Q&A Simulator
# ----------------------------------------------------------------------


@router.callback_query(F.data == "dash:sim")
async def cb_sim_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(DashboardStates.waiting_sim_question)
    await safe_edit_text(
        callback,
        "🧪 <b>Q&amp;A Giveaway Simulator</b>\n\n"
        "Send any sample message (e.g. <code>What is 47 * 83?</code>, "
        "<code>Capital of Japan</code>, <code>Unscramble: L E P A P</code>, or casual chat like <code>gg congrats!</code>).\n\n"
        "I'll show you whether the userbot would trigger, its exact giveaway answer, "
        "which engine answered, and exact speed in ms!\n\n"
        "<i>Send /cancel or tap Back when done.</i>",
        reply_markup=cancel_to_dashboard_kb(),
    )


@router.message(DashboardStates.waiting_sim_question)
async def msg_sim_run(message: Message, ctx: AppContext) -> None:
    text = (message.text or "").strip()
    if not text:
        return

    sim_group = GroupConfig(
        chat_id=0,
        title="Simulator",
        enabled=True,
        answer_style="giveaway",
        detection_mode="giveaway_aggressive",
        local_math_enabled=True,
    )
    decision = should_evaluate_message(text, sim_group)
    if not decision.should_process:
        await message.answer(
            f"⏭ <b>Skipped by fast local pre-filter</b> (0ms)\n"
            f"Reason: <code>{decision.reason}</code>\n\n"
            "Send another sample message or tap Back:",
            reply_markup=cancel_to_dashboard_kb(),
        )
        return

    keys = await ctx.db.get_all_provider_keys()
    primary_p = str(await ctx.db.get_setting("primary_provider", "groq"))
    primary_m = str(
        await ctx.db.get_setting("primary_model", PROVIDERS[primary_p].default_model)
    )
    fb_p = await ctx.db.get_setting("fallback_provider")
    fb_m = await ctx.db.get_setting("fallback_model")

    res = await ctx.ai.answer_question(
        text=text,
        answer_style="giveaway",
        primary_provider=primary_p,
        primary_model=primary_m,
        provider_keys=keys,
        fallback_provider=str(fb_p) if fb_p else None,
        fallback_model=str(fb_m) if fb_m else None,
        local_math_enabled=True,
    )

    if res.error:
        await message.answer(
            f"❌ <b>AI Error</b> ({res.latency_ms}ms):\n<code>{html.escape(res.error)}</code>",
            reply_markup=cancel_to_dashboard_kb(),
        )
        return

    if not res.answered:
        await message.answer(
            f"⏭ <b>AI judged NOT a question (SKIP)</b>\n"
            f"🤖 {html.escape(res.provider)} · <code>{html.escape(res.model)}</code> · ⏱ {res.latency_ms}ms\n\n"
            "Send another sample message or tap Back:",
            reply_markup=cancel_to_dashboard_kb(),
        )
        return

    fb_note = " (used fallback!)" if res.used_fallback else ""
    await message.answer(
        f"⚡ <b>Would Reply:</b>\n"
        f"<code>{html.escape(res.answer or '')}</code>\n\n"
        f"🤖 Engine: <b>{html.escape(res.provider)}</b>{fb_note}\n"
        f"🧠 Model: <code>{html.escape(res.model)}</code>\n"
        f"⏱ Speed: <b>{res.latency_ms}ms</b>\n\n"
        "Send another sample message or tap Back:",
        reply_markup=cancel_to_dashboard_kb(),
    )


# ----------------------------------------------------------------------
# Stats & Recent Answers
# ----------------------------------------------------------------------


@router.callback_query(F.data == "dash:stats")
async def cb_stats(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer()
    stats = await ctx.db.get_stats()
    logs = await ctx.db.get_recent_logs(limit=8)

    lines = [
        "📊 <b>Answer Stats &amp; Recent History</b>",
        f"Today: <b>{stats['today']}</b>  |  Total: <b>{stats['total']}</b>  |  "
        f"Avg: <b>{stats['avg_latency_ms']}ms</b>  |  Math: <b>{stats['local_math_hits']}</b>\n",
    ]
    if not logs:
        lines.append("<i>No questions answered yet.</i>")
    else:
        for log in logs:
            q = html.escape(log.question_text[:60] + ("…" if len(log.question_text) > 60 else ""))
            a = html.escape(log.answer_text[:60] + ("…" if len(log.answer_text) > 60 else ""))
            lines.append(
                f"• <b>{html.escape(log.chat_title)}</b> ({log.provider} · {log.latency_ms}ms)\n"
                f"  ❓ <i>{q}</i>\n"
                f"  💬 <code>{a}</code>"
            )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Clear History", callback_data="dash:clear_stats"),
                InlineKeyboardButton(text="⬅️ Dashboard", callback_data="dash:home"),
            ]
        ]
    )
    await safe_edit_text(callback, "\n".join(lines), reply_markup=kb)


@router.callback_query(F.data == "dash:clear_stats")
async def cb_clear_stats(callback: CallbackQuery, ctx: AppContext) -> None:
    await ctx.db.clear_logs()
    await callback.answer("History cleared")
    await cb_stats(callback, ctx)


# ----------------------------------------------------------------------
# Account & Reconnect / Logout
# ----------------------------------------------------------------------


@router.callback_query(F.data == "dash:account")
async def cb_account_menu(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer()
    status = (
        f"🟢 Connected as <b>{html.escape(ctx.userbot.account_display)}</b>"
        if ctx.userbot.is_connected
        else "🔴 Disconnected"
    )
    await safe_edit_text(
        callback,
        "⚙️ <b>Userbot Account &amp; Setup</b>\n\n"
        f"Status: {status}\n\n"
        "You can reconnect your saved session, re-run the guided setup wizard, or log out.",
        reply_markup=account_menu_kb(ctx.userbot.is_connected),
    )


@router.callback_query(F.data == "acc:reconnect")
async def cb_account_reconnect(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer("Reconnecting...")
    ok = await ctx.userbot.start_from_storage()
    if ok:
        await safe_edit_text(
            callback,
            f"✅ Reconnected as <b>{html.escape(ctx.userbot.account_display)}</b>!",
            reply_markup=account_menu_kb(True),
        )
    else:
        await safe_edit_text(
            callback,
            "❌ Could not reconnect saved session. Try re-running the Setup Wizard.",
            reply_markup=account_menu_kb(False),
        )


@router.callback_query(F.data == "acc:export_render")
async def cb_export_render(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer("Exported!")
    state_b64 = await ctx.db.export_state_b64()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Delete Secret Message", callback_data="msg:delete_me")]
        ]
    )
    if callback.message is None:
        return
    if len(state_b64) <= 3400:
        await callback.message.answer(
            "☁️ <b>Render Free Tier Persistence Backup</b>\n\n"
            "Render Free wipes local disk on restart/redeploy. To make your Telegram login, "
            "AI keys, and selected groups survive every restart automatically:\n\n"
            "1. Open your service on <b>Render → Environment</b>\n"
            "2. Add an environment variable named <code>BOT_STATE_B64</code>\n"
            "3. Paste the value below and click <b>Save Changes</b>\n"
            "4. Tap <b>🗑 Delete Secret Message</b> below once copied!\n\n"
            f"<code>{html.escape(state_b64)}</code>",
            reply_markup=kb,
        )
    else:
        await callback.message.answer(
            "⚠️ Your exported config is larger than one Telegram message. "
            "Disable unused groups or configure individual env vars (<code>TG_API_ID</code>, "
            "<code>TG_API_HASH</code>, <code>TG_STRING_SESSION</code>, <code>GROQ_API_KEY</code>).",
            reply_markup=kb,
        )


@router.callback_query(F.data == "msg:delete_me")
async def cb_delete_secret_msg(callback: CallbackQuery) -> None:
    await callback.answer("Deleted")
    if callback.message is not None:
        try:
            await callback.message.delete()
        except Exception:
            pass


@router.callback_query(F.data == "acc:logout_confirm")
async def cb_logout_confirm(callback: CallbackQuery) -> None:
    await callback.answer()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚠️ Yes, Log Out", callback_data="acc:logout_do"),
                InlineKeyboardButton(text="❌ Cancel", callback_data="dash:account"),
            ]
        ]
    )
    await safe_edit_text(
        callback,
        "⚠️ <b>Log out your Telegram Userbot?</b>\n\n"
        "This terminates the saved Telethon session and stops answering in all groups.",
        reply_markup=kb,
    )


@router.callback_query(F.data == "acc:logout_do")
async def cb_logout_do(callback: CallbackQuery, ctx: AppContext) -> None:
    await ctx.userbot.logout()
    await ctx.db.set_setting("setup_completed", False)
    await callback.answer("Logged out")
    await safe_edit_text(
        callback,
        "🚪 Logged out and cleared saved session.\nSend /start anytime to connect an account again.",
    )
