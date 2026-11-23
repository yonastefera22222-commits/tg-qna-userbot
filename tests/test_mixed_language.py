"""Mixed English/Amharic questions.

These groups code-switch constantly. "FIFA ሲተነተን ምን ማለት ነው" is one sentence in
two scripts, and it has to be treated as an Amharic question with an English
term inside - not as English, and not as something to translate.
"""

from __future__ import annotations

import pytest

from qna_bot.ai.engine import AMHARIC_TOKEN_HEADROOM, AIEngine, _TruncatedAnswerError
from qna_bot.ai.forex import is_forex_question
from qna_bot.ai.language import (
    AMHARIC,
    AMHARIC_GUIDANCE,
    ENGLISH,
    MIXED_AMHARIC_THRESHOLD,
    detect_language,
    ethiopic_ratio,
    is_mixed,
    prompt_guidance,
)
from qna_bot.storage.database import GroupConfig
from qna_bot.userbot.filters import should_evaluate_message as filter_message

#: The exact question that prompted this work.
FIFA = "FIFA ሲተነተን ምን ማለት ነው"


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_fifa_question_is_amharic() -> None:
    assert ethiopic_ratio(FIFA) > MIXED_AMHARIC_THRESHOLD
    assert detect_language(FIFA) == AMHARIC
    assert is_mixed(FIFA) is True


@pytest.mark.parametrize(
    "text",
    [
        FIFA,
        "explain VAR ማለት ምን ነው",
        "what is FIFA በአማርኛ",
        "ሲተነተን ምን ማለት ነው FIFA",
        "EURUSD ሲተነተን ምን ማለት ነው",
        "USDJPY ለምን ይወድቃል",
    ],
)
def test_mixed_questions_are_answered_as_amharic(text: str) -> None:
    assert detect_language(text) == AMHARIC


def test_mostly_latin_question_stays_english() -> None:
    """A Latin question that quotes one Amharic word is still an English ask."""
    assert detect_language("what does ሲተነተን mean in FIFA") == ENGLISH


@pytest.mark.parametrize(
    "text", ["Capital of Ethiopia?", "43 + 92", "who is the president of Kenya", ""]
)
def test_pure_latin_is_not_mixed(text: str) -> None:
    assert is_mixed(text) is False
    assert detect_language(text) == ENGLISH


def test_mixed_but_latin_led_is_not_flagged_amharic() -> None:
    assert is_mixed("what does ሲተነተን mean in FIFA") is True
    assert detect_language("what does ሲተነተን mean in FIFA") == ENGLISH


# ---------------------------------------------------------------------------
# Prompt guidance
# ---------------------------------------------------------------------------


def test_mixed_guidance_keeps_english_terms() -> None:
    guidance = prompt_guidance(FIFA)
    assert "MIXES English and Amharic" in guidance
    # An acronym written in English stays Latin script - "ፊፋ" is unreadable -
    # but its formal expansion is the better answer, not a translation.
    assert "exactly as it was written" in guidance
    # The no-transliteration rule is in the always-on block, so check there.
    from qna_bot.ai.language import ANSWER_IN_LANGUAGE_INSTRUCTION

    assert "transliterate" in ANSWER_IN_LANGUAGE_INSTRUCTION.lower()


def test_mixed_guidance_asks_for_meaning_not_restatement() -> None:
    guidance = prompt_guidance(FIFA)
    assert "ማለት ምን ነው" in guidance
    assert "definition or expansion of X" in guidance


def test_prompt_includes_mixed_rules_for_the_fifa_question() -> None:
    prompt = AIEngine._build_system_prompt("giveaway", FIFA)
    assert AMHARIC_GUIDANCE in prompt
    assert "MIXES English and Amharic" in prompt


def test_pure_english_prompt_has_no_amharic_rules() -> None:
    prompt = AIEngine._build_system_prompt("giveaway", "Capital of Kenya?")
    assert AMHARIC_GUIDANCE not in prompt


# ---------------------------------------------------------------------------
# Truncation: Ethiopic costs ~1 token per character
# ---------------------------------------------------------------------------


def test_amharic_answers_get_token_headroom() -> None:
    # 48 tokens is a comfortable English one-word answer and a truncated
    # Amharic sentence; the ceiling must be wider for Amharic.
    assert AMHARIC_TOKEN_HEADROOM > 0


@pytest.mark.asyncio
async def test_truncated_answer_is_retried_then_answered() -> None:
    """A cut-off fragment must never be posted as a complete answer."""
    calls: list[int] = []

    class _Engine(AIEngine):
        async def _call_provider(self, **kwargs):  # type: ignore[override]
            calls.append(kwargs["max_tokens"])
            if len(calls) == 1:
                raise _TruncatedAnswerError("hit the ceiling")
            return "አዲስ አበባ"

    engine = _Engine()
    try:
        result = await engine.answer_question(
            text=FIFA,
            answer_style="giveaway",
            primary_provider="groq",
            primary_model="openai/gpt-oss-120b",
            provider_keys={"groq": "k"},
            local_math_enabled=False,
        )
        assert result.answer == "አዲስ አበባ"
        assert len(calls) == 2, "must retry once with more room"
        assert calls[1] > calls[0]
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_amharic_request_uses_the_wider_ceiling() -> None:
    seen: list[int] = []

    class _Engine(AIEngine):
        async def _call_provider(self, **kwargs):  # type: ignore[override]
            seen.append(kwargs["max_tokens"])
            return "መልስ"

    engine = _Engine()
    try:
        await engine.answer_question(
            text=FIFA,
            answer_style="giveaway",
            primary_provider="groq",
            primary_model="openai/gpt-oss-120b",
            provider_keys={"groq": "k"},
            local_math_enabled=False,
        )
        assert seen[0] == 48 + AMHARIC_TOKEN_HEADROOM
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_english_request_keeps_the_48_token_cap() -> None:
    """The visible giveaway cap must not grow for English."""
    seen: list[int] = []

    class _Engine(AIEngine):
        async def _call_provider(self, **kwargs):  # type: ignore[override]
            seen.append(kwargs["max_tokens"])
            return "Nairobi"

    engine = _Engine()
    try:
        await engine.answer_question(
            text="Capital of Kenya?",
            answer_style="giveaway",
            primary_provider="groq",
            primary_model="openai/gpt-oss-120b",
            provider_keys={"groq": "k"},
            local_math_enabled=False,
        )
        assert seen[0] == 48
    finally:
        await engine.close()


# ---------------------------------------------------------------------------
# Filter, and Forex overlap
# ---------------------------------------------------------------------------


def test_mixed_question_passes_the_filter() -> None:
    group = GroupConfig(chat_id=-100, title="Giveaway Arena", enabled=True)
    group.detection_mode = "giveaway_aggressive"
    assert filter_message(FIFA, group).should_process is True


@pytest.mark.parametrize(
    "text",
    [
        "ጎልድ ማለት ምን ነው",
        "ሌቨሬጅ ስንት ነው",
        "ፒፕ ምን ማለት ነው",
        "ፎሬክስ ምን ነው",
    ],
)
def test_amharic_forex_terms_are_recognised(text: str) -> None:
    """Amharic traders write Forex terms phonetically: ጎልድ is 'gold'."""
    assert is_forex_question(text) is True


def test_mixed_general_question_is_not_forex() -> None:
    assert is_forex_question(FIFA) is False
