"""The answer is given in the most appropriate language, not blindly mirrored.

This reverses an earlier rule ("always answer in the language the question was
asked in, never translate"). That rule was wrong for definitions: asked in
Amharic what FIFA stands for, the correct answer is the French expansion, and
refusing to "translate" produced a useless reply.

The policy is now: ordinary facts and explanations follow the question's
language; names, acronyms, institutions and terms of art are given in the
language they are formally written in. This file pins the rule and the cases
that must not regress.
"""

from __future__ import annotations

import pytest

from qna_bot.ai.engine import AIEngine
from qna_bot.ai.language import (
    AMHARIC,
    AMHARIC_GUIDANCE,
    ANSWER_IN_LANGUAGE_INSTRUCTION,
    ENGLISH,
    detect_language,
)

#: The case the user actually asked for: an all-Amharic question about FIFA.
FIFA_AMHARIC = "FIFA ሲተነተን ምን ማለት ነው"
FIFA_EXPANSION = "Fédération Internationale de Football Association"


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------


def test_rule_is_appropriate_language_not_mirroring() -> None:
    rule = ANSWER_IN_LANGUAGE_INSTRUCTION
    assert "best fits the ANSWER" in rule
    # The old blanket rule must be gone: it is what forced a bad answer.
    assert "Always answer in the same language" not in rule
    assert "Never translate" not in rule


def test_rule_names_the_fifa_case_explicitly() -> None:
    """Without the worked example the model falls back to mirroring."""
    assert FIFA_EXPANSION in ANSWER_IN_LANGUAGE_INSTRUCTION
    assert "what FIFA stands for" in ANSWER_IN_LANGUAGE_INSTRUCTION


def test_rule_forbids_restating_the_question() -> None:
    assert "repeating or restating the question" in ANSWER_IN_LANGUAGE_INSTRUCTION


def test_rule_forbids_transliterating_into_ethiopic() -> None:
    # "ፊፋ" is not readable and is not what a host would check against.
    assert "transliterate a foreign name into Ethiopic" in ANSWER_IN_LANGUAGE_INSTRUCTION


def test_rule_keeps_ordinary_answers_in_the_question_language() -> None:
    assert "Amharic question -> Amharic answer" in ANSWER_IN_LANGUAGE_INSTRUCTION


# ---------------------------------------------------------------------------
# It reaches the prompt for every question shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Capital of Kenya?",
        "የኢትዮጵያ ዋና ከተማ ማን ነው",
        FIFA_AMHARIC,
        "what is FIFA",
        "2% of a 5000 account",
        "EURUSD ሲተነተን ምን ማለት ነው",
        "",
    ],
)
def test_rule_is_on_every_prompt(text: str) -> None:
    prompt = AIEngine._build_system_prompt("giveaway", text)
    assert ANSWER_IN_LANGUAGE_INSTRUCTION in prompt


@pytest.mark.parametrize("style", ["giveaway", "short", "detailed"])
def test_rule_applies_to_every_answer_style(style: str) -> None:
    prompt = AIEngine._build_system_prompt(style, FIFA_AMHARIC)
    assert ANSWER_IN_LANGUAGE_INSTRUCTION in prompt


def test_amharic_guidance_no_longer_mandates_amharic_everything() -> None:
    """The Amharic block must not contradict the new rule."""
    assert "never transliterated Latin letters" not in AMHARIC_GUIDANCE
    # It should defer to the answer's subject matter.
    assert "answer's own subject matter" in AMHARIC_GUIDANCE


def test_amharic_guidance_treats_metn_as_definitions() -> None:
    # "ሲተነተን" (interpreted/commented on) - a "what does X mean" shape.
    assert "definition or expansion of X" in AMHARIC_GUIDANCE
    assert "FIFA ሲተነተን ምን ማለት ነው" in AMHARIC_GUIDANCE


# ---------------------------------------------------------------------------
# Detection is unchanged: this is about the ANSWER language, not detection
# ---------------------------------------------------------------------------


def test_the_fifa_question_is_still_detected_as_amharic() -> None:
    assert detect_language(FIFA_AMHARIC) == AMHARIC


def test_english_question_is_still_english() -> None:
    assert detect_language("what does FIFA stand for") == ENGLISH


# ---------------------------------------------------------------------------
# The two failure modes this must not reintroduce
# ---------------------------------------------------------------------------


def test_prompt_does_not_demand_amharic_for_every_answer() -> None:
    prompt = AIEngine._build_system_prompt("giveaway", FIFA_AMHARIC)
    assert "Answer in Amharic using the Ethiopic script, never transliterated" not in prompt


def test_prompt_still_asks_for_a_bare_answer() -> None:
    """The 48-token giveaway cap depends on this staying true."""
    prompt = AIEngine._build_system_prompt("giveaway", FIFA_AMHARIC)
    assert "Keep the answer to the bare fact" in prompt
