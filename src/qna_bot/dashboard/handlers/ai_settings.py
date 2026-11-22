"""AI Providers & Models settings router (Primary, Fallback, Keys, Live Models, Benchmark)."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ...ai.providers import PROVIDERS, fetch_live_models
from ..context import AppContext, safe_delete_message, safe_edit_text
from ..keyboards import ai_menu_kb, model_picker_kb, provider_picker_kb
from ..states import DashboardStates

router = Router(name="ai_settings")


async def _render_ai_menu(target: CallbackQuery | Message, ctx: AppContext) -> None:
    keys = await ctx.db.get_all_provider_keys()
    primary_p = str(await ctx.db.get_setting("primary_provider", "groq"))
    p_spec = PROVIDERS.get(primary_p, PROVIDERS["groq"])
    primary_m = str(await ctx.db.get_setting("primary_model", p_spec.default_model))

    fb_p = await ctx.db.get_setting("fallback_provider")
    fb_m = await ctx.db.get_setting("fallback_model")
    fb_line = (
        f"{PROVIDERS[str(fb_p)].emoji} {PROVIDERS[str(fb_p)].name} · <code>{html.escape(str(fb_m))}</code>"
        if (fb_p and fb_m and str(fb_p) in PROVIDERS)
        else "<i>Not configured</i>"
    )

    configured_lines = []
    for slug, spec in PROVIDERS.items():
        badge = "✅ Key saved" if slug in keys else "⬜ No key"
        configured_lines.append(f"• {spec.emoji} <b>{spec.name}:</b> {badge}")

    text = (
        "🤖 <b>AI Providers &amp; Models</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ <b>Primary:</b> {p_spec.emoji} {p_spec.name} · <code>{html.escape(primary_m)}</code>\n"
        f"🛡 <b>Fallback:</b> {fb_line}\n\n"
        "<b>Configured Providers:</b>\n"
        + "\n".join(configured_lines)
        + "\n\n💡 <i>Tip: Use Groq as Primary for lowest latency and Gemini as Fallback if you hit rate limits!</i>"
    )
    kb = ai_menu_kb(has_fallback=bool(fb_p and fb_m))
    if isinstance(target, CallbackQuery):
        await safe_edit_text(target, text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(F.data == "ai:menu")
async def cb_ai_menu(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await _render_ai_menu(callback, ctx)


@router.callback_query(F.data == "ai:pick_primary")
async def cb_pick_primary(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer()
    keys = await ctx.db.get_all_provider_keys()
    await safe_edit_text(
        callback,
        "🔄 <b>Select Primary AI Provider</b>\n\n"
        "Choose a provider (if no key is saved yet, I'll ask for it):",
        reply_markup=provider_picker_kb("ai_pri", configured_keys=keys, back_cb="ai:menu"),
    )


@router.callback_query(F.data.startswith("ai_pri:"))
async def cb_set_primary_provider(
    callback: CallbackQuery, ctx: AppContext, state: FSMContext
) -> None:
    await callback.answer()
    provider = (callback.data or "").split(":")[1]
    spec = PROVIDERS[provider]
    key = await ctx.db.get_provider_key(provider)

    if not key:
        await state.update_data(key_target_provider=provider, key_target_role="primary")
        await state.set_state(DashboardStates.waiting_ai_key_update)
        await safe_edit_text(
            callback,
            f"🔑 <b>Enter {spec.emoji} {spec.name} API Key</b>\n\n"
            f"Get a free key at <a href='{spec.signup_url}'>{spec.signup_url}</a>\n"
            "📥 Send your API key now (it will be deleted immediately):",
        )
        return

    await ctx.db.set_setting("primary_provider", provider)
    await ctx.db.set_setting("primary_model", spec.default_model)
    await _show_model_picker(callback, ctx, state, provider=provider, role="primary")


@router.callback_query(F.data == "ai:pick_primary_model")
async def cb_pick_primary_model(
    callback: CallbackQuery, ctx: AppContext, state: FSMContext
) -> None:
    await callback.answer()
    provider = str(await ctx.db.get_setting("primary_provider", "groq"))
    await _show_model_picker(callback, ctx, state, provider=provider, role="primary")


@router.callback_query(F.data == "ai:pick_fallback")
async def cb_pick_fallback(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer()
    keys = await ctx.db.get_all_provider_keys()
    await safe_edit_text(
        callback,
        "🛡 <b>Select Fallback AI Provider</b>\n\n"
        "If your primary provider hits a rate limit (HTTP 429) or times out, "
        "the userbot automatically retries with your fallback provider:",
        reply_markup=provider_picker_kb(
            "ai_fb", configured_keys=keys, include_none=True, back_cb="ai:menu"
        ),
    )


@router.callback_query(F.data.startswith("ai_fb:"))
async def cb_set_fallback_provider(
    callback: CallbackQuery, ctx: AppContext, state: FSMContext
) -> None:
    provider = (callback.data or "").split(":")[1]
    if provider == "none":
        await ctx.db.delete_setting("fallback_provider")
        await ctx.db.delete_setting("fallback_model")
        await callback.answer("Fallback disabled")
        await _render_ai_menu(callback, ctx)
        return

    await callback.answer()
    spec = PROVIDERS[provider]
    key = await ctx.db.get_provider_key(provider)
    if not key:
        await state.update_data(key_target_provider=provider, key_target_role="fallback")
        await state.set_state(DashboardStates.waiting_ai_key_update)
        await safe_edit_text(
            callback,
            f"🔑 <b>Enter {spec.emoji} {spec.name} API Key (for Fallback)</b>\n\n"
            f"Get a free key at <a href='{spec.signup_url}'>{spec.signup_url}</a>\n"
            "📥 Send your API key now:",
        )
        return

    await ctx.db.set_setting("fallback_provider", provider)
    await ctx.db.set_setting("fallback_model", spec.default_model)
    await _show_model_picker(callback, ctx, state, provider=provider, role="fallback")


@router.callback_query(F.data == "ai:manage_keys")
async def cb_manage_keys(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer()
    keys = await ctx.db.get_all_provider_keys()
    await safe_edit_text(
        callback,
        "🔑 <b>Add or Update an API Key</b>\n\nPick a provider:",
        reply_markup=provider_picker_kb("ai_key", configured_keys=keys, back_cb="ai:menu"),
    )


@router.callback_query(F.data.startswith("ai_key:"))
async def cb_prompt_key_update(
    callback: CallbackQuery, state: FSMContext
) -> None:
    await callback.answer()
    provider = (callback.data or "").split(":")[1]
    spec = PROVIDERS[provider]
    await state.update_data(key_target_provider=provider, key_target_role="key_only")
    await state.set_state(DashboardStates.waiting_ai_key_update)
    await safe_edit_text(
        callback,
        f"🔑 <b>Update {spec.emoji} {spec.name} API Key</b>\n\n"
        f"Get a free key at <a href='{spec.signup_url}'>{spec.signup_url}</a>\n"
        "📥 Send the new API key now (your message will be deleted immediately):",
    )


@router.message(DashboardStates.waiting_ai_key_update)
async def msg_update_ai_key(message: Message, ctx: AppContext, state: FSMContext) -> None:
    api_key = (message.text or "").strip()
    await safe_delete_message(message)
    data = await state.get_data()
    provider = str(data.get("key_target_provider", "groq"))
    role = str(data.get("key_target_role", "key_only"))
    spec = PROVIDERS[provider]

    wait_msg = await message.answer(f"⚡ Testing {spec.name} API key...")
    ok, latency_ms, detail = await ctx.ai.test_connection(provider, spec.default_model, api_key)
    if not ok:
        await wait_msg.edit_text(
            f"❌ <b>{spec.name} test failed</b> ({latency_ms}ms):\n"
            f"<code>{html.escape(detail)}</code>\n\nSend another key or /cancel:"
        )
        return

    await ctx.db.set_provider_key(provider, api_key)
    if role == "primary":
        await ctx.db.set_setting("primary_provider", provider)
        await ctx.db.set_setting("primary_model", spec.default_model)
    elif role == "fallback":
        await ctx.db.set_setting("fallback_provider", provider)
        await ctx.db.set_setting("fallback_model", spec.default_model)

    await state.clear()
    await wait_msg.edit_text(
        f"✅ <b>{spec.name} API key saved!</b> ({latency_ms}ms · test answer: <code>{html.escape(detail)}</code>)"
    )
    await _render_ai_menu(message, ctx)


# ----------------------------------------------------------------------
# Model Selection (Presets, Live API Fetch, Custom Model ID)
# ----------------------------------------------------------------------


async def _show_model_picker(
    target: CallbackQuery,
    ctx: AppContext,
    state: FSMContext,
    *,
    provider: str,
    role: str,
    extra_models: list[str] | None = None,
) -> None:
    spec = PROVIDERS[provider]
    setting_key = "primary_model" if role == "primary" else "fallback_model"
    current = str(await ctx.db.get_setting(setting_key, spec.default_model))
    kb, model_ids = model_picker_kb(
        provider=provider,
        prefix="ai_m",
        active_model=current,
        extra_models=extra_models,
        back_cb="ai:menu",
    )
    await state.update_data(
        ai_model_choices=model_ids,
        ai_model_provider=provider,
        ai_model_role=role,
    )
    role_title = "Primary" if role == "primary" else "Fallback"
    await safe_edit_text(
        target,
        f"🧠 <b>Select {role_title} Model ({spec.emoji} {spec.name})</b>\n\n"
        f"Current: <code>{html.escape(current)}</code>",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("ai_m:"))
async def cb_ai_model_action(
    callback: CallbackQuery, ctx: AppContext, state: FSMContext
) -> None:
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    data = await state.get_data()
    provider = str(data.get("ai_model_provider", "groq"))
    role = str(data.get("ai_model_role", "primary"))

    if action == "idx":
        idx = int(parts[2])
        choices: list[str] = data.get("ai_model_choices") or []
        if idx < 0 or idx >= len(choices):
            await callback.answer("Please pick again", show_alert=True)
            return
        model_id = choices[idx]
        if role == "primary":
            await ctx.db.set_setting("primary_provider", provider)
            await ctx.db.set_setting("primary_model", model_id)
        else:
            await ctx.db.set_setting("fallback_provider", provider)
            await ctx.db.set_setting("fallback_model", model_id)
        await callback.answer(f"Set to {model_id}")
        await _render_ai_menu(callback, ctx)
        return

    if action == "live":
        await callback.answer("Fetching live models...")
        key = await ctx.db.get_provider_key(provider)
        if not key:
            await callback.answer("No API key saved for this provider.", show_alert=True)
            return
        try:
            models = await fetch_live_models(provider, key, ctx.ai.client)
        except Exception as exc:
            await callback.answer(f"Error: {exc}", show_alert=True)
            return
        if not models:
            await callback.answer("No models found", show_alert=True)
            return
        await _show_model_picker(
            callback, ctx, state, provider=provider, role=role, extra_models=models
        )
        return

    if action == "custom":
        await callback.answer()
        await state.set_state(DashboardStates.waiting_custom_model_update)
        await safe_edit_text(
            callback,
            f"✏️ Send the exact model ID for <b>{PROVIDERS[provider].name}</b> (or /cancel):",
        )


@router.message(DashboardStates.waiting_custom_model_update)
async def msg_custom_model_update(
    message: Message, ctx: AppContext, state: FSMContext
) -> None:
    model_id = (message.text or "").strip()
    if not model_id:
        await message.answer("❌ Model ID cannot be empty.")
        return
    data = await state.get_data()
    provider = str(data.get("ai_model_provider", "groq"))
    role = str(data.get("ai_model_role", "primary"))
    if role == "primary":
        await ctx.db.set_setting("primary_provider", provider)
        await ctx.db.set_setting("primary_model", model_id)
    else:
        await ctx.db.set_setting("fallback_provider", provider)
        await ctx.db.set_setting("fallback_model", model_id)
    await state.clear()
    await message.answer(f"✅ Model updated to <code>{html.escape(model_id)}</code>")
    await _render_ai_menu(message, ctx)


# ----------------------------------------------------------------------
# Live Latency Benchmark across All Configured Providers
# ----------------------------------------------------------------------


@router.callback_query(F.data == "ai:benchmark")
async def cb_ai_benchmark(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer("Running speed benchmark...")
    keys = await ctx.db.get_all_provider_keys()
    if not keys:
        await safe_edit_text(callback, "❌ No AI provider keys configured yet.")
        return

    await safe_edit_text(callback, "⚡ <b>Benchmarking configured AI providers...</b>")
    primary_p = str(await ctx.db.get_setting("primary_provider", "groq"))
    primary_m = str(
        await ctx.db.get_setting("primary_model", PROVIDERS[primary_p].default_model)
    )

    lines = ["⚡ <b>Provider Speed Benchmark</b>\n<i>Prompt: 'What is the capital of France?'</i>\n"]
    for slug, api_key in keys.items():
        spec = PROVIDERS[slug]
        model = primary_m if slug == primary_p else spec.default_model
        ok, ms, detail = await ctx.ai.test_connection(slug, model, api_key)
        if ok:
            lines.append(
                f"✅ {spec.emoji} <b>{spec.name}</b> (<code>{html.escape(model)}</code>)\n"
                f"    ⏱ <b>{ms}ms</b> · Answer: <code>{html.escape(detail)}</code>"
            )
        else:
            lines.append(
                f"❌ {spec.emoji} <b>{spec.name}</b> ({ms}ms): <code>{html.escape(detail[:90])}</code>"
            )

    fb_p = await ctx.db.get_setting("fallback_provider")
    fb_m = await ctx.db.get_setting("fallback_model")
    await safe_edit_text(
        callback,
        "\n".join(lines),
        reply_markup=ai_menu_kb(has_fallback=bool(fb_p and fb_m)),
    )
