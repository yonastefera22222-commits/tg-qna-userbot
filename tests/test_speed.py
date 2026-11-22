"""Tests for response-latency behaviour: reasoning effort, payload shape, hot path."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from qna_bot.ai.engine import KEEPALIVE_EXPIRY_SECONDS, REASONING_HEADROOM_TOKENS, AIEngine
from qna_bot.ai.providers import (
    DEFAULT_SPEED_MODE,
    reasoning_effort_for,
)
from qna_bot.storage.database import Database, SecretCipher

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    "models/gemini-3.5-flash-lite:generateContent"
)
GEMINI_38_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    "models/gemini-3.8-flash:generateContent"
)


# ---------------------------------------------------------------------------
# Reasoning effort: the default is the slow setting, and each family differs
# ---------------------------------------------------------------------------


def test_default_mode_is_the_fastest_one() -> None:
    assert DEFAULT_SPEED_MODE == "fast"


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("openai/gpt-oss-120b", "low"),  # gpt-oss 400s on "none"
        ("openai/gpt-oss-20b", "low"),
        ("openai/gpt-oss-safeguard-20b", "low"),
        ("qwen/qwen3.8-27b", "none"),  # Qwen accepts a true off switch
    ],
)
def test_groq_effort_uses_each_family_vocabulary(model: str, expected: str) -> None:
    assert reasoning_effort_for("groq", model, "fast") == expected


def test_groq_gpt_oss_never_gets_none() -> None:
    """`none` is a Qwen value: gpt-oss rejects it with a 400."""
    for mode in ("fast", "balanced", "smart"):
        assert reasoning_effort_for("groq", "openai/gpt-oss-120b", mode) != "none"


def test_groq_graded_ladder() -> None:
    assert reasoning_effort_for("groq", "openai/gpt-oss-120b", "balanced") == "medium"
    assert reasoning_effort_for("groq", "openai/gpt-oss-120b", "smart") == "high"


def test_gemini_38_flash_never_gets_minimal() -> None:
    """gemini-3.8-flash errors on `minimal`; `low` is the fastest legal value."""
    for mode in ("fast", "balanced", "smart"):
        assert reasoning_effort_for("gemini", "gemini-3.8-flash", mode) != "minimal"


def test_gemini_flash_lite_accepts_minimal() -> None:
    assert reasoning_effort_for("gemini", "gemini-3.5-flash-lite", "fast") == "minimal"
    assert reasoning_effort_for("gemini", "gemini-3.1-flash-lite", "fast") == "minimal"
    assert reasoning_effort_for("gemini", "gemini-3.6-flash", "fast") == "minimal"


def test_models_without_a_faster_setting_get_nothing() -> None:
    # Sending an unsupported value is a 400, so omit the parameter entirely.
    assert reasoning_effort_for("gemini", "gemini-3.1-pro", "fast") is None
    assert reasoning_effort_for("gemini", "gemma-4-31b-it", "fast") is None
    assert reasoning_effort_for("openrouter", "openrouter/free", "fast") is None


def test_unknown_gemini_flash_falls_back_to_low() -> None:
    assert reasoning_effort_for("gemini", "gemini-9-something-flash", "fast") == "low"


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------


async def _ask(engine: AIEngine, **kwargs) -> None:
    await engine.answer_question(
        text="Capital of Kenya?",
        answer_style="giveaway",
        provider_keys={"groq": "k", "gemini": "k"},
        local_math_enabled=False,
        **kwargs,
    )


@pytest.mark.asyncio
@respx.mock
async def test_groq_sends_low_effort_and_reasoning_headroom() -> None:
    """Without headroom the 48-token answer budget is eaten by thinking."""
    route = respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "Nairobi"}}]})
    )
    engine = AIEngine()
    await engine.start()
    try:
        await _ask(engine, primary_provider="groq", primary_model="openai/gpt-oss-120b")
        body = json.loads(route.calls[0].request.content)
        assert body["reasoning_effort"] == "low"
        assert body["max_tokens"] == 48 + REASONING_HEADROOM_TOKENS
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_qwen_off_switch_needs_no_headroom() -> None:
    route = respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "Nairobi"}}]})
    )
    engine = AIEngine()
    await engine.start()
    try:
        await _ask(engine, primary_provider="groq", primary_model="qwen/qwen3.8-27b")
        body = json.loads(route.calls[0].request.content)
        assert body["reasoning_effort"] == "none"
        assert body["max_tokens"] == 48, "no thinking happens, so no headroom"
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_gemini_sends_thinking_level() -> None:
    route = respx.post(GEMINI_URL).mock(
        return_value=httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "Nairobi"}]}}]}
        )
    )
    engine = AIEngine()
    await engine.start()
    try:
        await _ask(engine, primary_provider="gemini", primary_model="gemini-3.5-flash-lite")
        cfg = json.loads(route.calls[0].request.content)["generationConfig"]
        assert cfg["thinkingConfig"] == {"thinkingLevel": "minimal"}
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_rejected_speed_parameter_is_retried_without_it() -> None:
    """A 400 about thinkingConfig must cost one retry, never the answer."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "thinkingConfig" in body.get("generationConfig", {}):
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Thinking level THINKING_LEVEL_MINIMAL is not supported "
                        "for this model"
                    }
                },
            )
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "Nairobi"}]}}]}
        )

    respx.post(GEMINI_38_URL).mock(side_effect=handler)
    engine = AIEngine()
    await engine.start()
    try:
        res = await engine.answer_question(
            text="Capital of Kenya?",
            answer_style="giveaway",
            primary_provider="gemini",
            primary_model="gemini-3.8-flash",
            provider_keys={"gemini": "k"},
            local_math_enabled=False,
        )
        assert res.answered is True
        assert res.answer == "Nairobi"
        assert len(calls) == 2, "one rejection + one retry"
        assert "thinkingConfig" in calls[0]["generationConfig"]
        assert "thinkingConfig" not in calls[1]["generationConfig"]
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_unrelated_400_is_not_retried() -> None:
    """Only speed-parameter rejections get the silent retry."""
    route = respx.post(GROQ_URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "invalid api key"}})
    )
    engine = AIEngine()
    await engine.start()
    try:
        res = await engine.answer_question(
            text="Capital of Kenya?",
            answer_style="giveaway",
            primary_provider="groq",
            primary_model="openai/gpt-oss-120b",
            provider_keys={"groq": "k"},
            local_math_enabled=False,
        )
        assert res.answered is False
        assert route.call_count == 1
    finally:
        await engine.close()


# ---------------------------------------------------------------------------
# Hot path: fewer round-trips before the AI call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batched_settings_match_individual_reads(tmp_path) -> None:
    db = Database(tmp_path / "b.db", SecretCipher.from_env_or_file(None, tmp_path / ".k"))
    await db.connect()
    try:
        await db.set_setting("primary_provider", "gemini")
        await db.set_setting("ai_max_tokens", 200)
        batched = await db.get_settings(("primary_provider", "ai_max_tokens", "missing_key"))
        assert batched == {"primary_provider": "gemini", "ai_max_tokens": 200}
        assert "missing_key" not in batched
        # Callers apply defaults, exactly as the individual reads did.
        assert batched.get("missing_key", "fallback") == "fallback"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_batched_settings_decrypt_provider_keys(tmp_path) -> None:
    db = Database(tmp_path / "b.db", SecretCipher.from_env_or_file(None, tmp_path / ".k"))
    await db.connect()
    try:
        await db.set_setting("groq_api_key", "secret-value")
        got = await db.get_settings(("groq_api_key",))
        assert got["groq_api_key"] == "secret-value", "secrets must not leak encrypted"
    finally:
        await db.close()


def test_keepalive_connection_outlives_a_quiet_spell() -> None:
    """httpx's 5s default dropped the TLS connection between questions."""
    assert KEEPALIVE_EXPIRY_SECONDS >= 60, (
        "short keepalive means a fresh TLS handshake for every giveaway burst"
    )


@pytest.mark.asyncio
async def test_keepalive_setting_reaches_the_client() -> None:
    engine = AIEngine()
    await engine.start()
    try:
        pool = engine.client._transport._pool  # type: ignore[attr-defined]
        assert pool._keepalive_expiry == KEEPALIVE_EXPIRY_SECONDS
    finally:
        await engine.close()


# ---------------------------------------------------------------------------
# Admin cache: the 10-minute refresh must never block an answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_admin_cache_answers_without_waiting(tmp_path) -> None:
    """A stale list must not trigger a blocking participant scan."""
    from qna_bot.userbot.manager import UserbotManager

    db = Database(tmp_path / "b.db", SecretCipher.from_env_or_file(None, tmp_path / ".k"))
    await db.connect()
    try:
        mgr = UserbotManager(db, AIEngine())
        # Seed a stale entry whose refresh would be slow if it ran inline.
        mgr._admin_cache[999] = (time.monotonic() - 10_000, {42}, True)

        refresh = AsyncMock()
        mgr.refresh_group_admins = refresh  # type: ignore[assignment]

        event = MagicMock()
        event.sender_id = 42
        event.message.from_id = None

        started = time.perf_counter()
        allowed = await mgr._is_admin(999, event)
        elapsed = time.perf_counter() - started

        assert allowed is True, "the cached list already contains the sender"
        assert elapsed < 0.05, "answering must not wait for a participant scan"
        await asyncio.sleep(0)  # let the background task run
        refresh.assert_awaited()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unknown_group_still_fetches_once(tmp_path) -> None:
    """First sighting has nothing cached, so a fetch is unavoidable."""
    from qna_bot.userbot.manager import UserbotManager

    db = Database(tmp_path / "b.db", SecretCipher.from_env_or_file(None, tmp_path / ".k"))
    await db.connect()
    try:
        mgr = UserbotManager(db, AIEngine())
        refresh = AsyncMock(side_effect=lambda cid: mgr._cache_admins(cid, {7}, verified=True))
        mgr.refresh_group_admins = refresh  # type: ignore[assignment]

        event = MagicMock()
        event.sender_id = 7
        event.message.from_id = None

        assert await mgr._is_admin(555, event) is True
        refresh.assert_awaited_once()
    finally:
        await db.close()
