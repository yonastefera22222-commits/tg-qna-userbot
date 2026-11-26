"""Main Dashboard actions: kill switch, Q&A simulator, stats/logs, alerts, account management."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ...ai.language import is_amharic
from ...ai.providers import PROVIDERS
from ...storage.database import GroupConfig
from ...userbot.filters import should_evaluate_message
from ..context import AppContext, safe_edit_text
from ..filters import NotACommand
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


@router.message(NotACommand(), DashboardStates.waiting_sim_question)
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
    # Mirror a real group's optional modes so the simulator does not disagree
    # with production (e.g. rejecting "GU bias" that a Forex group would answer).
    for existing in await ctx.db.list_groups():
        if existing.enabled:
            sim_group.forex_enabled = existing.forex_enabled
            break
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
        forex_group=sim_group.forex_enabled,
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


SHARE_STEPS = (
    "🎁 <b>Share Your Setup With Someone</b>\n\n"
    "This snapshot carries your <b>groups and their settings</b> only. It contains\n"
    "<b>no credentials</b>: no Telegram session, no AI API keys, no owner id. It is\n"
    "safe to send, but treat it as private — it reveals which groups you monitor.\n\n"
    "<b>Do NOT send them your Backup snapshot instead.</b> A Telethon session string\n"
    "is a complete account login: whoever holds it <i>is</i> your Telegram account.\n\n"
    "<b>What they should do:</b>\n"
    "1. Fork this repository to their own GitHub account\n"
    "2. Create their own Render Blueprint (or Web Service) from the fork\n"
    "3. Set <b>BOT_TOKEN</b> (their @BotFather token) and <b>OWNER_ID</b> (their own\n"
    "   Telegram id from @userinfobot) in Render → Environment\n"
    "4. Paste the value below into <b>BOT_STATE_B64</b> in Render → Environment\n"
    "5. Deploy, then log in <b>their own</b> Telegram account and add <b>their own</b>\n"
    "   AI key in the dashboard — credentials never transfer\n\n"
)

NO_CREDENTIALS_NOTE = (
    "🔒 <i>This snapshot contains groups and settings only. tg_session, API keys "
    "and owner_id are excluded by design.</i>"
)


@router.callback_query(F.data == "acc:export_share")
async def cb_export_share(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer("Preparing a credential-free snapshot...")
    state_b64 = await ctx.db.export_state_b64(profile="share")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Delete Secret Message", callback_data="msg:delete_me")]
        ]
    )
    if callback.message is None:
        return
    if len(state_b64) > 3400:
        await callback.message.answer(
            SHARE_STEPS
            + "⚠️ Your snapshot is too large for one Telegram message. Enable fewer "
            "groups, or have them configure groups by hand after deploying.",
            reply_markup=kb,
        )
        return
    await callback.message.answer(
        SHARE_STEPS + NO_CREDENTIALS_NOTE + f"\n\n<code>{html.escape(state_b64)}</code>",
        reply_markup=kb,
    )


@router.callback_query(F.data == "acc:session_string")
async def cb_session_string(callback: CallbackQuery, ctx: AppContext) -> None:
    """Hand the owner the one value that makes a free host survivable.

    On Render Free the disk is wiped on every redeploy, restart and 15-minute idle
    spin-down (https://render.com/docs/free), and `data/bot.db` - where the login
    lives - is on that disk. `TG_STRING_SESSION` is read at boot, so pasting this
    into the host's Environment tab means the bot starts already logged in and
    nothing is lost. Getting it from here avoids the terminal entirely, which is
    the only way this is usable from a phone.
    """
    await callback.answer("Preparing...")
    payload = await ctx.userbot.session_file_bytes()
    if payload is None:
        await callback.message.answer(
            "❌ There is no saved session yet. Finish the Telegram login first, "
            "then come back here."
        )
        return

    filename, blob = payload
    session_str = blob.decode("utf-8").strip()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Delete Secret Message", callback_data="msg:delete_me")]
        ]
    )
    await callback.message.answer_document(
        BufferedInputFile(blob, filename=filename),
        caption=(
            "🔑 <b>Your Telegram session string</b>\n\n"
            "⚠️ <b>This is your account.</b> Anyone who has this file can read and "
            "send messages as you. Keep it in your own host's settings and nowhere "
            "else.\n\n"
            "Why you want it: on a free host the disk is wiped on every redeploy, "
            "restart and idle spin-down, which erases the login you just made. This "
            "value lives in an environment variable instead, so it survives.\n\n"
            "<b>Copy it verbatim from the file</b> (do not retype — phone copy can "
            "silently insert spaces).\n\n"
            "Then: host dashboard → <b>Environment</b> → add\n"
            "• <code>TG_STRING_SESSION</code> = the string\n"
            "• <code>TG_API_ID</code> / <code>TG_API_HASH</code> = your API credentials\n"
            "• <code>ENCRYPTION_KEY</code> = your fixed key\n\n"
            "Save Changes. After the redeploy the bot starts already connected — no "
            "code, and nothing left to lose.\n\n"
            "To revoke later: Telegram → Settings → Devices → terminate this session.",
        ),
        reply_markup=kb,
    )
    # The string is also short enough to read inline, which helps when file
    # copying is awkward on the device at hand.
    if len(session_str) <= 3400:
        await callback.message.answer(
            "For convenience, here it is inline as well:\n\n"
            f"<code>{html.escape(session_str)}</code>",
            reply_markup=kb,
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
                "🔐 <b>This backup contains your Telegram session and AI keys.</b>\n"
                "Anyone who has it can log in as you. Keep it in your own Render env "
                "only — never send it to another person. To share a setup, use "
                "<b>🎁 Share Setup With Someone</b> instead.\n\n"
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


# ----------------------------------------------------------------------
# /diag — end-to-end responder diagnostics
# ----------------------------------------------------------------------


@router.message(Command("diag"))
async def cmd_diag(message: Message, ctx: AppContext) -> None:
    """Show exactly why the userbot is or is not answering.

    Every gate between "a message arrives" and "a reply is sent" is reported, in
    order, together with the most recent skip reasons. Before this, a misconfigured
    group produced complete silence with no way to tell where it stopped.
    """
    ub = ctx.userbot
    users_ok = ub.is_connected
    global_on = bool(await ctx.db.get_setting("global_enabled", True))
    groups = await ctx.db.list_groups()
    enabled = [g for g in groups if g.enabled]
    counters = ub.counters

    lines = [
        "🩺 <b>Responder Diagnostics</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"{'🟢' if users_ok else '🔴'} <b>Telegram account:</b> "
        f"{'connected as ' + html.escape(ub.account_display) if users_ok else 'NOT connected'}",
        f"{'🟢' if global_on else '⏸'} <b>Responder:</b> "
        f"{'ACTIVE' if global_on else 'PAUSED (/dashboard to resume)'}",
        f"👥 <b>Groups enabled:</b> {len(enabled)} / {len(groups)} synced",
        f"📥 <b>Messages seen since start:</b> {counters['messages_seen']}",
        f"💬 <b>Answers sent since start:</b> {counters['answers_sent']}",
    ]
    if counters.get("own_messages"):
        lines.append(
            f"↩️ <b>Own messages skipped:</b> {counters['own_messages']} "
            "<i>(a userbot cannot answer itself)</i>"
        )

    # A brand-new deployment - a Render blueprint deploy especially - starts with
    # an empty group table: nothing is enabled until the owner opens the group
    # menu, which silently syncs. "Not responding" is then literally true and not
    # a bug, so say so here instead of leaving them to guess.
    if not groups:
        lines.append(
            "⬛ <b>No groups synced yet</b> — the bot has nothing to answer in. "
            "Open /dashboard → 👥 Select &amp; Configure Groups: it syncs your "
            "joined groups on first open, then tap a group to enable it."
        )

    ai_ok = bool(await ctx.db.get_all_provider_keys())
    primary = str(await ctx.db.get_setting("primary_provider", "—"))
    primary_model = str(await ctx.db.get_setting("primary_model", "—"))
    lines.append(
        f"{'🟢' if ai_ok else '🔴'} <b>AI:</b> {html.escape(primary)} · "
        f"<code>{html.escape(primary_model)}</code>"
        + ("" if ai_ok else " (no API key saved)")
    )

    # Live market feed: Forex groups are only as good as the numbers they can
    # actually reach from the host. Prove it here instead of guessing.
    fx_groups = [g for g in enabled if g.forex_enabled]
    if fx_groups:
        live_on = [g for g in fx_groups if g.forex_live_data]
        if not live_on:
            lines.append("📈 <b>Live prices:</b> OFF for all Forex groups")
        else:
            feed = ctx.ai.market_feed
            if not await feed.configured():
                lines.append(
                    "🔴 <b>Live prices:</b> Exness not connected — "
                    "AI menu → 📈 Exness Prices to add the API key"
                )
            else:
                probe = await feed.snapshot("EURUSD and gold")
                if probe.is_empty():
                    reason = feed.last_error or "no price returned"
                    lines.append(
                        "🔴 <b>Live prices:</b> Exness unreachable "
                        f"(<code>{html.escape(reason)}</code>) — "
                        "price questions will answer <code>No live data</code>"
                    )
                else:
                    sample = "; ".join(q.label(detailed=True) for q in probe.quotes[:3])
                    lines.append(
                        f"🟢 <b>Live prices:</b> {html.escape(sample)}"
                    )
                    if any(q.kind == "market_closed" for q in probe.quotes):
                        lines.append(
                            "   <i>market closed — these are the last Exness ticks</i>"
                        )

    # Per-group verdict
    if enabled:
        lines += ["", "<b>Enabled groups</b>"]
        for g in enabled[:6]:
            notes = []
            if g.sender_filter == "admins":
                entry = ub._admin_cache.get(g.chat_id)  # noqa: SLF001 - diagnostics
                if entry is None:
                    notes.append("admins not fetched yet")
                elif entry[2]:
                    notes.append(f"👑 {len(entry[1])} admins")
                else:
                    notes.append("👑 admin list unavailable → answering everyone")
            else:
                notes.append({"humans": "👥 humans only", "all": "🌐 everyone"}.get(
                    g.sender_filter, g.sender_filter
                ))
            notes.append({"giveaway_aggressive": "🔥 aggressive", "balanced": "⚖️ balanced",
                          "strict": "🔒 strict"}.get(g.detection_mode, g.detection_mode))
            if g.trigger_keyword_list():
                notes.append(f"🏷 must contain: {html.escape(g.trigger_keywords)}")
            if g.forex_enabled:
                notes.append("💱 forex" + (" + 📈 live" if g.forex_live_data else ""))
            title = html.escape(g.title[:32])
            lines.append(f"• <b>{title}</b> — {' · '.join(notes)}")
        if len(enabled) > 6:
            lines.append(f"…and {len(enabled) - 6} more")

    # Recent decisions
    recent = ub.recent_decisions
    amharic_seen = any(is_amharic(str(d.get("text") or "")) for d in recent)
    if recent:
        lines += ["", "<b>Recent messages (newest last)</b>"]
        for d in recent[-8:]:
            icon = "✅" if d["outcome"] == "answered" else "⛔"
            detail = str(d.get("detail") or d["outcome"])
            # Amharic questions are easy to mistake for "nothing happened", so
            # label them explicitly in the trail.
            if is_amharic(str(d.get("text") or "")):
                detail = f"🇪🇹 Amharic · {detail}"
            snippet = html.escape(str(d.get("text") or "—")[:40])
            lines.append(
                f"{icon} <code>{d['at']}</code> <i>{snippet}</i>\\n"
                f"   ↳ {html.escape(detail[:110])}"
            )
    else:
        lines += [
            "",
            "📭 <b>No messages seen yet since the bot started.</b>",
            "<i>That means the account is not receiving group updates at all — check "
            "that the groups are enabled, and that this is the account that is in them.</i>",
        ]

    # Actionable verdict
    lines += ["", "<b>Verdict</b>"]
    if not users_ok:
        lines.append("🔴 Log the Telegram account in first (/start Step 2).")
    elif not global_on:
        lines.append("⏸ The responder is paused. Tap /dashboard → Resume Responder.")
    elif not enabled:
        if not groups:
            lines.append(
                "⬛ No groups are enabled — none have been synced yet. Tap /dashboard → "
                "👥 Select &amp; Configure Groups (it imports your joined groups), then "
                "enable the ones the bot should answer in."
            )
        else:
            lines.append(
                "⬛ No groups are enabled. Tap /dashboard → Select &amp; Configure Groups."
            )
    elif not ai_ok:
        lines.append("🔴 No AI API key saved. Re-run Step 3 in /setup.")
    elif counters["messages_seen"] == 0 and counters.get("own_messages"):
        lines.append(
            "↩️ Only messages sent from this same account have arrived. A userbot "
            "cannot answer itself, so **test from a different account** - or you "
            "will see silence no matter what the settings are."
        )
    elif counters["messages_seen"] == 0:
        lines.append(
            "⚠️ The account is connected but no group messages have arrived. Most common "
            "cause: the enabled groups are not ones this account is actually in, or the "
            "service was restarted and updates were missed while it slept."
        )
    elif amharic_seen and counters["answers_sent"] == 0:
        lines.append(
            "🇪🇹 Amharic messages ARE arriving but none were answered. Amharic "
            "words need no '?', so try detection mode 🔥 Aggressive for those groups."
        )
    elif counters["answers_sent"] == 0:
        lines.append(
            "⚠️ Messages ARE arriving but none passed the filters. Read the per-group "
            "notes above: try detection mode 🔥 Aggressive, clear custom keywords, and "
            "set who-triggers to 🌐 Everyone for testing."
        )
    else:
        lines.append("🟢 The responder is working — answers are being sent.")

    lines.append(
        "\\n<i>Tip: send /diag right after posting a test question in a group to see "
        "the exact gate that stopped it.</i>"
    )
    await message.answer("\\n".join(lines), disable_web_page_preview=True)


@router.message(Command("testanswer"))
async def cmd_testanswer(message: Message, ctx: AppContext) -> None:
    """Prove the whole pipeline end-to-end without needing a real giveaway.

    Runs the exact local filter + AI call the userbot uses for an incoming
    message, and reports which stage answered or refused.
    """
    sample = "What is the capital of Kenya?"
    groups = [g for g in await ctx.db.list_groups() if g.enabled]

    lines = ["🧪 <b>Pipeline test</b>", f"<i>Sample question:</i> {html.escape(sample)}", ""]

    if not groups:
        lines.append("⬛ No enabled groups — enable one first.")
        await message.answer("\n".join(lines))
        return

    group = groups[0]
    decision = should_evaluate_message(sample, group)
    lines.append(
        f"1️⃣ <b>Local filter</b> ({html.escape(group.title[:30])}): "
        + ("✅ passed — " + html.escape(decision.reason) if decision.should_process
           else "⛔ blocked — " + html.escape(decision.reason))
    )
    if not decision.should_process:
        lines.append(
            "\n<i>The filter is stopping questions in this group. Set detection mode to "
            "🔥 Aggressive and clear custom keywords to fix it.</i>"
        )
        await message.answer("\n".join(lines))
        return

    keys = await ctx.db.get_all_provider_keys()
    provider = str(await ctx.db.get_setting("primary_provider", "groq"))
    spec = PROVIDERS.get(provider, PROVIDERS["groq"])
    model = str(await ctx.db.get_setting("primary_model", spec.default_model))

    if provider not in keys:
        lines.append(f"2️⃣ <b>AI call</b>: ⛔ no API key saved for {html.escape(provider)}")
        await message.answer("\n".join(lines))
        return

    result = await ctx.ai.answer_question(
        text=sample,
        forex_group=group.forex_enabled,
        answer_style=group.answer_style,
        primary_provider=provider,
        primary_model=model,
        provider_keys=keys,
        local_math_enabled=group.local_math_enabled,
    )
    if result.error:
        lines.append(f"2️⃣ <b>AI call</b>: ⛔ {html.escape(result.error[:300])}")
    elif result.answered:
        lines.append(
            f"2️⃣ <b>AI call</b>: ✅ answered <code>{html.escape(result.answer or '')}</code> "
            f"via {html.escape(result.provider)}/{html.escape(result.model)} "
            f"in {result.latency_ms}ms"
        )
        lines.append("\n🟢 <b>Pipeline is healthy.</b> If the group still gets no replies, "
                     "the account is probably not connected to that group's updates — "
                     "send /diag right after posting a question there.")
    else:
        lines.append("2️⃣ <b>AI call</b>: ⛔ the model declined to answer (SKIP)")

    await message.answer("\n".join(lines))
