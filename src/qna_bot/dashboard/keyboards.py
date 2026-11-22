"""Inline keyboard builders for the Setup Wizard, Group Selector, and Dashboard."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..ai.providers import PROVIDERS
from ..storage.database import GroupConfig

GROUPS_PER_PAGE = 7


def main_dashboard_kb(global_enabled: bool, notify_on: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(
        text="⏸ Pause Responder" if global_enabled else "▶️ Resume Responder",
        callback_data="dash:toggle_global",
    )
    b.button(text="🔄 Refresh", callback_data="dash:home")
    b.button(text="👥 Select & Configure Groups", callback_data="grp:list:0")
    b.button(text="🤖 AI Providers & Models", callback_data="ai:menu")
    b.button(text="🧪 Test Q&A Simulator", callback_data="dash:sim")
    b.button(text="📊 Stats & Recent Logs", callback_data="dash:stats")
    b.button(
        text=f"📣 Alerts: {'ON' if notify_on else 'OFF'}",
        callback_data="dash:toggle_notify",
    )
    b.button(text="⚙️ Account & Setup", callback_data="dash:account")
    b.adjust(2, 1, 1, 2, 2)
    return b.as_markup()


def wizard_welcome_kb(has_env_creds: bool, is_connected: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if is_connected:
        b.button(text="✅ Keep Connected Account → AI Setup", callback_data="wiz:ai_step")
        b.button(text="🔄 Switch Telegram Account", callback_data="wiz:step1_creds")
    else:
        if has_env_creds:
            b.button(text="⚡ Use API ID & Hash from .env", callback_data="wiz:use_env_creds")
        b.button(text="🚀 Start Guided Setup", callback_data="wiz:step1_creds")
    b.adjust(1)
    return b.as_markup()


def wizard_login_method_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📱 Login with Phone + OTP Keypad", callback_data="wiz:login_phone")
    b.button(text="📷 Login with QR Code (no SMS needed)", callback_data="wiz:login_qr")
    b.button(text="🔑 Paste Existing StringSession", callback_data="wiz:login_session")
    b.button(text="⬅️ Re-enter API Credentials", callback_data="wiz:step1_creds")
    b.adjust(1)
    return b.as_markup()


def otp_keypad_kb(current_digits: str) -> InlineKeyboardMarkup:
    """Inline numeric keypad so users NEVER send raw Telegram OTP codes as plain chat text."""
    b = InlineKeyboardBuilder()
    for d in ("1", "2", "3", "4", "5", "6", "7", "8", "9"):
        b.button(text=d, callback_data=f"otp:d:{d}")
    b.button(text="⌫", callback_data="otp:back")
    b.button(text="0", callback_data="otp:d:0")
    b.button(text="🗑 Clear", callback_data="otp:clear")
    b.button(
        text=f"✅ Submit ({len(current_digits)} digits)" if current_digits else "✅ Submit",
        callback_data="otp:submit",
    )
    b.button(text="🔄 Resend Code", callback_data="otp:resend")
    b.button(text="❓ No code received?", callback_data="otp:help")
    b.button(text="❌ Cancel", callback_data="wiz:login_method")
    b.adjust(3, 3, 3, 3, 1, 2, 1)
    return b.as_markup()


def provider_picker_kb(
    prefix: str,
    configured_keys: dict[str, str],
    include_none: bool = False,
    back_cb: str | None = None,
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for slug, spec in PROVIDERS.items():
        status = " ✅" if slug in configured_keys else ""
        b.button(text=f"{spec.emoji} {spec.name}{status}", callback_data=f"{prefix}:{slug}")
    if include_none:
        b.button(text="🚫 Disable Fallback", callback_data=f"{prefix}:none")
    if back_cb:
        b.button(text="⬅️ Back", callback_data=back_cb)
    b.adjust(1)
    return b.as_markup()


def model_picker_kb(
    provider: str,
    prefix: str,
    active_model: str | None = None,
    extra_models: list[str] | None = None,
    back_cb: str = "ai:menu",
    include_live_fetch: bool = True,
) -> tuple[InlineKeyboardMarkup, list[str]]:
    """Build model picker with safe short index callbacks (<64 bytes even for long model IDs)."""
    spec = PROVIDERS[provider]
    b = InlineKeyboardBuilder()
    model_ids: list[str] = []

    if extra_models is not None:
        for idx, model_id in enumerate(extra_models):
            model_ids.append(model_id)
            mark = "✅ " if model_id == active_model else ""
            b.button(text=f"{mark}{model_id}", callback_data=f"{prefix}:idx:{idx}")
    else:
        for idx, preset in enumerate(spec.presets):
            model_ids.append(preset.model_id)
            mark = "✅ " if preset.model_id == active_model else ""
            b.button(
                text=f"{mark}{preset.label}",
                callback_data=f"{prefix}:idx:{idx}",
            )

    if include_live_fetch:
        b.button(text="🔄 Fetch Live Models from API", callback_data=f"{prefix}:live:{provider}")
    b.button(text="✏️ Enter Custom Model ID", callback_data=f"{prefix}:custom:{provider}")
    b.button(text="⬅️ Back", callback_data=back_cb)
    b.adjust(1)
    return b.as_markup(), model_ids


def groups_list_kb(
    groups: list[GroupConfig],
    page: int,
    *,
    wizard_mode: bool = False,
    search_query: str | None = None,
) -> InlineKeyboardMarkup:
    total = len(groups)
    total_pages = max(1, (total + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * GROUPS_PER_PAGE
    chunk = groups[start : start + GROUPS_PER_PAGE]

    rows: list[list[InlineKeyboardButton]] = []
    mode_flag = "w" if wizard_mode else "d"

    for g in chunk:
        icon = "✅" if g.enabled else "⬜"
        short_title = g.title[:26] + ("…" if len(g.title) > 26 else "")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {short_title}",
                    callback_data=f"grp:tog:{g.chat_id}:{page}:{mode_flag}",
                ),
                InlineKeyboardButton(
                    text="⚙️",
                    callback_data=f"grp:cfg:{g.chat_id}:{page}:{mode_flag}",
                ),
            ]
        )

    # Pagination row
    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(
                    text="◀️ Prev", callback_data=f"grp:list:{page - 1}:{mode_flag}"
                )
            )
        nav_row.append(
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop")
        )
        if page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton(
                    text="Next ▶️", callback_data=f"grp:list:{page + 1}:{mode_flag}"
                )
            )
        rows.append(nav_row)

    # Utility row
    util_row = [
        InlineKeyboardButton(text="🔄 Sync Groups", callback_data=f"grp:sync:{mode_flag}"),
        InlineKeyboardButton(
            text="❌ Clear Search" if search_query else "🔍 Search",
            callback_data=f"grp:clear_search:{mode_flag}"
            if search_query
            else f"grp:search:{mode_flag}",
        ),
    ]
    rows.append(util_row)

    if wizard_mode:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Finish Setup & Open Dashboard",
                    callback_data="wiz:finish",
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(text="🚫 Disable All", callback_data="grp:disable_all"),
                InlineKeyboardButton(text="⬅️ Dashboard", callback_data="dash:home"),
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_config_kb(group: GroupConfig, back_page: int, mode_flag: str) -> InlineKeyboardMarkup:
    sender_labels = {
        "admins": "👑 Admins Only",
        "humans": "👥 Humans Only",
        "all": "🌐 Everyone (+Bots)",
    }
    style_labels = {
        "giveaway": "⚡ Giveaway (Ultra-Short)",
        "short": "💬 Short (1 sentence)",
        "detailed": "📖 Detailed",
    }
    detect_labels = {
        "giveaway_aggressive": "🔥 Giveaway Aggressive",
        "balanced": "⚖️ Balanced",
        "strict": "🔒 Strict (? required)",
    }

    cid = group.chat_id
    b = InlineKeyboardBuilder()
    b.button(
        text=f"Status: {'🟢 ENABLED' if group.enabled else '🔴 DISABLED'}",
        callback_data=f"gcfg:{cid}:enabled:{back_page}:{mode_flag}",
    )
    b.button(
        text=f"Who triggers: {sender_labels.get(group.sender_filter, group.sender_filter)}",
        callback_data=f"gcfg:{cid}:sender:{back_page}:{mode_flag}",
    )
    b.button(
        text=f"Answer style: {style_labels.get(group.answer_style, group.answer_style)}",
        callback_data=f"gcfg:{cid}:style:{back_page}:{mode_flag}",
    )
    b.button(
        text=f"Detection: {detect_labels.get(group.detection_mode, group.detection_mode)}",
        callback_data=f"gcfg:{cid}:detect:{back_page}:{mode_flag}",
    )
    b.button(
        text=f"↩️ Reply to question: {'ON' if group.reply_to_message else 'OFF'}",
        callback_data=f"gcfg:{cid}:reply:{back_page}:{mode_flag}",
    )
    b.button(
        text=f"🧮 Instant Local Math: {'ON' if group.local_math_enabled else 'OFF'}",
        callback_data=f"gcfg:{cid}:math:{back_page}:{mode_flag}",
    )
    b.button(
        text=f"⏱ Cooldown: {group.cooldown_seconds:g}s",
        callback_data=f"gcfg:{cid}:cooldown:{back_page}:{mode_flag}",
    )
    b.button(
        text=f"⏳ Delay: {group.human_delay_ms}ms",
        callback_data=f"gcfg:{cid}:delay:{back_page}:{mode_flag}",
    )
    kw_label = group.trigger_keywords[:18] + "…" if len(group.trigger_keywords) > 18 else (group.trigger_keywords or "Auto")
    b.button(
        text=f"🏷 Keywords: {kw_label}",
        callback_data=f"gcfg:{cid}:keywords:{back_page}:{mode_flag}",
    )
    b.button(
        text=f"🧠 Context msgs: {group.context_messages}",
        callback_data=f"gcfg:{cid}:context:{back_page}:{mode_flag}",
    )
    b.button(
        text="🔄 Refresh Group Admins",
        callback_data=f"gcfg:{cid}:admins:{back_page}:{mode_flag}",
    )
    b.button(
        text="⬅️ Back to Groups",
        callback_data=f"grp:list:{back_page}:{mode_flag}",
    )
    b.adjust(1, 1, 1, 1, 2, 2, 2, 1, 1)
    return b.as_markup()


def ai_menu_kb(has_fallback: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Switch Primary Provider", callback_data="ai:pick_primary")
    b.button(text="🧠 Change Primary Model", callback_data="ai:pick_primary_model")
    b.button(
        text="🛡 Change Fallback Provider" if has_fallback else "🛡 Add Fallback Provider",
        callback_data="ai:pick_fallback",
    )
    b.button(text="🔑 Add / Update API Keys", callback_data="ai:manage_keys")
    b.button(text="⚡ Benchmark All Configured Providers", callback_data="ai:benchmark")
    b.button(text="⬅️ Dashboard", callback_data="dash:home")
    b.adjust(1)
    return b.as_markup()


def account_menu_kb(is_connected: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if not is_connected:
        b.button(text="🔌 Reconnect Saved Session", callback_data="acc:reconnect")
    b.button(text="☁️ Export Render Config (BOT_STATE_B64)", callback_data="acc:export_render")
    b.button(text="🧭 Re-run Full Setup Wizard", callback_data="wiz:restart")
    b.button(text="🚪 Log Out Userbot Account", callback_data="acc:logout_confirm")
    b.button(text="⬅️ Dashboard", callback_data="dash:home")
    b.adjust(1)
    return b.as_markup()


def cancel_to_dashboard_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Back to Dashboard", callback_data="dash:home")
    return b.as_markup()
