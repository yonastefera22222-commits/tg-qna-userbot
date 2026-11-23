"""Tests for answering photo questions (vision routing + image payloads)."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from qna_bot.ai.engine import AIEngine
from qna_bot.ai.providers import supports_vision, vision_model_for
from qna_bot.storage.database import GroupConfig
from qna_bot.userbot.filters import should_evaluate_message

# 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    "models/gemini-3.5-flash-lite:generateContent"
)


# ---------------------------------------------------------------------------
# Which models can actually see an image
# ---------------------------------------------------------------------------


def test_only_qwen_sees_images_on_groq() -> None:
    """Groq serves vision through qwen3.8 only; gpt-oss is text-only."""
    assert supports_vision("groq", "qwen/qwen3.8-27b") is True
    assert supports_vision("groq", "openai/gpt-oss-120b") is False
    assert supports_vision("groq", "openai/gpt-oss-20b") is False
    assert vision_model_for("groq", "openai/gpt-oss-120b") == "qwen/qwen3.8-27b"


def test_gemini_flash_models_see_images() -> None:
    assert supports_vision("gemini", "gemini-3.5-flash-lite") is True
    assert supports_vision("gemini", "gemini-3.8-flash") is True
    assert vision_model_for("gemini", "gemini-3.6-flash") == "gemini-3.6-flash"


# ---------------------------------------------------------------------------
# The prefilter must let photos through
# ---------------------------------------------------------------------------


def test_photo_without_caption_is_a_valid_question() -> None:
    """A screenshot of a quiz has no text; it must not be dropped as 'empty'."""
    group = GroupConfig(chat_id=1, title="Quiz", enabled=True)
    decision = should_evaluate_message("", group, has_image=True)
    assert decision.should_process is True
    assert decision.reason == "image_only"


def test_photo_rejected_when_group_has_vision_off() -> None:
    group = GroupConfig(chat_id=1, title="Quiz", enabled=True)
    group.vision_enabled = False
    decision = should_evaluate_message("", group, has_image=True)
    assert decision.should_process is False
    assert decision.reason == "vision_disabled"


def test_empty_text_without_image_is_still_ignored() -> None:
    group = GroupConfig(chat_id=1, title="Quiz", enabled=True)
    assert should_evaluate_message("", group).should_process is False


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_groq_switches_to_vision_model_and_sends_data_uri() -> None:
    route = respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "Kilimanjaro"}}]})
    )
    engine = AIEngine()
    await engine.start()
    try:
        res = await engine.answer_question(
            text="What mountain is this?",
            answer_style="giveaway",
            primary_provider="groq",
            primary_model="openai/gpt-oss-120b",  # text-only model configured
            provider_keys={"groq": "k"},
            local_math_enabled=False,
            image=PNG,
            image_mime="image/png",
        )
        body = json.loads(route.calls[0].request.content)
        assert body["model"] == "qwen/qwen3.8-27b", "must switch to the vision model"
        content = body["messages"][1]["content"]
        assert isinstance(content, list)
        assert [p["type"] for p in content] == ["text", "image_url"]
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert res.answer == "Kilimanjaro"
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_gemini_sends_inline_data() -> None:
    route = respx.post(GEMINI_URL).mock(
        return_value=httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "Kilimanjaro"}]}}]}
        )
    )
    engine = AIEngine()
    await engine.start()
    try:
        res = await engine.answer_question(
            text="What mountain is this?",
            answer_style="giveaway",
            primary_provider="gemini",
            primary_model="gemini-3.5-flash-lite",
            provider_keys={"gemini": "k"},
            local_math_enabled=False,
            image=PNG,
            image_mime="image/png",
        )
        parts = json.loads(route.calls[0].request.content)["contents"][0]["parts"]
        assert parts[0]["text"] == "What mountain is this?"
        assert parts[1]["inline_data"]["mime_type"] == "image/png"
        assert parts[1]["inline_data"]["data"]
        assert res.answer == "Kilimanjaro"
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_text_only_requests_are_unchanged() -> None:
    """Regression: no image must keep the plain-string content shape."""
    route = respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "Nairobi"}}]})
    )
    engine = AIEngine()
    await engine.start()
    try:
        await engine.answer_question(
            text="Capital of Kenya",
            answer_style="giveaway",
            primary_provider="groq",
            primary_model="openai/gpt-oss-120b",
            provider_keys={"groq": "k"},
            local_math_enabled=False,
        )
        body = json.loads(route.calls[0].request.content)
        assert body["model"] == "openai/gpt-oss-120b"
        assert isinstance(body["messages"][1]["content"], str)
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_local_math_is_skipped_for_photos() -> None:
    """Math in *the image* is not solved from the caption.

    This has to keep holding: a poster reading "2 + 2 = ?" and a caption reading
    "47 * 83" are different questions, and solving the caption would post the
    wrong number with total confidence. Only a caption that IS the question - a
    text question with a decorative picture attached - takes the local path,
    which is covered in tests/test_photo_market_questions.py.
    """
    route = respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "42"}}]})
    )
    engine = AIEngine()
    await engine.start()
    try:
        res = await engine.answer_question(
            text="47 * 83",  # would normally be solved locally in <1ms
            answer_style="giveaway",
            primary_provider="groq",
            primary_model="qwen/qwen3.8-27b",
            provider_keys={"groq": "k"},
            local_math_enabled=True,
            image=PNG,
        )
        assert res.provider != "local_math"
        assert route.called
    finally:
        await engine.close()
