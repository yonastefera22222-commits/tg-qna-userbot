"""Exness credential setup from the dashboard.

Every Forex price the bot posts comes from the owner's own Exness trading
account, which means the owner has to hand over four values. That is the whole
job of this router: collect them one at a time, validate each *as it is typed*
(a bad Gateway host saved silently would surface days later as "no live data"),
and prove the set works with a live connection test.

Deliberately no wizard-style "press Next four times blind": each field reports
its own state, so the owner can stop, come back, and change one value later.
"""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ...ai.exness import (
    ExnessCredentialsMissing,
    ExnessError,
    normalise_gateway_url,
)
from ...ai.market import MarketFeed
from ..context import AppContext, safe_delete_message, safe_edit_text
from ..filters import NotACommand
from ..keyboards import cancel_to_dashboard_kb, exness_menu_kb
from ..states import ExnessStates

router = Router(name="exness")

#: setting key -> (state, human label, how to validate)
_FIELDS: dict[str, tuple[object, str]] = {
    "api_key": (ExnessStates.waiting_api_key, "API key"),
    "private_key": (ExnessStates.waiting_private_key, "private signing key"),
    "account_id": (ExnessStates.waiting_account_id, "trading account ID"),
    "gateway_url": (ExnessStates.waiting_gateway_url, "Gateway host"),
}

_HELP = {
    "api_key": (
        "🔑 <b>Exness API Key</b>\n"
        "Looks like <code>exnsk-…</code>.\n\n"
        "Where to get it:\n"
        "1. Sign in to your Exness Personal Area\n"
        "2. Open <b>Key Management</b> → <b>Create API key</b>\n"
        "3. Grant <b>Trading — Read-only</b>, and select the account you trade\n"
        "4. Copy the key identifier <i>and</i> the private signing key it shows\n\n"
        "<i>Send the key identifier now.</i>"
    ),
    "private_key": (
        "🗝 <b>Exness Private Signing Key</b>\n"
        "128 hex characters (shown once when the key is created), or an "
        "Ed25519 PEM if you registered your own public key.\n\n"
        "<i>Send it now. It is encrypted at rest and never echoed back.</i>"
    ),
    "account_id": (
        "🔢 <b>Exness Trading Account ID</b>\n"
        "Digits only — the account number in your Personal Area, not the "
        "login label.\n\n"
        "<i>Send it now.</i>"
    ),
    "gateway_url": (
        "🌐 <b>Exness Gateway Host</b>\n"
        "In Key Management, the <b>DNS widget</b> shows the current host. Copy "
        "it in full, e.g. <code>https://gateway.example</code>.\n\n"
        "<i>This is only used to discover your account's Access Point.</i>"
    ),
}


async def _configured(ctx: AppContext) -> dict[str, bool]:
    values = await ctx.db.get_settings(
        ("exness_api_key", "exness_private_key", "exness_account_id", "exness_gateway_url")
    )
    return {
        "api_key": bool(values.get("exness_api_key")),
        "private_key": bool(values.get("exness_private_key")),
        "account_id": bool(values.get("exness_account_id")),
        "gateway_url": bool(values.get("exness_gateway_url")),
    }


def _menu_text(state: dict[str, bool]) -> str:
    ready = all(state.values())
    lines = [
        "📈 <b>Live Prices — Exness</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "Every Forex price in your groups is read from your own Exness trading "
        "account, so the number the bot posts is the number on the Exness "
        "terminal. Exness has no public quote feed: it needs an API key.",
        "",
        ("🟢 <b>Connected</b> — prices will come from Exness."
         if ready
         else "🔴 <b>Not connected</b> — Forex groups will answer "
              "<code>No live data</code> rather than quote another venue."),
        "",
        "<b>Setup</b> (all four are required):",
        f"{'✅' if state['api_key'] else '⬜'} 1. API key",
        f"{'✅' if state['private_key'] else '⬜'} 2. Private signing key",
        f"{'✅' if state['account_id'] else '⬜'} 3. Trading account ID",
        f"{'✅' if state['gateway_url'] else '⬜'} 4. Gateway host",
    ]
    if state["account_id"]:
        lines.append("\n<i>Tip: uses a read-only key. The bot never places an order.</i>")
    return "\n".join(lines)


async def _render(target: CallbackQuery | Message, ctx: AppContext) -> None:
    state = await _configured(ctx)
    text = _menu_text(state)
    kb = exness_menu_kb(state)
    if isinstance(target, CallbackQuery):
        await safe_edit_text(target, text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(F.data == "exn:menu")
async def cb_menu(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await _render(callback, ctx)


@router.callback_query(F.data.startswith("exn:set:"))
async def cb_set_field(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    field = (callback.data or "").split(":", 2)[-1]
    entry = _FIELDS.get(field)
    if entry is None:
        await callback.answer("Unknown field", show_alert=True)
        return
    fsm_state, _label = entry
    await callback.answer()
    await state.set_state(fsm_state)
    await safe_edit_text(
        callback, _HELP[field], reply_markup=cancel_to_dashboard_kb()
    )


@router.callback_query(F.data == "exn:test")
async def cb_test(callback: CallbackQuery, ctx: AppContext) -> None:
    """Prove the whole chain works and show exactly what came back."""
    await callback.answer("Testing…")
    await safe_edit_text(callback, "🔌 <b>Testing Exness…</b>\nResolving host and reading a price.")

    feed = MarketFeed(settings_loader=lambda: ctx.db.get_settings(
        ("exness_api_key", "exness_private_key", "exness_account_id", "exness_gateway_url")
    ))
    try:
        snapshot = await feed.snapshot("EURUSD and gold")
    finally:
        await feed.close()

    if snapshot.is_empty():
        reason = feed.last_error or "no price returned"
        lines = [
            "🔴 <b>Exness test failed</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"<code>{html.escape(reason)}</code>",
            "",
            "<b>What it usually means</b>",
            "• <code>1000</code> — the API key is wrong or expired",
            "• <code>1001</code> — the private key does not match the API key",
            "• <code>1002</code>/<code>403</code> — the key cannot read this account",
            "• <code>4000</code> — the account does not quote that instrument",
            "• Timeout — the Gateway host is wrong, or this server cannot reach it",
        ]
        await safe_edit_text(callback, "\n".join(lines), reply_markup=exness_menu_kb(
            await _configured(ctx)
        ))
        return

    sample = "\n".join(f"• {html.escape(q.label(detailed=True))}" for q in snapshot.quotes)
    await safe_edit_text(
        callback,
        "🟢 <b>Exness connected</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{sample}\n\n"
        "<i>These prices came from your account. Forex groups will use them.</i>",
        reply_markup=exness_menu_kb(await _configured(ctx)),
    )


def _validate(field: str, raw: str) -> tuple[str | None, str | None]:
    """Return (clean value, error). One of them is always None."""
    value = (raw or "").strip()
    if not value:
        return None, "That was empty — nothing saved."
    if field == "api_key":
        if len(value) < 12:
            return None, "That is too short for an Exness API key."
        return value, None
    if field == "private_key":
        # Validation proper happens on load (hex or PEM); catch the obvious
        # paste errors here so the owner is told now, not at answer time.
        if value.startswith("-----BEGIN"):
            return value, None
        compact = "".join(value.split())
        if len(compact) != 128 or any(c not in "0123456789abcdefABCDEF" for c in compact):
            return None, (
                "The private key should be 128 hex characters, or a PEM starting "
                "with <code>-----BEGIN</code>."
            )
        return compact, None
    if field == "account_id":
        if not value.isdigit():
            return None, "The account ID is digits only (the account number, not the label)."
        return value, None
    if field == "gateway_url":
        try:
            return normalise_gateway_url(value), None
        except ExnessError as exc:
            return None, str(exc)
        except ExnessCredentialsMissing:
            return None, "That was empty — nothing saved."
    return None, "Unknown field."


@router.message(NotACommand(), ExnessStates.waiting_api_key)
@router.message(NotACommand(), ExnessStates.waiting_private_key)
@router.message(NotACommand(), ExnessStates.waiting_account_id)
@router.message(NotACommand(), ExnessStates.waiting_gateway_url)
async def msg_field(message: Message, ctx: AppContext, state: FSMContext) -> None:
    current = await state.get_state() or ""
    field = next(
        (name for name, (fsm_state, _label) in _FIELDS.items()
         if str(fsm_state.state) == current),
        None,
    )
    if field is None or message.text is None:
        return
    label = _FIELDS[field][1]
    clean, error = _validate(field, message.text)
    if error is not None:
        # Stay in the same state so the owner can simply retype.
        await message.answer(f"⚠️ {error}\n\nTry again, or /cancel.")
        return

    await ctx.db.set_setting(f"exness_{field}", clean)
    await state.clear()
    # The message may contain a secret; remove it from the chat.
    if field in ("api_key", "private_key"):
        await safe_delete_message(message)
        saved = f"✅ <b>Saved.</b> {label[0].upper()}{label[1:]} stored (encrypted)."
    else:
        saved = f"✅ <b>Saved.</b> {label[0].upper()}{label[1:]}: <code>{html.escape(clean)}</code>"

    configured = await _configured(ctx)
    tail = (
        "\n\n🟢 All four values are set — tap <b>Test Connection</b>."
        if all(configured.values())
        else "\n\nStill needed: " + ", ".join(
            f"{label}" for key, (_s, lab) in _FIELDS.items() if not configured[key]
            for label in [lab]
        )
    )
    await message.answer(
        f"{saved}{tail}", reply_markup=exness_menu_kb(configured)
    )
