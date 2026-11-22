"""Tests for model-retirement drift: a dead model ID must never look like a bad key."""

from __future__ import annotations

import httpx
import pytest
import respx

from qna_bot.ai.engine import AIEngine, _is_model_not_found
from qna_bot.ai.providers import PROVIDERS, retired_model_replacement

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

#: The literal body Groq returned to the owner for the retired Llama model.
GROQ_MODEL_GONE = {
    "error": {
        "message": "The model `llama-3.3-70b-versatile` does not exist or you do not "
        "have access to it.",
        "type": "invalid_request_error",
        "code": "model_not_found",
    }
}


def test_groq_default_is_a_live_free_model() -> None:
    """Groq retired every Llama model from the free tier on 2026-08-16."""
    groq = PROVIDERS["groq"]
    assert groq.default_model == "openai/gpt-oss-120b"
    preset_ids = {p.model_id for p in groq.presets}
    for dead in ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"):
        assert dead not in preset_ids
    # The free plan's actual model set.
    assert {"openai/gpt-oss-120b", "openai/gpt-oss-20b"} <= preset_ids


def test_gemini_defaults_avoid_shut_down_2x_models() -> None:
    gem = PROVIDERS["gemini"]
    preset_ids = {p.model_id for p in gem.presets}
    assert "gemini-2.0-flash" not in preset_ids  # shut down 2026-06-01
    assert all(p.startswith(("gemini-3", "gemma")) for p in preset_ids)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (GROQ_MODEL_GONE, True),
        ({"error": {"code": "model_decommissioned"}}, True),
        ({"error": {"message": "model no longer supported"}}, True),
        ({"error": {"message": "Invalid API Key"}}, False),
        ({"error": {"message": "rate limit exceeded"}}, False),
    ],
)
def test_model_not_found_detection(body: dict, expected: bool) -> None:
    assert _is_model_not_found(str(body)) is expected


def test_retired_model_mapping() -> None:
    assert retired_model_replacement("groq", "llama-3.3-70b-versatile") == "openai/gpt-oss-120b"
    assert retired_model_replacement("groq", "llama-3.1-8b-instant") == "openai/gpt-oss-20b"
    assert retired_model_replacement("groq", "openai/gpt-oss-120b") is None
    assert retired_model_replacement("openai", "whatever") is None


@pytest.mark.asyncio
@respx.mock
async def test_key_test_recovers_from_retired_model() -> None:
    """This is the exact failure the owner hit: valid key, dead model."""
    route = respx.post(GROQ_URL).mock(
        side_effect=[
            httpx.Response(404, json=GROQ_MODEL_GONE),
            httpx.Response(200, json={"choices": [{"message": {"content": "Paris"}}]}),
        ]
    )
    engine = AIEngine()
    await engine.start()
    try:
        ok, _ms, detail = await engine.test_connection(
            "groq", "llama-3.3-70b-versatile", "gsk_still_valid"
        )
        assert ok is True, "a retired model must not be reported as a bad key"
        assert "Paris" in detail
        assert "__MODEL_FALLBACK__:llama-3.3-70b-versatile->openai/gpt-oss-120b" in detail
        assert route.call_count == 2
        # Second request used the live model.
        assert "openai/gpt-oss-120b" in route.calls[-1].request.content.decode()
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_real_bad_key_still_fails_clearly() -> None:
    """A genuinely invalid key must still be reported as a key problem."""
    respx.post(GROQ_URL).mock(
        return_value=httpx.Response(
            401, json={"error": {"message": "Invalid API Key", "code": "invalid_api_key"}}
        )
    )
    engine = AIEngine()
    await engine.start()
    try:
        ok, _ms, detail = await engine.test_connection("groq", "openai/gpt-oss-120b", "gsk_bad")
        assert ok is False
        assert "401" in detail
        assert "__MODEL_FALLBACK__" not in detail
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_runtime_answer_recovers_from_retired_saved_model() -> None:
    """A model saved months ago must not silence every giveaway answer."""
    respx.post(GROQ_URL).mock(
        side_effect=[
            httpx.Response(404, json=GROQ_MODEL_GONE),
            httpx.Response(200, json={"choices": [{"message": {"content": "Nairobi"}}]}),
        ]
    )
    engine = AIEngine()
    await engine.start()
    try:
        res = await engine.answer_question(
            text="Capital of Kenya",
            answer_style="giveaway",
            primary_provider="groq",
            primary_model="llama-3.3-70b-versatile",
            provider_keys={"groq": "gsk_valid"},
            local_math_enabled=False,
        )
        assert res.answered is True
        assert res.answer == "Nairobi"
        assert res.model == "openai/gpt-oss-120b"
    finally:
        await engine.close()
