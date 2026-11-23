"""Amharic (Ethiopic script) support: detection, filters, math, and answers."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from qna_bot.ai.engine import AIEngine
from qna_bot.ai.language import (
    AMHARIC,
    ANSWER_IN_LANGUAGE_INSTRUCTION,
    ENGLISH,
    detect_language,
    ethiopic_ratio,
    has_amharic_numerals,
    parse_amharic_numeral,
    prompt_guidance,
)
from qna_bot.ai.math_solver import to_amharic_numeral, try_solve_math
from qna_bot.storage.database import GroupConfig
from qna_bot.userbot.filters import should_evaluate_message

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

CAPITAL_Q = "የኢትዮጵያ ዋና ከተማ ማን ይባላል?"
CAPITAL_Q_NO_MARK = "ማን ነው የኢትዮጵያ ዋና ከተማ"


def _group(**kwargs) -> GroupConfig:
    group = GroupConfig(chat_id=1, title="Amharic Quiz", enabled=True)
    for key, value in kwargs.items():
        setattr(group, key, value)
    return group


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [CAPITAL_Q, CAPITAL_Q_NO_MARK, "ስንት ነው 43 + 92", "ጥያቄ ፩፦ ማን ነው"],
)
def test_amharic_text_is_detected(text: str) -> None:
    assert detect_language(text) == AMHARIC


@pytest.mark.parametrize("text", ["Capital of Ethiopia?", "43 + 92", "", "Addis Ababa"])
def test_latin_and_numeric_text_is_english(text: str) -> None:
    assert detect_language(text) == ENGLISH


def test_detection_ignores_digits_and_punctuation() -> None:
    """A mostly-numeric Amharic question is still Amharic."""
    assert detect_language("ስንት ነው 43 + 92?") == AMHARIC
    assert ethiopic_ratio("43 + 92") == 0.0


def test_one_english_word_does_not_flip_the_language() -> None:
    assert detect_language("የኢትዮጵያ ዋና ከተማ Addis ናት?") == AMHARIC


# ---------------------------------------------------------------------------
# Ethiopic numerals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("numeral", "value"),
    [("፩", 1), ("፲", 10), ("፵፫", 43), ("፪፻", 200), ("፲፻", 1000), ("፼", 10_000),
     ("፩፼፪፻", 10_200)],
)
def test_parse_amharic_numerals(numeral: str, value: int) -> None:
    assert parse_amharic_numeral(numeral) == value


@pytest.mark.parametrize(
    "value", [1, 10, 43, 99, 100, 200, 250, 999, 1000, 1234, 10_000, 10_200]
)
def test_numerals_round_trip(value: int) -> None:
    assert parse_amharic_numeral(to_amharic_numeral(value)) == value


def test_invalid_numeral_returns_none() -> None:
    assert parse_amharic_numeral("43") is None
    assert parse_amharic_numeral("") is None


def test_has_amharic_numerals() -> None:
    assert has_amharic_numerals("፵፫ + ፶፪") is True
    assert has_amharic_numerals("43 + 52") is False


# ---------------------------------------------------------------------------
# Local math with Amharic wrappers and numerals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ስንት ነው 43 + 92", "135"),          # prefix "how much is"
        ("43 + 92 ስንት ነው?", "135"),          # suffix
        ("፵፫ + ፶፪ ስንት ነው?", "፺፭"),          # numerals in, numerals out
        ("ስንት ነው ፲፻ ÷ ፬", "፪፻፶"),          # 1000 / 4
        ("ድምር ፳ + ፳ ስንት ነው", "፵"),         # "sum 20 + 20"
        ("12 × 8 ስንት", "96"),                # Latin digits stay Latin
    ],
)
def test_amharic_math_is_solved_locally(text: str, expected: str) -> None:
    assert try_solve_math(text) == expected


def test_amharic_question_is_not_mistaken_for_math() -> None:
    assert try_solve_math(CAPITAL_Q_NO_MARK) is None
    assert try_solve_math("2018 ዓ.ም") is None  # a year is not arithmetic


# ---------------------------------------------------------------------------
# Pre-filter: Amharic questions must reach the AI
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [CAPITAL_Q, CAPITAL_Q_NO_MARK, "ዋና ከተማዋ ማን ይባላል"])
def test_amharic_question_passes_balanced_mode(text: str) -> None:
    """Amharic has no Latin keywords, so these were skipped before."""
    decision = should_evaluate_message(text, _group(detection_mode="balanced"))
    assert decision.should_process is True


def test_amharic_math_takes_the_instant_path() -> None:
    decision = should_evaluate_message("ስንት ነው 43 + 92", _group())
    assert decision.should_process is True
    assert decision.reason == "math_detected"


def test_amharic_chatter_is_still_skipped() -> None:
    decision = should_evaluate_message("እንኳን ደህና መጣህ", _group(detection_mode="balanced"))
    assert decision.should_process is False


def test_amharic_quiz_prefix_is_a_quiz_format() -> None:
    decision = should_evaluate_message(
        "ጥያቄ ፩፦ የኢትዮጵያ ዋና ከተማ", _group(detection_mode="strict")
    )
    assert decision.should_process is True


# ---------------------------------------------------------------------------
# Prompting and answer cleaning
# ---------------------------------------------------------------------------


def test_language_rule_is_on_every_prompt() -> None:
    """The language rule is stable so the provider's prompt cache keeps hitting.

    It is no longer "answer in the question's language" - that produced useless
    answers to "what does FIFA stand for" - but it is still one constant block.
    """
    prompt = AIEngine._build_system_prompt("giveaway", "Capital of Kenya?")
    assert ANSWER_IN_LANGUAGE_INSTRUCTION in prompt
    assert "best fits the ANSWER" in prompt


def test_amharic_question_adds_script_guidance() -> None:
    english = AIEngine._build_system_prompt("giveaway", "Capital of Kenya?")
    amharic = AIEngine._build_system_prompt("giveaway", CAPITAL_Q)
    assert "Ethiopic script" in amharic
    assert "Ethiopic script" not in english


def test_prompt_prefix_stays_stable_for_cache_hits() -> None:
    """A varying per-language prompt would break the provider's prompt cache."""
    english = AIEngine._build_system_prompt("giveaway", "Who is it?")
    amharic = AIEngine._build_system_prompt("giveaway", CAPITAL_Q)
    assert english.splitlines()[:2] == amharic.splitlines()[:2]


def test_guidance_only_for_amharic() -> None:
    assert prompt_guidance(CAPITAL_Q)
    assert prompt_guidance("Capital of Kenya?") == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("መልስ፦ አዲስ አበባ", "አዲስ አበባ"),   # "answer:" label stripped
        ("መልሱ: አዲስ አበባ፡", "አዲስ አበባ"),  # label + Ethiopic word space
        ("አዲስ አበባ።", "አዲስ አበባ"),          # Ethiopic full stop
        ("“አዲስ አበባ”", "አዲስ አበባ"),        # curly quotes
        ("አዲስ አበባ", "አዲስ አበባ"),
    ],
)
def test_amharic_answers_are_cleaned(raw: str, expected: str) -> None:
    assert AIEngine._clean_output(raw, "giveaway") == expected


def test_skip_still_works_for_amharic_prompts() -> None:
    assert AIEngine._clean_output("SKIP", "giveaway") is None


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_amharic_question_is_answered_in_amharic() -> None:
    route = respx.post(GROQ_URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "አዲስ አበባ።"}}]}
        )
    )
    engine = AIEngine()
    await engine.start()
    try:
        result = await engine.answer_question(
            text=CAPITAL_Q,
            answer_style="giveaway",
            primary_provider="groq",
            primary_model="openai/gpt-oss-120b",
            provider_keys={"groq": "k"},
            local_math_enabled=True,
        )
        assert result.answered is True
        assert result.answer == "አዲስ አበባ"
        assert result.language == AMHARIC

        body = json.loads(route.calls[0].request.content)
        system = body["messages"][0]["content"]
        assert "Ethiopic script" in system, "the model must be told to answer in Amharic"
        assert body["messages"][1]["content"] == CAPITAL_Q, "question sent untranslated"
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_amharic_math_never_touches_the_network() -> None:
    route = respx.post(GROQ_URL).mock(return_value=httpx.Response(500))
    engine = AIEngine()
    await engine.start()
    try:
        result = await engine.answer_question(
            text="፵፫ + ፶፪ ስንት ነው?",
            answer_style="giveaway",
            primary_provider="groq",
            primary_model="openai/gpt-oss-120b",
            provider_keys={"groq": "k"},
            local_math_enabled=True,
        )
        assert result.answer == "፺፭"
        assert result.provider == "local_math"
        assert not route.called, "arithmetic must be answered locally in <1ms"
    finally:
        await engine.close()


@pytest.mark.asyncio
@respx.mock
async def test_english_answer_is_not_affected() -> None:
    route = respx.post(GROQ_URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "The answer is: Nairobi."}}]}
        )
    )
    engine = AIEngine()
    await engine.start()
    try:
        result = await engine.answer_question(
            text="Capital of Kenya?",
            answer_style="giveaway",
            primary_provider="groq",
            primary_model="openai/gpt-oss-120b",
            provider_keys={"groq": "k"},
            local_math_enabled=True,
        )
        assert result.answer == "Nairobi"
        assert result.language == ENGLISH
        assert "Ethiopic script" not in json.loads(route.calls[0].request.content)["messages"][0]["content"]
    finally:
        await engine.close()
