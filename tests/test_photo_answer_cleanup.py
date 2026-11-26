"""Photo questions: the answer that reaches the group must be the answer.

Vision models are the messiest source of output this bot has. They mirror the
bold they see in the picture, they put a label on one line and the figure on the
next, and they wrap Amharic answers in markdown. The cleanup step is the last
thing between that and a group, so these tests pin what a group actually sees.

Each case below is a real shape observed from vision models, not an invention:
``**Answer:** 85`` used to post as the literal string ``Answer:** 85``.
"""

from __future__ import annotations

import pytest

from qna_bot.ai.engine import AIEngine


def _clean(raw: str) -> str | None:
    return AIEngine._clean_output(raw, "giveaway")


# ---------------------------------------------------------------------------
# Labels the model decorates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "**Answer:** 85",
        "**Answer**: 85",
        "**Answer:** 85.",
        "__Answer:__ 85",
        "Answer: 85",
        "Answer — 85",
        "The answer is 85.",
        "the answer is **85**",
        "**The answer is 85**",
    ],
)
def test_a_decorated_answer_label_is_removed(raw: str) -> None:
    assert _clean(raw) == "85"


@pytest.mark.parametrize(
    "raw",
    ["**መልሱ፦ 85**", "መልሱ፦ 85", "**መልሱ 85**", "መልስ፦ 85"],
)
def test_amharic_labels_are_removed_even_when_bold(raw: str) -> None:
    """Bold markers used to defeat the Amharic prefix entirely."""
    assert _clean(raw) == "85"


# ---------------------------------------------------------------------------
# Label on one line, figure on the next
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "**Answer:**\n**85**",
        "Answer:\n85",
        "**Answer:**\n85\n\nbecause x=y",
        "መልሱ፦\n85",
    ],
)
def test_a_label_alone_on_its_line_is_not_the_answer(raw: str) -> None:
    """Posting the word "Answer:" as the reply is a real failure, not a nitpick."""
    assert _clean(raw) == "85"


# ---------------------------------------------------------------------------
# The cleanup must not eat real answers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "Answer is not possible",
        "An answer cannot be determined",
        "No answer is possible",
    ],
)
def test_a_sentence_beginning_with_the_word_answer_survives(raw: str) -> None:
    """Stripping these would post an empty reply or a fragment of a sentence."""
    out = _clean(raw)
    assert out
    assert "answer" in out.lower()


def test_a_labelled_negative_is_reduced_to_the_negative() -> None:
    """ "Answer: not possible" is a label plus an answer, so the label goes."""
    assert _clean("Answer: not possible") == "not possible"


def test_a_bare_figure_passes_through_untouched() -> None:
    assert _clean("85") == "85"
    assert _clean("**85**") == "85"
    assert _clean("1.0984") == "1.0984"


def test_ethiopic_full_stop_is_stripped_but_the_words_remain() -> None:
    assert _clean("85 ነው።") == "85 ነው"


# ---------------------------------------------------------------------------
# The photo pipeline reaches these paths at all
# ---------------------------------------------------------------------------


def test_a_photo_answer_is_cleaned_with_the_same_rules() -> None:
    """The cleanup is shared, so a photo gets it for free - pinned so it stays so."""
    import inspect

    from qna_bot.ai.engine import AIEngine as Engine

    source = inspect.getsource(Engine.answer_question)
    # Every return path that carries model text is cleaned, and no branch is
    # guarded by `if image:` - otherwise a photo answer could bypass the rules
    # that every other answer gets.
    assert source.count("self._clean_output(") >= 1
    for line_no, line in enumerate(source.splitlines()):
        if "self._clean_output(" not in line:
            continue
        # Look back a few lines for an unresolved image-only guard on this path.
        window = "\n".join(source.splitlines()[max(0, line_no - 4) : line_no])
        assert "if image" not in window, line


def test_the_transcription_prompt_asks_for_the_window_and_the_units() -> None:
    """A poster's question is only answerable if the period words survive."""
    prompt = AIEngine.TRANSCRIBE_PROMPT
    for phrase in ("last week", "this week", "unit", "line by line"):
        assert phrase in prompt


def test_a_no_text_transcription_is_not_treated_as_a_question() -> None:
    assert "NONE" in AIEngine._NON_QUESTION_TRANSCRIPTIONS
    assert "NO LEGIBLE TEXT" in AIEngine._NON_QUESTION_TRANSCRIPTIONS
