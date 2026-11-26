"""Inline keyboard builders for the Setup Wizard, Group Selector, and Dashboard."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..ai.providers import PROVIDERS
from ..storage.database import GroupConfig

GROUPS_PER_PAGE = 7


#: The human delay values offered in the picker, in milliseconds.
#:
#: A picker rather than a cycle: nine stops is far too many to tap through one
#: at a time, and "tap five times and hope" is how the wrong delay gets saved
#: during a live giveaway. 500 ms steps up to 5 s.
HUMAN_DELAY_CHOICES = (0, 500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000)


def _delay_label(human_delay_ms: int) -> str:
    """Describe the delay by what it does, not just by its number.

    The setting is measured from the message arriving to the reply being posted,
    for every answer type. Saying so on the button is cheaper than the support
    question "does this apply to the instant answers too?".
    """
    if human_delay_ms <= 0:
        return "⏳ Delay: Off"
    seconds = human_delay_ms / 1000.0
    pretty = f"{seconds:g}s"
    return f"⏳ Delay: {pretty} (all replies)"


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
    # Ordered by what actually works, not by how the feature is built.
    # 1) Email: Telegram's only delivery channel that third-party apps still get
    #    reliably (SMS was withdrawn in 2023; the in-app code is often withheld).
    # 2) Phone + OTP: fine when Telegram does deliver.
    # 3) QR: Telegram refuses tg://login links opened inside its own app and
    #    requires the in-app camera ("must be scanned from QR-code using in-app
    #    camera" - TDLib internalLinkTypeQrCodeAuthentication), so this route
    #    always needs a second screen. It stays as a last resort.
    b.button(text="📧 Login With Email Code (most reliable)", callback_data="wiz:emailcode")
    b.button(text="📱 Login with Phone + OTP Keypad", callback_data="wiz:login_phone")
    b.button(text="📷 QR Code (needs a second device)", callback_data="wiz:login_qr")
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
    # The route that cannot fail to deliver, next to the ones that can.
    b.button(text="📷 Use QR Login Instead", callback_data="wiz:login_qr")
    # The dead end this fixes: when no code ever arrives, the keypad used to
    # offer only "resend" and "cancel". One tap now switches the *channel* to
    # email, which Telegram still delivers to third-party apps.
    b.button(text="📧 No code coming? Get it by email", callback_data="wiz:emailcode")
    b.button(text="❌ Cancel", callback_data="wiz:login_method")
    b.adjust(3, 3, 3, 3, 1, 2, 1, 1, 1)
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
        text=f"🏁 First Comment Race: {'ON' if group.first_comment_enabled else 'OFF'}",
        callback_data=f"gcfg:{cid}:firstcmt:{back_page}:{mode_flag}",
    )
    b.button(
        text=f"📷 Answer Photo Questions: {'ON' if group.vision_enabled else 'OFF'}",
        callback_data=f"gcfg:{cid}:vision:{back_page}:{mode_flag}",
    )
    b.button(
        text=f"💱 Forex Trading Mode: {'ON' if group.forex_enabled else 'OFF'}",
        callback_data=f"gcfg:{cid}:forex:{back_page}:{mode_flag}",
    )
    if group.forex_enabled:
        b.button(
            text=f"📈 Live Market Prices: {'ON' if group.forex_live_data else 'OFF'}",
            callback_data=f"gcfg:{cid}:fxlive:{back_page}:{mode_flag}",
        )
    b.button(
        text=f"⏱ Cooldown: {group.cooldown_seconds:g}s",
        callback_data=f"gcfg:{cid}:cooldown:{back_page}:{mode_flag}",
    )
    b.button(
        text=_delay_label(group.human_delay_ms),
        callback_data=f"gdelay:menu:{cid}:{back_page}:{mode_flag}",
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


def delay_picker_kb(
    group: GroupConfig, back_page: int, mode_flag: str
) -> InlineKeyboardMarkup:
    """Every delay value on its own button, current one marked.

    Two columns keeps all eleven options on one screen, which a single-column
    list of this length would not: a settings screen that needs scrolling hides
    the option the owner is looking for.
    """
    cid = group.chat_id
    b = InlineKeyboardBuilder()
    for choice in HUMAN_DELAY_CHOICES:
        mark = "✅ " if int(group.human_delay_ms) == choice else ""
        label = "Off" if choice == 0 else f"{choice / 1000:g}s"
        b.button(
            text=f"{mark}{label}",
            callback_data=f"gdelay:{cid}:{choice}:{back_page}:{mode_flag}",
        )
    b.button(
        text="⬅️ Back",
        callback_data=f"grp:cfg:{cid}:{back_page}:{mode_flag}",
    )
    b.adjust(2, 2, 2, 2, 2, 1, 1)
    return b.as_markup()


_SPEED_LABELS = {"fast": "⚡ Fastest", "balanced": "⚖️ Balanced", "smart": "🧠 Smart"}
SPEED_CYCLE = ("fast", "balanced", "smart")


def ai_menu_kb(
    has_fallback: bool, speed_mode: str = "fast", *, exness_ready: bool = False
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Switch Primary Provider", callback_data="ai:pick_primary")
    b.button(text="🧠 Change Primary Model", callback_data="ai:pick_primary_model")
    b.button(
        text=f"🚀 Response Speed: {_SPEED_LABELS.get(speed_mode, speed_mode)}",
        callback_data="ai:speed",
    )
    b.button(
        text="🛡 Change Fallback Provider" if has_fallback else "🛡 Add Fallback Provider",
        callback_data="ai:pick_fallback",
    )
    b.button(text="🔑 Add / Update API Keys", callback_data="ai:manage_keys")
    b.button(
        text="📈 Exness Prices: " + ("CONNECTED" if exness_ready else "NOT CONNECTED"),
        callback_data="exn:menu",
    )
    b.button(text="⚡ Benchmark All Configured Providers", callback_data="ai:benchmark")
    b.button(text="⬅️ Dashboard", callback_data="dash:home")
    b.adjust(1)
    return b.as_markup()


def exness_menu_kb(configured: dict[str, bool]) -> InlineKeyboardMarkup:
    """Credential entry for the Exness Trader API, one field per button."""
    b = InlineKeyboardBuilder()
    b.button(
        text=("✅ " if configured["api_key"] else "⬜ ") + "1. API Key",
        callback_data="exn:set:api_key",
    )
    b.button(
        text=("✅ " if configured["private_key"] else "⬜ ") + "2. Private Signing Key",
        callback_data="exn:set:private_key",
    )
    b.button(
        text=("✅ " if configured["account_id"] else "⬜ ") + "3. Trading Account ID",
        callback_data="exn:set:account_id",
    )
    b.button(
        text=("✅ " if configured["gateway_url"] else "⬜ ") + "4. Gateway Host",
        callback_data="exn:set:gateway_url",
    )
    b.button(text="🔌 Test Connection", callback_data="exn:test")
    b.button(text="⬅️ AI Providers", callback_data="ai:menu")
    b.adjust(1)
    return b.as_markup()


def account_menu_kb(is_connected: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if not is_connected:
        b.button(text="🔌 Reconnect Saved Session", callback_data="acc:reconnect")
    b.button(
        text="☁️ Backup This Deployment (BOT_STATE_B64)",
        callback_data="acc:export_render",
    )
    b.button(
        text="🔑 Get Session String (survives restarts)",
        callback_data="acc:session_string",
    )
    b.button(
        text="🎁 Share Setup With Someone (no credentials)",
        callback_data="acc:export_share",
    )
    b.button(text="🧭 Re-run Full Setup Wizard", callback_data="wiz:restart")
    b.button(text="🚪 Log Out Userbot Account", callback_data="acc:logout_confirm")
    b.button(text="⬅️ Dashboard", callback_data="dash:home")
    b.adjust(1)
    return b.as_markup()


def cancel_to_dashboard_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Back to Dashboard", callback_data="dash:home")
    return b.as_markup()
