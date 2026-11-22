"""Tests for AIEngine single-shot answering, SKIP handling, local math, and fallback."""

from __future__ import annotations

import httpx
import pytest
import respx

from qna_bot.ai.engine import AIEngine


@pytest.mark.asyncio
async def test_local_math_bypasses_http() -> None:
    engine = AIEngine()
    await engine.start()
    try:
        res = await engine.answer_question(
            text="What is 125 * 8?",
            answer_style="giveaway",
            primary_provider="groq",
            primary_model="llama-3.3-70b-versatile",
            provider_keys={},  # No API keys needed for local math!
            local_math_enabled=True,
        )
        assert res.answered is True
        assert res.answer == "1000"
        assert res.provider == "local_math"
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_groq_giveaway_answer_cleaning_and_skip() -> None:
    route = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"choices": [{"message": {"content": "The answer is: Paris."}}]},
            ),
            httpx.Response(
                200,
                json={"choices": [{"message": {"content": "SKIP"}}]},
            ),
        ]
    )

    engine = AIEngine()
    await engine.start()
    try:
        r1 = await engine.answer_question(
            text="Capital of France?",
            answer_style="giveaway",
            primary_provider="groq",
            primary_model="llama-3.3-70b-versatile",
            provider_keys={"groq": "gsk_fake"},
        )
        assert r1.answered is True
        assert r1.answer == "Paris"
        assert r1.provider == "groq"
        assert r1.used_fallback is False

        r2 = await engine.answer_question(
            text="Everyone get ready for round two!",
            answer_style="giveaway",
            primary_provider="groq",
            primary_model="llama-3.3-70b-versatile",
            provider_keys={"groq": "gsk_fake"},
        )
        assert r2.answered is False
        assert r2.answer is None
        assert route.call_count == 2
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_automatic_fallback_from_groq_429_to_gemini() -> None:
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(429, json={"error": {"message": "Rate limit exceeded"}})
    )
    respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "Tokyo"}]}}]},
        )
    )

    engine = AIEngine()
    await engine.start()
    try:
        res = await engine.answer_question(
            text="Capital of Japan?",
            answer_style="giveaway",
            primary_provider="groq",
            primary_model="llama-3.3-70b-versatile",
            provider_keys={"groq": "gsk_fake", "gemini": "gem_fake"},
            fallback_provider="gemini",
            fallback_model="gemini-3.5-flash-lite",
        )
        assert res.answered is True
        assert res.answer == "Tokyo"
        assert res.provider == "gemini"
        assert res.used_fallback is True
    finally:
        await engine.close()
