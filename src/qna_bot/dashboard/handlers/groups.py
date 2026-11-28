"""Group selection checklist, search, Telegram sync, and per-group giveaway configuration."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ...storage.database import GroupConfig
from ..context import AppContext, safe_edit_text
from ..filters import NotACommand
from ..keyboards import delay_picker_kb, group_config_kb, groups_list_kb
from ..states import GroupStates

router = Router(name="groups")

_SENDER_CYCLE = ["admins", "humans", "all"]
_STYLE_CYCLE = ["giveaway", "short", "detailed"]
_DETECT_CYCLE = ["giveaway_aggressive", "balanced", "strict"]
_COOLDOWN_CYCLE = [0.0, 1.0, 2.0, 5.0, 10.0, 30.0]
_CONTEXT_CYCLE = [0, 1, 2, 3]


def _next_in_cycle(options: list, current):
    try:
        idx = options.index(current)
        return options[(idx + 1) % len(options)]
    except ValueError:
        return options[0]


async def _render_group_list(
    target: CallbackQuery | Message,
    ctx: AppContext,
    state: FSMContext,
    page: int,
    wizard_mode: bool,
) -> None:
    data = await state.get_data()
    search_query = data.get("group_search_query")
    all_groups = await ctx.db.list_groups()

    if search_query:
        q = str(search_query).lower()
        groups = [
            g for g in all_groups if q in g.title.lower() or (g.username and q in g.username.lower())
        ]
    else:
        groups = all_groups

    enabled_total = sum(1 for g in all_groups if g.enabled)
    header = (
        "<b>Step 4 / 4 — Select Group Chats</b>"
        if wizard_mode
        else "<b>👥 Select &amp; Configure Groups</b>"
    )
    search_line = (
        f"\n🔍 Filter: <code>{html.escape(str(search_query))}</code> ({len(groups)} matches)"
        if search_query
        else ""
    )
    text = (
        f"{header}\n\n"
        f"Active: <b>{enabled_total}</b> / {len(all_groups)} synced groups{search_line}\n"
        "• Tap group name to toggle <b>✅ ON / ⬜ OFF</b>\n"
        "• Tap <b>⚙️</b> to configure per-group giveaway rules"
    )
    kb = groups_list_kb(groups, page=page, wizard_mode=wizard_mode, search_query=search_query)
    if isinstance(target, CallbackQuery):
        await safe_edit_text(target, text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


def _group_settings_text(group: GroupConfig) -> str:
    kw = html.escape(group.trigger_keywords) if group.trigger_keywords else "<i>Auto-detect</i>"
    return (
        f"⚙️ <b>Group Settings: {html.escape(group.title)}</b>\n"
        f"<code>ID: {group.chat_id}</code>\n\n"
        "<b>Tips for Telegram Q&amp;A Giveaways:</b>\n"
        "• <b>👑 Admins Only</b> prevents trolls from baiting your account\n"
        "• <b>⚡ Giveaway Style</b> sends bare exact answers (no fluff)\n"
        "• <b>🧮 Instant Local Math</b> answers math in &lt;1ms\n"
        "• <b>🏁 First Comment Race</b> replies \"I win\" on any \"first comment\"\n"
        "   signal (&lt;1ms, no AI); it posts on its own, so use it only when asked\n"
        "• <b>↩️ Reply to Question</b> replies directly to the host's message\n"
        "• <b>💱 Forex Mode</b> accepts pair shorthand (EURUSD?, GU bias) and\n"
        "   uses FX conventions; it never invents a live price\n"
        "• <b>📈 Live Market Prices</b> fetches real quotes (fx, gold, oil,\n"
        "   indices, yields) so price questions get a number, not a refusal\n"
        f"• <b>🏷 Trigger Keywords:</b> {kw}"
    )


def _delay_picker_text(group: GroupConfig) -> str:
    current = int(group.human_delay_ms)
    shown = "Off" if current <= 0 else f"{current / 1000:g}s"
    return (
        f"⏳ <b>Human Delay: {html.escape(group.title)}</b>\n\n"
        f"Current: <b>{shown}</b>\n\n"
        "How long the bot waits before posting a reply, measured from the moment "
        "the question arrived. It applies to <b>every</b> reply type — instant "
        "math, Forex formulas, Exness history, model answers and photo "
        "questions — so the number means the same thing everywhere.\n\n"
        "A slower answer already past the delay posts immediately; the delay is "
        "a minimum, not an extra wait.\n\n"
        "<i>First Comment Race is capped at 250 ms regardless, so it can still "
        "win the race.</i>\n\n"
        "Pick a delay:"
    )


@router.callback_query(F.data.startswith("gdelay:"))
async def cb_delay_picker(callback: CallbackQuery, ctx: AppContext) -> None:
    """Open the delay picker, or save the chosen value.

    A picker rather than a cycle because the range now runs to 5 s: eleven stops
    tapped one at a time is how the wrong value gets saved during a live
    giveaway.
    """
    parts = (callback.data or "").split(":")
    # gdelay:menu:<cid>:<page>:<mode>  |  gdelay:<cid>:<ms>:<page>:<mode>
    if len(parts) > 1 and parts[1] == "menu":
        chat_id = int(parts[2])
        back_page = int(parts[3])
        mode_flag = parts[4] if len(parts) > 4 else "d"
        group = await ctx.db.get_group(chat_id)
        if not group:
            await callback.answer("Group not found", show_alert=True)
            return
        await callback.answer()
        await safe_edit_text(
            callback,
            _delay_picker_text(group),
            reply_markup=delay_picker_kb(group, back_page, mode_flag),
        )
        return

    chat_id = int(parts[1])
    chosen = int(parts[2])
    back_page = int(parts[3])
    mode_flag = parts[4] if len(parts) > 4 else "d"

    group = await ctx.db.get_group(chat_id)
    if not group:
        await callback.answer("Group not found", show_alert=True)
        return
    group.human_delay_ms = chosen
    await ctx.db.save_group(group)
    await ctx.userbot.reload_cache()
    await callback.answer("Off" if chosen == 0 else f"Delay: {chosen / 1000:g}s")
    await safe_edit_text(
        callback,
        _group_settings_text(group),
        reply_markup=group_config_kb(group, back_page, mode_flag),
    )


@router.callback_query(F.data.startswith("grp:list:"))
async def cb_group_list(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await callback.answer()
    parts = (callback.data or "").split(":")
    page = int(parts[2]) if len(parts) > 2 else 0
    wizard_mode = len(parts) > 3 and parts[3] == "w"

    groups = await ctx.db.list_groups()
    if not groups and ctx.userbot.is_connected:
        await safe_edit_text(callback, "🔄 Syncing your Telegram groups for the first time...")
        try:
            await ctx.userbot.sync_groups()
        except Exception as exc:
            await safe_edit_text(callback, f"❌ Sync failed: <code>{html.escape(str(exc))}</code>")
            return

    await _render_group_list(callback, ctx, state, page=page, wizard_mode=wizard_mode)


@router.callback_query(F.data.startswith("grp:sync:"))
async def cb_group_sync(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    wizard_mode = (callback.data or "").endswith(":w")
    if not ctx.userbot.is_connected:
        await callback.answer("Userbot is offline! Reconnect in Account menu.", show_alert=True)
        return
    await callback.answer("Syncing groups from Telegram...")
    try:
        await ctx.userbot.sync_groups()
    except Exception as exc:
        await safe_edit_text(callback, f"❌ Sync failed: <code>{html.escape(str(exc))}</code>")
        return
    await _render_group_list(callback, ctx, state, page=0, wizard_mode=wizard_mode)


@router.callback_query(F.data.startswith("grp:tog:"))
async def cb_group_toggle(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    parts = (callback.data or "").split(":")
    chat_id = int(parts[2])
    page = int(parts[3])
    wizard_mode = len(parts) > 4 and parts[4] == "w"

    updated = await ctx.db.toggle_group_enabled(chat_id)
    await ctx.userbot.reload_cache()
    if updated and updated.enabled and updated.sender_filter == "admins":
        # Warm admin cache immediately so the very first giveaway question is instant
        await ctx.userbot.refresh_group_admins(chat_id)

    status_txt = f"{'✅ Enabled' if updated and updated.enabled else '⬜ Disabled'}"
    await callback.answer(status_txt)
    await _render_group_list(callback, ctx, state, page=page, wizard_mode=wizard_mode)


@router.callback_query(F.data == "grp:disable_all")
async def cb_group_disable_all(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await ctx.db.disable_all_groups()
    await ctx.userbot.reload_cache()
    await callback.answer("All groups disabled")
    await _render_group_list(callback, ctx, state, page=0, wizard_mode=False)


@router.callback_query(F.data.startswith("grp:search:"))
async def cb_group_search(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    mode_flag = (callback.data or "").split(":")[2]
    await state.update_data(group_search_mode=mode_flag)
    await state.set_state(GroupStates.waiting_search_query)
    await safe_edit_text(
        callback,
        "🔍 <b>Search Groups</b>\n\nSend part of the group name or @username:",
    )


@router.message(NotACommand(), GroupStates.waiting_search_query)
async def msg_group_search(message: Message, ctx: AppContext, state: FSMContext) -> None:
    query = (message.text or "").strip()
    data = await state.get_data()
    wizard_mode = data.get("group_search_mode") == "w"
    await state.set_state(None)
    await state.update_data(group_search_query=query)
    await _render_group_list(message, ctx, state, page=0, wizard_mode=wizard_mode)


@router.callback_query(F.data.startswith("grp:clear_search:"))
async def cb_clear_search(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await callback.answer("Search cleared")
    wizard_mode = (callback.data or "").endswith(":w")
    await state.update_data(group_search_query=None)
    await _render_group_list(callback, ctx, state, page=0, wizard_mode=wizard_mode)


# ----------------------------------------------------------------------
# Per-Group Settings Editor
# ----------------------------------------------------------------------


@router.callback_query(F.data.startswith("grp:cfg:"))
async def cb_group_cfg(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer()
    parts = (callback.data or "").split(":")
    chat_id = int(parts[2])
    back_page = int(parts[3])
    mode_flag = parts[4] if len(parts) > 4 else "d"

    group = await ctx.db.get_group(chat_id)
    if not group:
        await callback.answer("Group not found", show_alert=True)
        return
    await safe_edit_text(
        callback,
        _group_settings_text(group),
        reply_markup=group_config_kb(group, back_page, mode_flag),
    )


@router.callback_query(F.data.startswith("gcfg:"))
async def cb_group_cfg_update(
    callback: CallbackQuery, ctx: AppContext, state: FSMContext
) -> None:
    parts = (callback.data or "").split(":")
    chat_id = int(parts[1])
    field = parts[2]
    back_page = int(parts[3])
    mode_flag = parts[4] if len(parts) > 4 else "d"

    group = await ctx.db.get_group(chat_id)
    if not group:
        await callback.answer("Group not found", show_alert=True)
        return

    if field == "keywords":
        await callback.answer()
        await state.update_data(
            kw_chat_id=chat_id, kw_back_page=back_page, kw_mode_flag=mode_flag
        )
        await state.set_state(GroupStates.waiting_trigger_keywords)
        await safe_edit_text(
            callback,
            f"🏷 <b>Trigger Keywords for {html.escape(group.title)}</b>\n\n"
            "Send comma-separated keywords that a message MUST contain to trigger an answer "
            "(example: <code>Q:, Question, Giveaway, Trivia</code>).\n\n"
            "Send <code>clear</code> to return to automatic detection.",
        )
        return

    if field == "admins":
        admins = await ctx.userbot.refresh_group_admins(chat_id)
        await callback.answer(f"Cached {len(admins)} group admins!", show_alert=True)
        return

    if field == "enabled":
        group.enabled = not group.enabled
    elif field == "sender":
        group.sender_filter = _next_in_cycle(_SENDER_CYCLE, group.sender_filter)
    elif field == "style":
        group.answer_style = _next_in_cycle(_STYLE_CYCLE, group.answer_style)
    elif field == "detect":
        group.detection_mode = _next_in_cycle(_DETECT_CYCLE, group.detection_mode)
    elif field == "reply":
        group.reply_to_message = not group.reply_to_message
    elif field == "math":
        group.local_math_enabled = not group.local_math_enabled
    elif field == "firstcmt":
        group.first_comment_enabled = not group.first_comment_enabled
    elif field == "vision":
        group.vision_enabled = not group.vision_enabled
    elif field == "forex":
        group.forex_enabled = not group.forex_enabled
    elif field == "fxlive":
        group.forex_live_data = not group.forex_live_data
    elif field == "cooldown":
        group.cooldown_seconds = _next_in_cycle(_COOLDOWN_CYCLE, group.cooldown_seconds)
    elif field == "delay":
        # Backwards compatibility: an old message's keyboard may still carry the
        # cycling callback. Open the picker instead of silently stepping.
        await callback.answer()
        await safe_edit_text(
            callback,
            _delay_picker_text(group),
            reply_markup=delay_picker_kb(group, back_page, mode_flag),
        )
        return
    elif field == "context":
        group.context_messages = _next_in_cycle(_CONTEXT_CYCLE, group.context_messages)

    await ctx.db.save_group(group)
    await ctx.userbot.reload_cache()
    if group.enabled and group.sender_filter == "admins":
        await ctx.userbot.refresh_group_admins(chat_id)

    await callback.answer("Updated")
    await safe_edit_text(
        callback,
        _group_settings_text(group),
        reply_markup=group_config_kb(group, back_page, mode_flag),
    )


@router.message(NotACommand(), GroupStates.waiting_trigger_keywords)
async def msg_trigger_keywords(message: Message, ctx: AppContext, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    data = await state.get_data()
    chat_id = int(data["kw_chat_id"])
    back_page = int(data.get("kw_back_page", 0))
    mode_flag = str(data.get("kw_mode_flag", "d"))
    await state.set_state(None)

    group = await ctx.db.get_group(chat_id)
    if not group:
        await message.answer("❌ Group not found.")
        return

    group.trigger_keywords = "" if raw.lower() in ("clear", "none", "-") else raw
    await ctx.db.save_group(group)
    await ctx.userbot.reload_cache()

    await message.answer(
        _group_settings_text(group),
        reply_markup=group_config_kb(group, back_page, mode_flag),
    )
