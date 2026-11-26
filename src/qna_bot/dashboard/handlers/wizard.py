"""Guided 4-step Setup Wizard triggered on /start (or re-runnable anytime)."""

from __future__ import annotations

import html
import logging
import re

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ...ai.providers import PROVIDERS, fetch_live_models
from ...userbot.login import CodeDelivery, LoginStatus
from ..context import AppContext, safe_delete_message, safe_edit_text
from ..filters import NotACommand
from ..keyboards import (
    groups_list_kb,
    main_dashboard_kb,
    model_picker_kb,
    otp_keypad_kb,
    provider_picker_kb,
    wizard_login_method_kb,
    wizard_welcome_kb,
)
from ..states import AI_MENU_STATES, WizardStates

logger = logging.getLogger(__name__)

router = Router(name="wizard")


def _otp_prompt_text(
    phone: str,
    digits: str,
    note: str | None = None,
    delivery: CodeDelivery | None = None,
) -> str:
    """Render the code-entry prompt, stating exactly where Telegram put the code.

    The old prompt always claimed "sent via Telegram", which left owners who got
    an SMS/email/call (or an in-app confirmation prompt) waiting for a message
    that was never going to arrive.
    """
    if digits:
        masked = " ".join(digits)
    else:
        slots = (delivery.length or 5) if delivery is not None else 5
        masked = " ".join("—" for _ in range(slots))

    if delivery is not None:
        where = f"<b>{delivery.title}</b>\n{delivery.detail}"
        if delivery.length:
            where += f"\n\n🔢 The code has <b>{delivery.length} digits</b>."
    else:
        where = (
            "Telegram accepted the request. Depending on your account the code is delivered as a "
            "<b>service message inside the Telegram app</b>, by <b>SMS</b>, or by <b>call</b>."
        )

    extra = f"\n\n⚠️ {note}" if note else ""
    return (
        "<b>Step 2 / 4 — Enter Telegram Login Code</b>\n\n"
        f"📞 Number: <code>{html.escape(phone)}</code>\n\n"
        f"📨 {where}\n\n"
        f"🔢 <b>Entered code:</b> <code>{masked}</code>\n\n"
        "💡 <i>Tap the keypad buttons below to enter your code.</i>\n"
        "<i>(Type digits spaced like <code>1 2 3 4 5</code> if you prefer.)</i>\n\n"
        "⚡️ <b>No code arriving?</b> Telegram has stopped delivering login codes to "
        "third-party clients for many accounts — the request succeeds and nothing is "
        "sent. Tap <b>“No code coming? Get it by email”</b> below: email is the one "
        "channel Telegram still delivers for third-party apps."
        f"{extra}"
    )


async def _show_wizard_welcome(target: Message | CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    has_env_creds = bool(ctx.env.tg_api_id and ctx.env.tg_api_hash)
    text = (
        "👋 <b>Welcome to Q&amp;A Giveaway Userbot Setup!</b>\n\n"
        "I will walk you through connecting your Telegram account, setting up a fast free AI "
        "provider, and selecting which group chats to monitor.\n\n"
        "<b>What you'll set up (4 quick steps):</b>\n"
        "1️⃣ Telegram API ID &amp; Hash (from <code>my.telegram.org</code>)\n"
        "2️⃣ Connect your Telegram account (secure OTP keypad)\n"
        "3️⃣ Pick a fast, free AI provider (Groq / Gemini / OpenRouter)\n"
        "4️⃣ Select which group chats to answer questions in\n\n"
        "🔒 <i>Sensitive messages (keys, passwords) are auto-deleted and encrypted at rest.</i>"
    )
    # Say the quiet part out loud. Render Free has no persistent disk
    # (https://render.com/docs/free): every redeploy, restart and spin-down erases
    # data/bot.db, which is where the wizard is about to put everything. On that
    # host the wizard can be completed and then silently undone 15 minutes later,
    # so the owner needs the durable channel named before they start, not after
    # their third attempt.
    if ctx.env.public_url and not has_env_creds:
        text += (
            "\n\n⚠️ <b>Before you start — do you see a free host here?</b>\n"
            "On <b>Render Free</b> the disk is <b>wiped</b> on every deploy, restart "
            "and sleep (it sleeps after 15 idle minutes), so anything entered below "
            "is lost when that happens. Set <code>TG_API_ID</code>, "
            "<code>TG_API_HASH</code> and <code>ENCRYPTION_KEY</code> in the host's "
            "<b>Environment</b> tab, or you will be asked for them again and again. "
            "Once logged in, also save <b>BOT_STATE_B64</b> so the session survives."
        )
    kb = wizard_welcome_kb(has_env_creds=has_env_creds, is_connected=ctx.userbot.is_connected)
    if isinstance(target, CallbackQuery):
        await safe_edit_text(target, text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb, disable_web_page_preview=True)


async def _show_ai_step(target: Message | CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    keys = await ctx.db.get_all_provider_keys()
    text = (
        "<b>Step 3 / 4 — Choose Your Fast &amp; Free AI Provider</b>\n\n"
        "Pick which AI provider you want to power your Q&amp;A answers "
        "(you can add a fallback provider and switch anytime later):\n\n"
        "⚡ <b>Groq</b> — Fastest free inference (~200-400ms, best for speed giveaways)\n"
        "🧠 <b>Google Gemini</b> — Very accurate free tier (Flash-Lite / Flash)\n"
        "🌐 <b>OpenRouter</b> — Wide catalog of free models + auto-free router"
    )
    kb = provider_picker_kb("wiz_ai", configured_keys=keys)
    if isinstance(target, CallbackQuery):
        await safe_edit_text(target, text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb, disable_web_page_preview=True)


async def _show_group_step(target: Message | CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    if isinstance(target, CallbackQuery):
        await safe_edit_text(target, "🔄 <b>Step 4 / 4</b> — Syncing your Telegram groups...")
    else:
        await target.answer("🔄 <b>Step 4 / 4</b> — Syncing your Telegram groups...")

    try:
        groups = await ctx.userbot.sync_groups()
    except Exception as exc:
        err_text = f"❌ Failed to fetch your groups: <code>{html.escape(str(exc))}</code>"
        if isinstance(target, CallbackQuery):
            await safe_edit_text(target, err_text)
        else:
            await target.answer(err_text)
        return

    enabled_count = sum(1 for g in groups if g.enabled)
    text = (
        "<b>Step 4 / 4 — Select Group Chats to Monitor</b>\n\n"
        f"Found <b>{len(groups)}</b> groups ({enabled_count} enabled).\n"
        "• Tap a group to toggle <b>✅ ON / ⬜ OFF</b>\n"
        "• Tap <b>⚙️</b> next to any group to customize Admin-only, Answer Style, Cooldown, etc.\n"
        "• By default, groups use <b>👑 Admins Only + ⚡ Giveaway Ultra-Short + ↩️ Reply to Question</b>."
    )
    kb = groups_list_kb(groups, page=0, wizard_mode=True)
    if isinstance(target, CallbackQuery):
        await safe_edit_text(target, text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


# ----------------------------------------------------------------------
# Entry Commands: /start, /setup, /cancel
# ----------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    if await ctx.is_setup_complete():
        global_on = bool(await ctx.db.get_setting("global_enabled", True))
        notify_on = bool(await ctx.db.get_setting("notify_on_answer", False))
        text = await ctx.render_dashboard_text()
        await message.answer(
            text,
            reply_markup=main_dashboard_kb(global_on, notify_on),
            disable_web_page_preview=True,
        )
        return

    await _show_wizard_welcome(message, ctx, state)


@router.message(Command("setup"))
async def cmd_setup(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await _show_wizard_welcome(message, ctx, state)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, ctx: AppContext, state: FSMContext) -> None:
    # Clear the state FIRST and never let anything below undo that. This is the
    # owner's way out of a stuck step: if cancelling the pending login threw, the
    # handler would abort with the state still set and the bot would keep
    # demanding an API key forever. A failed side effect is worth a log line, not
    # a trapped owner.
    await state.clear()
    try:
        await ctx.userbot.cancel_pending_login()
    except Exception as exc:  # noqa: BLE001 - an escape hatch must always open
        logger.warning("Cancelling the pending login failed: %s", exc)
    if await ctx.is_setup_complete():
        global_on = bool(await ctx.db.get_setting("global_enabled", True))
        notify_on = bool(await ctx.db.get_setting("notify_on_answer", False))
        await message.answer(
            "Cancelled. Back to Dashboard:\n\n" + await ctx.render_dashboard_text(),
            reply_markup=main_dashboard_kb(global_on, notify_on),
        )
    else:
        await message.answer("Cancelled. Send /start anytime to resume setup.")


@router.callback_query(F.data == "wiz:restart")
async def cb_wiz_restart(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await callback.answer()
    await _show_wizard_welcome(callback, ctx, state)


# ----------------------------------------------------------------------
# Step 1: API ID & API Hash
# ----------------------------------------------------------------------


@router.callback_query(F.data == "wiz:use_env_creds")
async def cb_use_env_creds(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await callback.answer()
    if not (ctx.env.tg_api_id and ctx.env.tg_api_hash):
        await safe_edit_text(callback, "❌ No TG_API_ID / TG_API_HASH found in .env.")
        return
    await state.update_data(api_id=ctx.env.tg_api_id, api_hash=ctx.env.tg_api_hash)
    await safe_edit_text(
        callback,
        f"✅ Loaded API ID <code>{ctx.env.tg_api_id}</code> from <code>.env</code>.\n\n"
        "<b>Step 2 / 4 — Connect Your Telegram Account</b>\n\n"
        "⚡️ <b>Fastest: Instant Login.</b> One tap on the phone that is already "
        "signed in to Telegram — no SMS, no code to type, and <b>no second phone</b>.\n\n"
        "Choose how you want to log in:",
        reply_markup=wizard_login_method_kb(),
    )


@router.callback_query(F.data == "wiz:step1_creds")
async def cb_step1_creds(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(WizardStates.waiting_api_id)
    await safe_edit_text(
        callback,
        "<b>Step 1 / 4 — Telegram API Credentials</b>\n\n"
        "1. Open <a href='https://my.telegram.org'>my.telegram.org</a> and log in\n"
        "2. Click <b>API development tools</b>\n"
        "3. Copy your numeric <b>App api_id</b>\n\n"
        "📥 Send your <b>API ID</b> (numbers only) now:",
    )


@router.message(NotACommand(), WizardStates.waiting_api_id)
async def msg_api_id(message: Message, ctx: AppContext, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    await safe_delete_message(message)
    if not raw.isdigit():
        await message.answer("❌ API ID must be a number (e.g. <code>12345678</code>). Try again:")
        return
    await state.update_data(api_id=int(raw))
    # Store it immediately rather than waiting for the hash. If the conversation
    # state is lost between these two messages, the ID is still in the database and
    # the owner is only asked for the hash - instead of both, again, forever.
    try:
        await ctx.db.set_setting("tg_api_id", int(raw))
    except Exception as exc:  # noqa: BLE001 - never block the step on storage
        logger.warning("Could not store tg_api_id yet: %s", exc)
    await state.set_state(WizardStates.waiting_api_hash)
    await message.answer(
        f"✅ API ID saved (<code>{raw}</code>).\n\n"
        "📥 Now send your <b>API Hash</b> (32-character hex string):"
    )


@router.message(NotACommand(), WizardStates.waiting_api_hash)
async def msg_api_hash(message: Message, ctx: AppContext, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    await safe_delete_message(message)
    if len(raw) < 16:
        await message.answer("❌ That API Hash looks too short. Please send the full API Hash:")
        return
    await state.update_data(api_hash=raw)
    # Persist them. They used to live only in this conversation's state, so any
    # restart (Render Free sleeps after 15 minutes and redeploys on every push)
    # silently lost them, and the next login failed with "Missing API
    # credentials" - or, worse, on a blueprint deploy that only sets BOT_TOKEN and
    # OWNER_ID, with an API ID from nowhere in particular.
    api_id = (await state.get_data()).get("api_id")
    if api_id:
        try:
            await ctx.db.set_setting("tg_api_id", int(api_id))
            await ctx.db.set_setting("tg_api_hash", raw.strip())
        except Exception as exc:  # noqa: BLE001 - explain, never crash the step
            logger.warning("Could not save API credentials: %s", exc)
            await message.answer(
                "⚠️ I could not save these credentials.\n\n"
                "That normally means <b>ENCRYPTION_KEY</b> changed (or was never "
                "set) on the host, so previously saved secrets cannot be read or "
                "written. Set a fixed ENCRYPTION_KEY in the host's environment, "
                "then send the API Hash again."
            )
            return
    await state.set_state(None)
    await message.answer(
        "✅ API credentials saved!\n\n"
        "<b>Step 2 / 4 — Connect Your Telegram Account</b>\n\n"
        "⚡️ <b>Fastest: Instant Login.</b> One tap on the phone that is already "
        "signed in to Telegram — no SMS, no code to type, and <b>no second phone</b>.\n\n"
        "Choose how you want to log in:",
        reply_markup=wizard_login_method_kb(),
    )


# ----------------------------------------------------------------------
# Step 2: Telegram Account Login (Phone + Inline OTP Keypad or StringSession)
# ----------------------------------------------------------------------


@router.callback_query(F.data == "wiz:login_method")
async def cb_login_method(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    """Show the login options WITHOUT silently killing a live attempt.

    This used to cancel the pending login and clear the state unconditionally.
    Because "Login Options" is the back button on several screens - and every
    failure screen offers it - merely looking at the options destroyed whatever
    the owner had already done, so the next message arrived with nothing waiting
    for it and got "I am not waiting for any input right now".

    Choosing a method is a deliberate act: that button releases any previous
    attempt before starting its own.
    """
    await callback.answer()
    await state.set_state(None)
    await safe_edit_text(
        callback,
        "<b>Step 2 / 4 — Connect Your Telegram Account</b>\n\n"
        "⚡️ <b>Fastest: Instant Login.</b> One tap on the phone that is already "
        "signed in to Telegram — no SMS, no code to type, and <b>no second phone</b>.\n\n"
        "Choose how you want to log in:",
        reply_markup=wizard_login_method_kb(),
    )


@router.callback_query(F.data == "wiz:login_phone")
async def cb_login_phone(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await callback.answer()
    # Starting the phone flow over: drop any previous attempt so its temporary
    # client does not linger and hold an old phone_code_hash.
    await ctx.userbot.cancel_pending_login()
    await state.set_state(WizardStates.waiting_phone)
    await safe_edit_text(
        callback,
        "<b>Step 2 / 4 — Phone Number</b>\n\n"
        "📥 Send your Telegram phone number in <b>international format</b> "
        "(country code, <u>no</u> leading 0):\n"
        "• ✅ <code>+251912345678</code>\n"
        "• ✅ <code>+15551234567</code>\n"
        "• ❌ <code>0912345678</code> (missing country code)\n\n"
        "ℹ️ <i>Telegram usually delivers the login code as a message inside the "
        "Telegram app for this number, not by SMS. I will tell you exactly where "
        "it went after you send the number.</i>",
    )


#: Dialling codes that make a bare digit string a phone number rather than an
#: API ID. Deliberately a *positive* list instead of "7-15 digits", because an API
#: ID is 6-9 digits and any length-based heuristic confuses the two.
_CALL_PREFIXES = (
    "1", "20", "27", "30", "31", "32", "33", "34", "36", "39", "40", "41", "43",
    "44", "45", "46", "47", "48", "49", "51", "52", "53", "54", "55", "56", "57",
    "58", "60", "61", "62", "63", "64", "65", "66", "81", "82", "84", "86", "90",
    "91", "92", "93", "94", "95", "98", "212", "213", "216", "218", "220", "221",
    "233", "234", "249", "251", "252", "254", "255", "256", "257", "258", "260",
    "261", "263", "264", "265", "266", "267", "268", "269", "351", "352", "353",
    "358", "380", "381", "385", "420", "421", "503", "504", "505", "506", "507",
    "509", "549", "591", "593", "595", "598", "601", "628", "673", "855", "856",
    "880", "886", "960", "961", "962", "963", "964", "965", "966", "967", "968",
    "971", "972", "973", "974", "975", "977", "992", "993", "994", "995", "998",
)


def _looks_like_a_phone_number(text: str) -> bool:
    """True only for something that is unmistakably a dialable number.

    Used by the catch-all to recover a lost login step. It must never claim an
    API ID (6-9 digits, no dialling code) or an API Hash (32 hex characters,
    which can be all digits).
    """
    raw = (text or "").strip()
    if not raw:
        return False
    # An API Hash is 32 hex characters and can be entirely numeric-ish; it must
    # never be mistaken for a number. Likewise require the input to *look* like
    # phone input at all, so a hex string with letters is rejected before the
    # digits are stripped out of it.
    if re.fullmatch(r"[0-9a-fA-F]{32}", raw):
        return False
    if not re.fullmatch(r"(?i)(tel:)?[\d\s+\-().]+", raw):
        return False
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("+"):
        # An explicit international prefix is unambiguous, and an API ID is never
        # written that way. Allow a short one through so msg_phone can say it is
        # too short, which is more useful than "I am not waiting for input".
        return bool(re.fullmatch(r"\+\d{7,15}", digits))
    if digits.startswith("00"):
        digits = digits[2:]
    if not digits.isdigit():
        return False
    if digits.startswith("0"):
        # A leading 0 is a national trunk prefix - never an API ID. Let it reach
        # the phone step so msg_phone can explain that the country code is
        # missing, which is far more useful than "I am not waiting for input".
        return len(digits) >= 10
    # A bare number needs a real dialling code and a plausible length: >= 10
    # digits excludes every API ID, and <= 13 excludes arbitrary digit runs.
    if not (10 <= len(digits) <= 13):
        return False
    return any(digits.startswith(prefix) for prefix in _CALL_PREFIXES)


@router.message(NotACommand(), WizardStates.waiting_phone)
async def msg_phone(message: Message, ctx: AppContext, state: FSMContext) -> None:
    phone = (message.text or "").strip()
    await safe_delete_message(message)
    data = await state.get_data()
    api_id = data.get("api_id") or ctx.env.tg_api_id or await ctx.db.get_setting("tg_api_id")
    api_hash = data.get("api_hash") or ctx.env.tg_api_hash or await ctx.db.get_setting("tg_api_hash")

    if not (api_id and api_hash):
        # Distinguish "never entered" from "was entered, but cannot be decrypted".
        # The second one used to surface as a bare "Missing API credentials", which
        # sends the owner round the same loop forever.
        lost = bool(getattr(ctx.db, "undecryptable", set()) & {"tg_api_id", "tg_api_hash"})
        if lost:
            await message.answer(
                "🔐 <b>Your saved API credentials can no longer be read.</b>\n\n"
                "They are stored encrypted, and the encryption key on this host "
                "changed — usually because <code>ENCRYPTION_KEY</code> was left blank "
                "in the blueprint deploy, so the bot generated a new one on each "
                "restart.\n\n"
                "<b>Best fix:</b> add a fixed <code>ENCRYPTION_KEY</code> in the host's "
                "Environment tab (I can give you the exact value), then send "
                "<code>/setup</code> and enter the API ID and Hash again.\n\n"
                "Until then every login attempt will fail here, so this has to come "
                "first."
            )
            return
        await message.answer("❌ Missing API credentials. Send /setup to start Step 1 again.")
        return

    status_msg = await message.answer("📡 Requesting login code from Telegram...")
    res = await ctx.userbot.start_phone_login(int(api_id), str(api_hash), phone)

    if ctx.userbot.email_setup_required:
        # Telegram point-blank refused to send anything until a login email exists.
        # Showing "code sent" here (or just text) was the original dead end.
        await state.update_data(phone=ctx.userbot.pending_phone or phone)
        await state.set_state(WizardStates.waiting_login_email)
        await status_msg.edit_text(
            "🚧 <b>Telegram is blocking the code for this account</b>\n\n"
            "Telegram will not deliver <i>any</i> login code until a <b>login email</b> is "
            "set up for this number. Until that is done, no SMS, service message or call "
            "will ever arrive — no matter how many times you tap Resend.\n\n"
            "✅ <b>I can set it up right here.</b>\n\n"
            "📥 Send the <b>email address</b> you want to use "
            "(e.g. <code>you@gmail.com</code>).\n"
            "<i>Telegram will email a short verification code, and then the normal login "
            "code is delivered immediately.</i>\n\n"
            "— or —",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📷 Use QR Login Instead", callback_data="wiz:login_qr")],
                    [InlineKeyboardButton(text="⬅️ Login Options", callback_data="wiz:login_method")],
                ]
            ),
        )
        return

    if res.status is not LoginStatus.CODE_SENT:
        # Show Telegram's real reason (flood wait, bad number, bad API hash, ...)
        # and offer a retry button instead of dumping the user back to Step 1.
        cancel_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔁 Try Again", callback_data="wiz:login_phone")],
                [InlineKeyboardButton(text="📷 Use QR Login Instead", callback_data="wiz:login_qr")],
                [InlineKeyboardButton(text="⬅️ Login Options", callback_data="wiz:login_method")],
            ]
        )
        await status_msg.edit_text(
            f"{res.message}\n\n"
            "<i>Tap 🔁 Try Again to re-send your number, or use QR login — it needs no "
            "code at all.</i>",
            reply_markup=cancel_kb,
        )
        return

    delivery = res.delivery or ctx.userbot.last_code_delivery
    # Show the normalised E.164 number Telegram actually used (e.g. "+2519...").
    display_phone = ctx.userbot.pending_phone or phone
    await state.update_data(phone=display_phone, otp_digits="")
    await state.set_state(WizardStates.waiting_otp)
    await status_msg.edit_text(
        _otp_prompt_text(display_phone, "", delivery=delivery),
        reply_markup=otp_keypad_kb(""),
    )


# ----------------------------------------------------------------------
# Step 2b: login-email setup (unblocks SentCodeTypeSetUpEmailRequired)
# ----------------------------------------------------------------------


@router.message(NotACommand(), WizardStates.waiting_login_email)
async def msg_login_email(message: Message, ctx: AppContext, state: FSMContext) -> None:
    email = (message.text or "").strip()
    await safe_delete_message(message)

    wait = await message.answer("📧 Asking Telegram to email you a verification code...")
    res = await ctx.userbot.submit_login_email(email)
    if res.status is not LoginStatus.CODE_SENT:
        await wait.edit_text(f"{res.message}\n\nSend the email address again:")
        return

    await state.update_data(login_email=email)
    await state.set_state(WizardStates.waiting_login_email_code)
    await wait.edit_text(
        "📬 <b>Check your email inbox</b>\n\n"
        f"{res.message}\n\n"
        "📥 Send the code from that email now.\n"
        "<i>(Check spam/junk if you do not see it. Your message is deleted after reading.)</i>",
    )


@router.message(NotACommand(), WizardStates.waiting_login_email_code)
async def msg_login_email_code(message: Message, ctx: AppContext, state: FSMContext) -> None:
    code = (message.text or "").strip()
    await safe_delete_message(message)

    wait = await message.answer("✅ Verifying your email with Telegram...")
    res = await ctx.userbot.submit_login_email_code(code)

    if res.status is LoginStatus.CODE_SENT:
        # Email verified — Telegram has now actually delivered the login code.
        delivery = res.delivery or ctx.userbot.last_code_delivery
        await state.update_data(otp_digits="")
        await state.set_state(WizardStates.waiting_otp)
        await wait.edit_text(
            _otp_prompt_text(ctx.userbot.pending_phone or "", "", delivery=delivery),
            reply_markup=otp_keypad_kb(""),
        )
        return

    if res.status is LoginStatus.TWO_FA_REQUIRED:
        await state.set_state(WizardStates.waiting_2fa)
        await wait.edit_text(
            "🔐 <b>Two-Step Verification (2FA) Required</b>\n\nSend your Telegram cloud password:"
        )
        return

    await wait.edit_text(f"{res.message}\n\nSend the email code again, or tap Cancel.")


# ----------------------------------------------------------------------
# Step 2b-bis: get the login code by EMAIL (the channel that delivers)
# ----------------------------------------------------------------------


@router.callback_query(F.data == "wiz:emailcode")
async def cb_email_code(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    """Ask for an inbox and route the login code there.

    Why this is the answer rather than another retry: Telegram has stopped
    delivering login codes to third-party clients over SMS (Feb 2023) and now
    silently withholds the in-app code for many accounts. Email is an official
    delivery channel for third-party apps, and Telegram's own mail servers send
    it, so nothing depends on an app session or an SMS gateway.
    """
    await callback.answer()
    await state.set_state(WizardStates.waiting_email_login_address)
    await safe_edit_text(
        callback,
        "📧 <b>Get the login code by email</b>\n\n"
        "Telegram will send the login code to an <b>email address</b> instead of the "
        "Telegram app or SMS. This works for third-party apps, which is why it "
        "actually arrives.\n\n"
        "📥 Send the email address to use (e.g. <code>you@gmail.com</code>).\n"
        "<i>It becomes this account's Telegram login email. Your message is deleted "
        "immediately after it is read.</i>",
    )


@router.message(NotACommand(), WizardStates.waiting_email_login_address)
async def msg_email_login_address(message: Message, ctx: AppContext, state: FSMContext) -> None:
    address = (message.text or "").strip()
    await safe_delete_message(message)

    wait = await message.answer("📧 Asking Telegram to send the login code there...")
    res = await ctx.userbot.use_login_email(address)
    if res.status is not LoginStatus.CODE_SENT:
        await wait.edit_text(
            f"{res.message}\n\nSend the email address again, or tap ⬅️ Login Options.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Login Options", callback_data="wiz:login_method")]
                ]
            ),
        )
        return

    await state.update_data(login_email=address, otp_digits="")
    await state.set_state(WizardStates.waiting_email_login_code)
    await wait.edit_text(
        f"{res.message}\n\n"
        "📥 Send the code now.\n"
        "<i>(Check spam/junk. Your message is deleted after reading.)</i>",
    )


@router.message(NotACommand(), WizardStates.waiting_email_login_code)
async def msg_email_login_code(message: Message, ctx: AppContext, state: FSMContext) -> None:
    code = (message.text or "").strip()
    await safe_delete_message(message)

    wait = await message.answer("✅ Verifying the emailed code...")
    res = await ctx.userbot.submit_email_login_code(code)

    if res.status is LoginStatus.OK:
        await state.clear()
        await wait.edit_text(f"🎉 <b>{html.escape(res.message)}</b>")
        await _show_ai_step(wait, ctx, state)
        return

    if res.status is LoginStatus.TWO_FA_REQUIRED:
        await state.set_state(WizardStates.waiting_2fa)
        await wait.edit_text(
            "🔐 <b>Two-Step Verification (2FA) Required</b>\n\n"
            "The email code was accepted — your account also has a Telegram "
            "<b>cloud password</b>.\n\n📥 Send that 2FA password now."
        )
        return

    if res.status is LoginStatus.CODE_SENT:
        # Email verified: Telegram has now dispatched the real login code.
        delivery = res.delivery or ctx.userbot.last_code_delivery
        await state.update_data(otp_digits="")
        await state.set_state(WizardStates.waiting_otp)
        await wait.edit_text(
            _otp_prompt_text(ctx.userbot.pending_phone or "", "", delivery=delivery),
            reply_markup=otp_keypad_kb(""),
        )
        return

    await wait.edit_text(f"{res.message}\n\nSend the code again, or tap ⬅️ Login Options.")


# ----------------------------------------------------------------------
# Step 2c: QR-code login (no code delivery involved)
# ----------------------------------------------------------------------


def _qr_kb(url: str | None = None, page_url: str | None = None) -> InlineKeyboardMarkup:
    """Keyboard for the camera/QR screen.

    ``tg://login?token=...`` is intentionally **not** used as a ``url`` button
    here. Telegram's deep-link documentation says these links must only be
    handled when opened from *outside* the app and explicitly not when clicked
    inside a message or button - a bot button is exactly that, so the tap did
    nothing. The token is rendered as a QR image for another device instead, and
    the one-phone route is the browser page (``page_url``), which is outside the
    app and therefore legal.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if page_url:
        rows.append(
            [InlineKeyboardButton(text="🌐 Log In On This Phone (no camera)", url=page_url)]
        )
    rows += [
        [InlineKeyboardButton(text="🔄 I Approved — Continue", callback_data="wiz:qr_check")],
        [InlineKeyboardButton(text="🌐 Send Me the Login Link", callback_data="wiz:loginlink")],
        [InlineKeyboardButton(text="📷 Make a QR Code", callback_data="wiz:qr_image")],
        [InlineKeyboardButton(text="♻️ Refresh QR Image", callback_data="wiz:qr_refresh")],
        [InlineKeyboardButton(text="🔁 Start Over", callback_data="wiz:login_qr")],
        [InlineKeyboardButton(text="⬅️ Login Options", callback_data="wiz:login_method")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _qr_caption(
    url: str,
    snapshot: dict | None = None,
    page_url: str | None = None,
) -> str:
    """Caption for the QR image, explaining that it renews itself."""
    renewed = ""
    if snapshot and snapshot.get("renewals"):
        renewed = (
            f"\n♻️ <i>Auto-renewed {snapshot['renewals']}× — the code stays valid while "
            "this screen is open.</i>"
        )
    return (
        "📷 <b>Scan this code with another device</b>\n\n"
        "On the device that is <b>already signed in to Telegram</b>: "
        "<b>Settings → Devices → Link Desktop Device</b>, then point its camera at "
        "the code above.\n\n"
        "⚠️ <b>This route needs a camera and a second screen — it cannot be done from "
        "one phone.</b> Telegram will not let an app approve a login from a link: "
        "opening one inside Telegram only shows <i>“go to Settings → Devices → Scan "
        "QR”</i>. That restriction is deliberate, so there is no bypass.\n\n"
        "📧 <b>One phone only?</b> Use <b>Login With Email Code</b> instead — the code "
        "arrives in an inbox and needs no second device.\n\n"
        "♾ The code renews itself, so there is no rush — tokens live ~30s but a fresh "
        "one is issued the moment the old lapses. If a device says “code expired”, tap "
        "♻️ Refresh QR Image."
        f"{renewed}"
    )


@router.callback_query(F.data == "wiz:login_qr")
async def cb_login_qr(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    """Start the no-code login.

    The browser page is preferred because it works with one phone and needs no
    code delivery. The QR image is only rendered when the user explicitly asks
    for it (``wiz:qr_image``) or when the deployment has no public URL: two live
    Telegram connections for one login would just double the auth keys and the
    chances of a flood wait.
    """
    await callback.answer("Starting login...")
    api_id, api_hash = await _resolve_api_creds(ctx, state)
    if not (api_id and api_hash):
        await safe_edit_text(callback, "❌ Missing API credentials. Send /setup to restart.")
        return

    await safe_edit_text(callback, "🔑 Starting a login that needs no code...")
    page_url = await _start_browser_login(ctx, int(api_id), str(api_hash))
    if page_url is None:
        # No public URL (local run, or PORT unset): fall back to the QR image.
        await _show_qr_image(callback, ctx, state, int(api_id), str(api_hash))
        return

    await state.set_state(WizardStates.waiting_qr_2fa)
    await state.update_data(qr_generation=1)
    await safe_edit_text(
        callback,
        "📷 <b>QR Login</b>\n\n"
        "This route needs a <b>second device</b> with a camera, because Telegram "
        "refuses to approve a login from a link opened inside its own app.\n\n"
        "🌐 <b>No second device?</b> The button below opens the same token as a web "
        "page, which is the one place Telegram will accept it from — but if your phone "
        "still answers with <i>“go to Settings → Devices → Scan QR”</i>, use "
        "<b>📧 Login With Email Code</b> instead. That one needs no camera at all.\n\n"
        "Then tap <b>🔄 I Approved — Continue</b> here.",
        reply_markup=_qr_kb(None, page_url),
    )


@router.callback_query(F.data == "wiz:qr_image")
async def cb_qr_image(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    """Render the QR image for a second device (explicit opt-in)."""
    await callback.answer("Generating QR code...")
    api_id, api_hash = await _resolve_api_creds(ctx, state)
    if not (api_id and api_hash):
        await safe_edit_text(callback, "❌ Missing API credentials. Send /setup to restart.")
        return
    await _show_qr_image(callback, ctx, state, int(api_id), str(api_hash))


async def _show_qr_image(
    callback: CallbackQuery,
    ctx: AppContext,
    state: FSMContext,
    api_id: int,
    api_hash: str,
) -> None:
    """Start a QR login session and post the code as an image."""
    if callback.message is None:
        return
    await safe_edit_text(callback, "📷 Generating a QR login code...")
    res, url = await ctx.userbot.start_qr_login(api_id, api_hash)
    if url is None:
        await safe_edit_text(callback, f"{res.message}", reply_markup=_qr_kb())
        return

    await state.set_state(WizardStates.waiting_qr_2fa)
    await state.update_data(qr_generation=1)

    manager = ctx.browser_login
    page_url = (
        manager.page_url(ctx.env.public_url) if manager and ctx.env.public_url else None
    )
    caption = _qr_caption(url, page_url=page_url)
    png = ctx.userbot.qr_code()
    if png is not None:
        from aiogram.types import BufferedInputFile

        await callback.message.answer_photo(
            BufferedInputFile(png, filename="login_qr.png"),
            caption=caption,
            reply_markup=_qr_kb(url, page_url),
        )
    else:
        await safe_edit_text(callback, caption, reply_markup=_qr_kb(url, page_url))


async def _resolve_api_creds(ctx: AppContext, state: FSMContext) -> tuple[object, object]:
    """API id/hash for a login attempt: wizard state, then env, then the database."""
    data = await state.get_data()
    api_id = data.get("api_id") or ctx.env.tg_api_id or await ctx.db.get_setting("tg_api_id")
    api_hash = (
        data.get("api_hash") or ctx.env.tg_api_hash or await ctx.db.get_setting("tg_api_hash")
    )
    return api_id, api_hash


async def _start_browser_login(ctx: AppContext, api_id: int, api_hash: str) -> str | None:
    """Start the browser-based login and return the page URL for this owner.

    Returns ``None`` (without raising) when the deployment has no public URL -
    which is the normal case for a local run with no PORT. The wizard then simply
    offers the QR image instead, so nothing breaks.
    """
    manager = ctx.browser_login
    base = ctx.env.public_url
    if manager is None or not base:
        return None
    res, _url = await manager.start(api_id, api_hash)
    if res.status is LoginStatus.ERROR:
        return None
    return manager.page_url(base)


async def _browser_login_result(ctx: AppContext, state: FSMContext) -> tuple[LoginStatus, str]:
    """Poll the browser attempt and report it in the shape the QR screen expects."""
    manager = ctx.browser_login
    if manager is None:
        return LoginStatus.ERROR, "Browser login is not available on this deployment."
    attempt = manager.current
    if attempt is None:
        return LoginStatus.ERROR, "No login in progress."
    if attempt.status is LoginStatus.OK:
        res = await ctx.userbot.adopt_browser_login(manager)
        if res.status is LoginStatus.OK:
            return LoginStatus.OK, res.message
        return LoginStatus.ERROR, res.message
    if attempt.status is LoginStatus.TWO_FA_REQUIRED:
        return LoginStatus.TWO_FA_REQUIRED, attempt.message
    if attempt.status is LoginStatus.ERROR:
        return LoginStatus.ERROR, attempt.message
    return attempt.status, (
        f"Waiting for the approval. The page stays valid on its own "
        f"(~{attempt.seconds_left}s on this link, then it renews)."
    )


@router.callback_query(F.data == "wiz:loginlink")
async def cb_refresh_login_link(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    """Re-send the browser login page link for the current attempt.

    The page URL contains a per-attempt capability, so re-posting it is only safe
    inside the owner's private dashboard chat - which is where this runs.
    """
    manager = ctx.browser_login
    page_url = manager.page_url(ctx.env.public_url) if manager and ctx.env.public_url else None
    if page_url is None:
        api_id, api_hash = await _resolve_api_creds(ctx, state)
        if not (api_id and api_hash):
            await callback.answer("Missing API credentials. Send /setup.", show_alert=True)
            return
        page_url = await _start_browser_login(ctx, int(api_id), str(api_hash))
    if page_url is None:
        await callback.answer("No public URL on this deployment — use the QR code.", show_alert=True)
        return

    await safe_edit_text(
        callback,
        "🌐 <b>This-phone login link</b>\n\n"
        "Open it in the browser on the phone that already has Telegram. Tap "
        "<b>Open Telegram &amp; Approve</b>, then <b>Approve</b> in Telegram.\n\n"
        "<i>The link renews the token by itself while the page is open, so leave it "
        "open as long as you like.</i>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🌐 Open the login page", url=str(page_url))],
                [
                    InlineKeyboardButton(
                        text="🔄 I Approved — Continue", callback_data="wiz:qr_check"
                    )
                ],
            ]
        ),
    )
    await callback.answer("Link ready")


@router.callback_query(F.data == "wiz:qr_refresh")
async def cb_qr_refresh(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    """Swap the on-screen QR for the current (auto-renewed) token."""
    snapshot = ctx.userbot.qr_snapshot()
    if snapshot is None:
        await callback.answer("No QR login in progress — tap 🔁 Start Over.", show_alert=True)
        return
    if snapshot["status"] is LoginStatus.ERROR:
        await callback.answer(str(snapshot["message"])[:190], show_alert=True)
        return

    png = ctx.userbot.qr_code()
    if png is None or callback.message is None:
        await callback.answer("Could not render the QR image.", show_alert=True)
        return

    from aiogram.types import BufferedInputFile, InputMediaPhoto

    # The renewed token has its own deep link, so anything showing it must be
    # rebuilt from the NEW url or it would hand Telegram an expired token.
    current_url = str(snapshot["url"])
    manager = ctx.browser_login
    page_url = manager.page_url(ctx.env.public_url) if manager and ctx.env.public_url else None
    if page_url is None:
        api_id, api_hash = await _resolve_api_creds(ctx, state)
        if api_id and api_hash:
            page_url = await _start_browser_login(ctx, int(api_id), str(api_hash))
    caption = _qr_caption(current_url, snapshot, page_url)
    try:
        # Replace the photo in place so the chat does not fill with images.
        await callback.message.edit_media(
            media=InputMediaPhoto(
                media=BufferedInputFile(png, filename="login_qr.png"),
                caption=caption,
            ),
            reply_markup=_qr_kb(current_url, page_url),
        )
        await callback.answer("QR refreshed")
    except Exception:
        # edit_media can fail on some clients; fall back to a new message.
        await callback.message.answer_photo(
            BufferedInputFile(png, filename="login_qr.png"),
            caption=caption,
            reply_markup=_qr_kb(current_url, page_url),
        )
        await callback.answer("QR refreshed")


@router.callback_query(F.data == "wiz:qr_check")
async def cb_qr_check(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    status, message = ctx.userbot.qr_login_status()
    # Tracks whether the OK came from the browser page, which finalises the
    # session itself, or from the QR scan, which still needs finishing here.
    finalised = False
    if status is not LoginStatus.OK:
        # The browser page drives a *separate* Telethon connection, so the QR
        # session knows nothing about an approval that happened there.
        browser_status, browser_message = await _browser_login_result(ctx, state)
        if browser_status is LoginStatus.OK:
            status, message, finalised = browser_status, browser_message, True
        elif browser_status is LoginStatus.TWO_FA_REQUIRED and status is not LoginStatus.ERROR:
            status, message = browser_status, browser_message
        elif status is LoginStatus.ERROR and browser_status is not LoginStatus.ERROR:
            status, message = browser_status, browser_message

    # Check 2FA *before* finalising: otherwise the "2FA required" state is
    # reported as a generic error and the owner never gets to type their password.
    if status is LoginStatus.TWO_FA_REQUIRED:
        await callback.answer("QR approved — 2FA needed")
        await state.set_state(WizardStates.waiting_qr_2fa)
        await safe_edit_text(
            callback,
            "🔐 <b>QR approved — 2FA Required</b>\n\nThis account has a Telegram "
            "cloud password. Send it now:\n"
            "<i>(Your message is deleted immediately after it is read.)</i>",
        )
        return

    if status is LoginStatus.ERROR:
        await callback.answer(message[:190], show_alert=True)
        return

    if status is not LoginStatus.OK:
        await callback.answer(message[:190] or "Waiting for approval...", show_alert=True)
        return

    if not finalised:
        res = await ctx.userbot.finish_qr_login_if_scanned()
        if res is None or res.status is not LoginStatus.OK:
            await callback.answer(
                (res.message if res is not None else "Login session was lost.")[:190],
                show_alert=True,
            )
            return
        message = res.message

    await callback.answer("Logged in!")
    await state.clear()
    if callback.message is not None:
        await safe_edit_text(callback, f"🎉 <b>{html.escape(message)}</b>")
        await _show_ai_step(callback.message, ctx, state)


@router.message(NotACommand(), WizardStates.waiting_qr_2fa)
async def msg_qr_2fa(message: Message, ctx: AppContext, state: FSMContext) -> None:
    password = message.text or ""
    await safe_delete_message(message)

    manager = ctx.browser_login
    attempt = manager.current if manager is not None else None
    if attempt is not None and attempt.needs_2fa:
        res = await manager.submit_password(password)
        if res.status is LoginStatus.OK:
            adopt = await ctx.userbot.adopt_browser_login(manager)
            if adopt.status is not LoginStatus.OK:
                await message.answer(f"{adopt.message}\n\nTry the 2FA password again:")
                return
            await state.clear()
            await message.answer(f"🎉 <b>{html.escape(adopt.message)}</b>")
            await _show_ai_step(message, ctx, state)
            return
        if res.status is LoginStatus.TWO_FA_REQUIRED:
            await message.answer(f"{res.message}\n\nTry the 2FA password again:")
            return
        await message.answer(f"❌ {res.message}\n\nTry the 2FA password again.")
        return

    status, info = ctx.userbot.qr_login_status()
    if status is LoginStatus.OK:
        res = await ctx.userbot.finish_qr_login_if_scanned()
        if res is not None and res.status is LoginStatus.OK:
            await state.clear()
            await message.answer(f"🎉 <b>{html.escape(res.message)}</b>")
            await _show_ai_step(message, ctx, state)
            return
    if status is not LoginStatus.TWO_FA_REQUIRED:
        await message.answer(
            f"⏳ {html.escape(info)}\n\nApprove the login first, then send your 2FA password."
        )
        return

    res = await ctx.userbot.submit_qr_2fa(password)
    if res.status is not LoginStatus.OK:
        await message.answer(f"{res.message}\n\nTry the 2FA password again:")
        return

    await state.clear()
    await message.answer(f"🎉 <b>{html.escape(res.message)}</b>")
    await _show_ai_step(message, ctx, state)


async def _show_no_code_help(
    callback: CallbackQuery,
    ctx: AppContext,
    state: FSMContext,
) -> None:
    """Troubleshooting checklist for "I did not get any Telegram code".

    This is the exact complaint the wizard used to be unable to answer: it kept
    saying "code sent via Telegram" while Telegram had actually delivered it
    somewhere else (or blocked delivery entirely).
    """
    data = await state.get_data()
    phone = str(data.get("phone", "")) or (ctx.userbot.pending_phone or "")
    delivery = ctx.userbot.last_code_delivery

    if delivery is not None:
        where = f"📨 <b>Telegram says the code went to:</b> {delivery.title}\n{delivery.detail}"
    else:
        where = (
            "ℹ️ No login request is active right now. Send your phone number again "
            "first (or tap ⬅️ Login Options)."
        )

    text = (
        "❓ <b>No code received</b>\n\n"
        "📧 <b>Fastest fix: change the channel.</b> Tap "
        "<b>📧 Get the code by email</b> below. Telegram sends the login code to an "
        "inbox instead of the app or SMS, which is the one delivery route it still "
        "gives third-party apps. Everything below is about the code you are waiting "
        "for.\n\n"
        f"📞 Number used: <code>{html.escape(phone) or '—'}</code>\n\n"
        f"{where}\n\n"
        "<b>1. Check inside Telegram first (most common)</b>\n"
        "Telegram sends login codes as a <b>service message from “Telegram”</b> "
        "(blue shield ✓) at the very top of your chat list — <b>not</b> by SMS. "
        "Open the Telegram app on the device that already has this number and look for it, "
        "or for a “Confirm login” / “Yes, it's me” button.\n\n"
        "<b>2. Check SMS and email</b>\n"
        "Look in your SMS app (including spam/blocked) and your login email inbox.\n\n"
        "<b>3. Make sure the number is right</b>\n"
        "Use international format with the country code and <u>no</u> leading 0: "
        "<code>+251912345678</code>.\n\n"
        "<b>4. Try another delivery channel</b>\n"
        "Tap 🔄 <b>Resend Code</b> — Telegram then switches channel "
        "(app message → SMS → call). Telegram may also offer a voice call.\n\n"
        "<b>5. Wait out the rate limit</b>\n"
        "Repeated requests trigger Telegram's flood limit, after which <b>no</b> code is "
        "sent for a while. If you see a wait time, stop retrying for that long.\n\n"
        "<b>6. Match the API ID to the account</b>\n"
        "A code is sent to whichever number you typed, but Telegram ties the request to "
        "the <b>API ID / API Hash</b> from Step 1. If those came from a different "
        "<a href='https://my.telegram.org'>my.telegram.org</a> app than the one this "
        "number has used before, the code can be silently dropped. Log in to "
        "my.telegram.org <b>as the account being connected</b>, open API development "
        "tools, and re-enter both values.\n\n"
        "<b>7. A fresh server never gets the code</b>\n"
        "Telegram limits first logins from new IPs, and a brand-new host is exactly "
        "that. Two things make an enormous difference: log in to Telegram <b>on your "
        "phone first</b> and tap “Confirm login” / “Yes, it's me” when the prompt "
        "appears, and prefer <b>QR login</b> (⬅️ Login Options → Use QR Login), which "
        "needs no code at all and is the one route a new server cannot break.\n\n"
        "<b>8. Check the server can still reach Telegram</b>\n"
        "On Render Free Tier the service sleeps after 15 minutes idle. If the request "
        "timed out, the code may never have been sent — use an uptime monitor on "
        "<code>/health</code>, then try again."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            # The no-code route first: it is the answer to "no code received".
            [InlineKeyboardButton(text="📧 Get the code by email", callback_data="wiz:emailcode")],
            [InlineKeyboardButton(text="📷 Use QR Login (no code needed)", callback_data="wiz:login_qr")],
            [InlineKeyboardButton(text="🔄 Resend Code", callback_data="otp:resend")],
            [
                InlineKeyboardButton(
                    text="🔁 Change Phone Number", callback_data="wiz:login_phone"
                ),
                InlineKeyboardButton(text="⬅️ Login Options", callback_data="wiz:login_method"),
            ],
        ]
    )
    await safe_edit_text(callback, text, reply_markup=kb)


@router.callback_query(F.data.startswith("otp:"))
async def cb_otp_keypad(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    data = await state.get_data()
    digits = str(data.get("otp_digits", ""))
    phone = str(data.get("phone", "")) or (ctx.userbot.pending_phone or "")
    delivery = ctx.userbot.last_code_delivery
    max_digits = delivery.length if (delivery and delivery.length) else 7

    async def _rerender(note: str | None = None) -> None:
        await safe_edit_text(
            callback,
            _otp_prompt_text(phone, digits, note=note, delivery=delivery),
            reply_markup=otp_keypad_kb(digits),
        )

    if action == "d" and len(parts) == 3:
        if len(digits) >= max_digits:
            await callback.answer(
                f"Code length reached ({max_digits} digits). Tap ✅ Submit.",
                show_alert=True,
            )
            return
        digits += parts[2]
        await state.update_data(otp_digits=digits)
        await callback.answer()
        await _rerender()
        return

    if action == "back":
        digits = digits[:-1]
        await state.update_data(otp_digits=digits)
        await callback.answer()
        await _rerender()
        return

    if action == "clear":
        await state.update_data(otp_digits="")
        digits = ""
        await callback.answer("Cleared")
        await _rerender()
        return

    if action == "help":
        await callback.answer()
        await _show_no_code_help(callback, ctx, state)
        return

    if action == "resend":
        await safe_edit_text(
            callback,
            _otp_prompt_text(phone, "", note="Requesting a fresh code from Telegram...", delivery=delivery),
            reply_markup=otp_keypad_kb(""),
        )
        await callback.answer("Requesting new code...")
        res = await ctx.userbot.resend_phone_code()
        await state.update_data(otp_digits="")
        if res.status is LoginStatus.CODE_SENT:
            delivery = res.delivery or delivery
            await safe_edit_text(
                callback,
                _otp_prompt_text(phone, "", delivery=delivery),
                reply_markup=otp_keypad_kb(""),
            )
        else:
            # Resend failed (flood wait, dead attempt, ...): explain and let the
            # owner start the phone step over without hunting for /start.
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔁 Re-enter Phone Number", callback_data="wiz:login_phone")],
                    [InlineKeyboardButton(text="⬅️ Login Options", callback_data="wiz:login_method")],
                ]
            )
            await safe_edit_text(callback, f"{res.message}", reply_markup=kb)
        return

    if action == "submit":
        if not digits:
            await callback.answer("Enter the code first!", show_alert=True)
            return
        await callback.answer("Verifying code...")
        await _process_otp_submission(callback, ctx, state, digits)


@router.message(NotACommand(), WizardStates.waiting_otp)
async def msg_otp_fallback(message: Message, ctx: AppContext, state: FSMContext) -> None:
    """Allow spaced text fallback like '1 2 3 4 5' if the user types instead of using keypad."""
    raw = (message.text or "").strip()
    await safe_delete_message(message)
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        await message.answer(
            "⚠️ Please tap the keypad buttons on the message above, or type the digits "
            "separated by spaces (e.g. <code>1 2 3 4 5</code>)."
        )
        return
    await _process_otp_submission(message, ctx, state, digits)


async def _process_otp_submission(
    target: Message | CallbackQuery,
    ctx: AppContext,
    state: FSMContext,
    digits: str,
) -> None:
    res = await ctx.userbot.submit_otp_code(digits)
    data = await state.get_data()
    phone = str(data.get("phone", "")) or (ctx.userbot.pending_phone or "")
    delivery = ctx.userbot.last_code_delivery

    if res.status is LoginStatus.OK:
        await state.clear()
        if isinstance(target, CallbackQuery):
            await safe_edit_text(target, f"🎉 <b>{html.escape(res.message)}</b>")
            inner = target.message
        else:
            await target.answer(f"🎉 <b>{html.escape(res.message)}</b>")
            inner = target
        if inner is not None:
            await _show_ai_step(inner, ctx, state)
        return

    if res.status is LoginStatus.TWO_FA_REQUIRED:
        await state.set_state(WizardStates.waiting_2fa)
        msg_text = (
            "🔐 <b>Two-Step Verification (2FA) Required</b>\n\n"
            "The login code was accepted — your account also has a Telegram "
            "<b>cloud password</b>.\n\n"
            "📥 Send that 2FA password now.\n"
            "<i>(Your message is deleted immediately after it is read.)</i>"
        )
        if isinstance(target, CallbackQuery):
            await safe_edit_text(target, msg_text)
        else:
            await target.answer(msg_text)
        return

    # Error -> show Telegram's real reason, keep the login session alive for a retry
    await state.update_data(otp_digits="")
    err_prompt = _otp_prompt_text(phone, "", note=res.message, delivery=delivery)
    if res.retry_after:
        # FloodWait: a keypad retry would just fail again, so tell them to wait.
        err_prompt += f"\n\n⏳ <i>Retry in about {res.retry_after}s.</i>"
    if isinstance(target, CallbackQuery):
        await safe_edit_text(target, err_prompt, reply_markup=otp_keypad_kb(""))
    else:
        await target.answer(err_prompt, reply_markup=otp_keypad_kb(""))


@router.message(NotACommand(), WizardStates.waiting_2fa)
async def msg_2fa_password(message: Message, ctx: AppContext, state: FSMContext) -> None:
    password = message.text or ""
    await safe_delete_message(message)
    res = await ctx.userbot.submit_2fa_password(password)
    if res.status is not LoginStatus.OK:
        await message.answer(f"{res.message}\n\nPlease try your 2FA password again:")
        return

    await message.answer(f"🎉 <b>{html.escape(res.message)}</b>")
    await _show_ai_step(message, ctx, state)


@router.callback_query(F.data == "wiz:login_session")
async def cb_login_session(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(WizardStates.waiting_string_session)
    await safe_edit_text(
        callback,
        "<b>Step 2 / 4 — Paste Telethon StringSession</b>\n\n"
        "📥 Send your Telethon <code>StringSession</code> string now.\n"
        "<i>(It will be deleted immediately and stored encrypted.)</i>",
    )


@router.message(NotACommand(), WizardStates.waiting_string_session)
async def msg_string_session(message: Message, ctx: AppContext, state: FSMContext) -> None:
    session_str = (message.text or "").strip()
    await safe_delete_message(message)
    data = await state.get_data()
    api_id = data.get("api_id") or ctx.env.tg_api_id or await ctx.db.get_setting("tg_api_id")
    api_hash = data.get("api_hash") or ctx.env.tg_api_hash or await ctx.db.get_setting("tg_api_hash")
    if not (api_id and api_hash):
        await message.answer("❌ Missing API ID / API Hash. Send /setup to restart.")
        return

    wait_msg = await message.answer("🔌 Connecting with StringSession...")
    res = await ctx.userbot.login_with_string_session(int(api_id), str(api_hash), session_str)
    if res.status is not LoginStatus.OK:
        await wait_msg.edit_text(f"{res.message}\n\nPaste a valid StringSession:")
        return

    await wait_msg.edit_text(f"🎉 <b>{html.escape(res.message)}</b>")
    await _show_ai_step(message, ctx, state)


# ----------------------------------------------------------------------
# Step 3: AI Provider + Key + Model
# ----------------------------------------------------------------------


@router.callback_query(F.data == "wiz:ai_step")
async def cb_wiz_ai_step(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await callback.answer()
    await _show_ai_step(callback, ctx, state)


@router.callback_query(F.data.startswith("wiz_ai:"))
async def cb_wiz_pick_provider(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await callback.answer()
    provider = (callback.data or "").split(":")[1]
    spec = PROVIDERS[provider]
    await state.update_data(wiz_provider=provider)

    existing_key = await ctx.db.get_provider_key(provider)
    if existing_key:
        await _prompt_wizard_model(callback, ctx, state, provider)
        return

    await state.set_state(WizardStates.waiting_ai_key)
    await safe_edit_text(
        callback,
        f"<b>Step 3 / 4 — {spec.emoji} {spec.name} API Key</b>\n\n"
        f"Get a free API key here: <a href='{spec.signup_url}'>{spec.signup_url}</a>\n\n"
        f"📥 Paste your <b>{spec.name} API Key</b> now.\n"
        "<i>(I'll test its speed immediately and delete your message.)</i>",
    )


@router.message(NotACommand(), WizardStates.waiting_ai_key)
async def msg_wiz_ai_key(message: Message, ctx: AppContext, state: FSMContext) -> None:
    api_key = (message.text or "").strip()
    await safe_delete_message(message)
    data = await state.get_data()
    provider = str(data.get("wiz_provider", "groq"))
    spec = PROVIDERS[provider]

    testing_msg = await message.answer(f"⚡ Testing {spec.name} API key & measuring speed...")
    ok, latency_ms, detail = await ctx.ai.test_connection(provider, spec.default_model, api_key)

    # The engine may have recovered from a retired model ID; use whatever worked.
    chosen_model = spec.default_model
    fallback_note = ""
    if "__MODEL_FALLBACK__:" in detail:
        detail, _, fallback = detail.partition("\n__MODEL_FALLBACK__:")
        old_model, _, new_model = fallback.partition("->")
        chosen_model = new_model or chosen_model
        fallback_note = (
            f"\n\nℹ️ <i><code>{html.escape(old_model)}</code> was retired by "
            f"{spec.name}; automatically switched to "
            f"<code>{html.escape(chosen_model)}</code>.</i>"
        )

    if not ok:
        await testing_msg.edit_text(
            f"❌ <b>{spec.name} key test failed</b> ({latency_ms}ms):\n"
            f"<code>{html.escape(detail)}</code>\n\n"
            "This usually means the <b>key itself</b> is wrong, expired, or lacks access.\n"
            "• Check the key was copied completely (no spaces)\n"
            "• Make a fresh key at "
            f"<a href='{spec.signup_url}'>{spec.signup_url}</a>\n"
            "• Or send /setup to pick another provider",
        )
        return

    await ctx.db.set_provider_key(provider, api_key)
    await ctx.db.set_setting("primary_provider", provider)
    await ctx.db.set_setting("primary_model", chosen_model)
    await state.set_state(None)

    kb, model_ids = model_picker_kb(
        provider=provider,
        prefix="wiz_m",
        active_model=chosen_model,
        back_cb="wiz:ai_step",
    )
    await state.update_data(wiz_model_choices=model_ids, wiz_provider=provider)
    await testing_msg.edit_text(
        f"✅ <b>{spec.name} connected!</b>\n"
        f"⚡ Test answer: <code>{html.escape(detail)}</code> in <b>{latency_ms}ms</b>\n"
        f"🧠 Model: <code>{html.escape(chosen_model)}</code>{fallback_note}\n\n"
        "Choose a model (or keep the recommended default):",
        reply_markup=kb,
    )


async def _prompt_wizard_model(
    target: CallbackQuery,
    ctx: AppContext,
    state: FSMContext,
    provider: str,
    extra_models: list[str] | None = None,
) -> None:
    spec = PROVIDERS[provider]
    current_model = str(await ctx.db.get_setting("primary_model", spec.default_model))
    kb, model_ids = model_picker_kb(
        provider=provider,
        prefix="wiz_m",
        active_model=current_model,
        extra_models=extra_models,
        back_cb="wiz:ai_step",
    )
    await state.update_data(wiz_model_choices=model_ids, wiz_provider=provider)
    await safe_edit_text(
        target,
        f"<b>Step 3 / 4 — Choose {spec.emoji} {spec.name} Model</b>\n\n"
        f"Current: <code>{html.escape(current_model)}</code>\n"
        "Select a model to continue to Group Selection:",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("wiz_m:"))
async def cb_wiz_model(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    data = await state.get_data()
    provider = str(data.get("wiz_provider", "groq"))

    if action == "idx":
        idx = int(parts[2])
        choices: list[str] = data.get("wiz_model_choices") or []
        if idx < 0 or idx >= len(choices):
            await callback.answer("Please pick again", show_alert=True)
            return
        model_id = choices[idx]
        await ctx.db.set_setting("primary_provider", provider)
        await ctx.db.set_setting("primary_model", model_id)
        await callback.answer(f"Selected {model_id}")
        await _show_group_step(callback, ctx, state)
        return

    if action == "live":
        await callback.answer("Fetching live models...")
        api_key = await ctx.db.get_provider_key(provider)
        if not api_key:
            await callback.answer("Missing API key", show_alert=True)
            return
        try:
            models = await fetch_live_models(provider, api_key, ctx.ai.client)
        except Exception as exc:
            await callback.answer(f"Fetch failed: {exc}", show_alert=True)
            return
        if not models:
            await callback.answer("No models returned", show_alert=True)
            return
        await _prompt_wizard_model(callback, ctx, state, provider, extra_models=models)
        return

    if action == "custom":
        await callback.answer()
        await state.set_state(WizardStates.waiting_custom_model)
        await safe_edit_text(
            callback,
            f"✏️ Send the exact model ID for <b>{PROVIDERS[provider].name}</b>:",
        )


@router.message(NotACommand(), WizardStates.waiting_custom_model)
async def msg_wiz_custom_model(message: Message, ctx: AppContext, state: FSMContext) -> None:
    model_id = (message.text or "").strip()
    if not model_id:
        await message.answer("❌ Please send a non-empty model ID.")
        return
    data = await state.get_data()
    provider = str(data.get("wiz_provider", "groq"))
    await ctx.db.set_setting("primary_provider", provider)
    await ctx.db.set_setting("primary_model", model_id)
    await state.set_state(None)
    await message.answer(f"✅ Model set to <code>{html.escape(model_id)}</code>!")
    await _show_group_step(message, ctx, state)


# ----------------------------------------------------------------------
# Step 4 Completion
# ----------------------------------------------------------------------


@router.callback_query(F.data == "wiz:finish")
async def cb_wiz_finish(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await ctx.db.set_setting("setup_completed", True)
    await ctx.db.set_setting("global_enabled", True)
    await ctx.userbot.reload_cache()

    await callback.answer("🎉 Setup complete! Userbot is LIVE.")
    notify_on = bool(await ctx.db.get_setting("notify_on_answer", False))
    text = (
        "🎉 <b>Setup Complete — Your Q&amp;A Userbot is Live!</b>\n\n"
        + await ctx.render_dashboard_text()
        + "\n\n☁️ <i>Using Render Free Tier? Open <b>⚙️ Account &amp; Setup → "
        "☁️ Backup This Deployment</b> to save your login &amp; groups across restarts!</i>"
        "\n🎁 <i>Want to help someone else set up their own copy? Use "
        "<b>Share Setup With Someone</b> — it never includes your credentials.</i>"
    )
    await safe_edit_text(callback, text, reply_markup=main_dashboard_kb(True, notify_on))


# ----------------------------------------------------------------------
# Catch-all: never leave the owner in silence
# ----------------------------------------------------------------------
# Every state-specific message handler above requires an exact FSM state. If the
# state is lost (process restart, duplicate instance, or the owner just types
# something unexpected) aiogram silently drops the update: the bot looks dead.
# This handler guarantees a reply that explains what to do next.


@router.message()
async def msg_any_fallback(message: Message, ctx: AppContext, state: FSMContext) -> None:
    current = await state.get_state()

    # This handler exists to explain silence, never to hijack another feature's
    # input. If the active state belongs to the AI/model menus, let those routers
    # process the message instead.
    if current in AI_MENU_STATES:
        raise SkipHandler
    has_session = bool(await ctx.db.get_setting("tg_session"))
    text = (message.text or "").strip()

    if current is None:
        # A phone number typed into an empty conversation is not "input while not
        # waiting" - it is someone in the middle of the login whose step got lost.
        # This happened when the owner reached the phone prompt through a screen
        # that cleared the state, so the number arrived with nothing waiting for
        # it and was answered with a shrug.
        #
        # The test must be *positive*: an international number, or a bare `+`
        # number. A first attempt also accepted any 7-15 digits, which swallowed
        # the API ID - "12345678" passed this check, so the wizard jumped to the
        # phone step, never stored the hash, and every later attempt failed with
        # "Missing API credentials". A dialling code is exactly what such a number
        # has and an API ID does not.
        if _looks_like_a_phone_number(text):
            await state.set_state(WizardStates.waiting_phone)
            await msg_phone(message, ctx, state)
            return

        # Likewise an API ID arriving with no step: that is someone typing the
        # credential again after "Missing API credentials. Send /setup", without
        # actually re-entering the wizard. Answering with a shrug is what made the
        # loop feel infinite - take the value instead.
        if re.fullmatch(r"\d{6,9}", text):
            await state.set_state(WizardStates.waiting_api_id)
            await msg_api_id(message, ctx, state)
            return

        hint = (
            "Send /start to open the dashboard."
            if has_session
            else "Send /start to open the setup wizard."
        )
        await message.answer(
            "🤔 I am not waiting for any input right now, so I did not act on that message.\n\n"
            f"{hint}\n"
            "<i>(If you were mid-setup, the conversation had reset — /start resumes it.)</i>"
        )
        return

    # A state is set but no handler matched: show where we are and how to continue.
    await message.answer(
        f"⚠️ I am waiting for a different kind of input right now "
        f"(step: <code>{html.escape(str(current).split(':')[-1])}</code>).\n\n"
        f"You sent: <code>{html.escape(text[:80]) or '(empty)'}</code>\n\n"
        "• Tap the buttons on my last message, or\n"
        "• Send /cancel then /start to restart the wizard."
    )


# ----------------------------------------------------------------------
# /logindiagnose — one-shot report of exactly what Telegram is doing
# ----------------------------------------------------------------------


@router.message(Command("logindiagnose"))
async def cmd_login_diagnose(message: Message, ctx: AppContext, state: FSMContext) -> None:
    """Report the real state of the Telegram login pipeline.

    ``send_code_request`` is the last step that can fail silently from the
    owner's point of view, so this prints the raw Telegram response type,
    whether a code was actually delivered, and any active flood wait.
    """
    data = await state.get_data()
    api_id = data.get("api_id") or ctx.env.tg_api_id or await ctx.db.get_setting("tg_api_id")
    api_hash = data.get("api_hash") or ctx.env.tg_api_hash or await ctx.db.get_setting("tg_api_hash")

    lines = ["🩺 <b>Telegram Login Diagnostics</b>", ""]
    lines.append(f"• API ID: <code>{api_id or 'MISSING'}</code>")
    lines.append(f"• API Hash: <code>{'set' if api_hash else 'MISSING'}</code>")
    lost = sorted(getattr(ctx.db, "undecryptable", set()))
    lines.append(
        "• ENCRYPTION_KEY: "
        + (
            "❌ changed — these stored secrets are unreadable: "
            + ", ".join(f"<code>{html.escape(k)}</code>" for k in lost)
            if lost
            else ("env" if ctx.env.encryption_key else "⚠️ not set (generated file)")
        )
    )
    lines.append(f"• Wizard state: <code>{await state.get_state() or 'none'}</code>")
    lines.append(f"• Phone: <code>{ctx.userbot.pending_phone or '—'}</code>")
    session = await ctx.db.get_setting("tg_session")
    lines.append(f"• Saved session: {'yes' if session else 'no'}")
    lines.append(
        f"• Database: <code>{html.escape(str(ctx.env.db_path))}</code>"
    )
    lines.append(f"• Userbot connected: {ctx.userbot.is_connected}")
    lines.append(
        f"• No-code login page: "
        f"{'✅ ' + html.escape(ctx.env.public_url) if ctx.env.public_url else '❌ no PUBLIC_URL / RENDER_EXTERNAL_URL'}"
    )

    if not (api_id and api_hash):
        lines += ["", "❌ No API credentials — send /start to run Step 1."]
        await message.answer("\n".join(lines))
        return

    # Probe Telegram directly so the answer is authoritative.
    from telethon.sessions import StringSession as _SS

    from ...userbot.login import describe_delivery, make_login_client

    probe_phone = ctx.userbot.pending_phone or ""
    client = make_login_client(int(api_id), str(api_hash), session=_SS())
    try:
        await client.connect()
        lines.append("• Telegram connection: ✅ reachable")
        if probe_phone:
            try:
                sent = await client.send_code_request(probe_phone)
                delivery = describe_delivery(sent)
                lines += [
                    "",
                    f"📨 <b>Telegram answered with:</b> "
                    f"<code>{type(getattr(sent, 'type', sent)).__name__}</code>",
                    f"• Delivered via: <b>{delivery.title}</b>",
                    f"• Code length: {delivery.length or 'unknown'}",
                ]
                if delivery.channel.value == "setup_required":
                    lines.append(
                        "• ⚠️ <b>BLOCKED</b>: no code will ever arrive until a login email "
                        "is set up (button below works)."
                    )
                    next_txt = (
                        delivery.next_channel.value.replace("_", " ")
                        if delivery.next_channel
                        else "none offered"
                    )
                    lines.append(f"• Next channel on resend: <b>{next_txt}</b>")
                    lines.append(
                        "• ⚠️ Telegram <b>accepted</b> the request — that is not proof it "
                        "delivered anything. Since Feb 2023 it sends no SMS to third-party "
                        "clients, and since early 2026 it often withholds the in-app code too "
                        "(Telethon #4730). If the service chat is empty, use the no-code login."
                    )
            except Exception as exc:
                lines.append(f"• ⚠️ send_code_request failed: <code>{html.escape(str(exc)[:200])}</code>")
        else:
            lines.append("• No phone number in progress — send one in Step 2 to probe delivery.")
    except Exception as exc:
        lines += ["", f"❌ <b>Cannot reach Telegram:</b> <code>{html.escape(str(exc)[:200])}</code>", ""]
        lines.append(
            "This is a network problem, not a code problem. On Render Free the service "
            "sleeps after 15 min idle — keep it awake with an uptime monitor on /health."
        )
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    lines += ["", "<i>Send /start to continue, or tap a button below.</i>"]
    await message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📷 QR Login (no code)", callback_data="wiz:login_qr")],
                [InlineKeyboardButton(text="⬅️ Login Options", callback_data="wiz:login_method")],
            ]
        ),
    )
