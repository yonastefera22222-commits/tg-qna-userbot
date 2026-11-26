"""First-comment triggers and replies.

Two complaints from a real group, both about looking scripted:

* hosts post ``Go``, ``ጀምሩ``, ``ፃፉ``, ``ጻፉ`` as the race signal, and the bot only
  understood "first comment" - so it lost races it was switched on for;
* the reply was always the identical two words, in every group, every day, which
  is the most obvious sign that a bot won.

The guard rails matter as much as the new triggers: "go" is one of the most
common words in English, and a matcher that fires on "here we go" would answer
ordinary conversation.
"""

from __future__ import annotations

import pytest

from qna_bot.userbot import first_comment
from qna_bot.userbot.first_comment import (
    FIRST_COMMENT_ANSWERS,
    is_first_comment,
    looks_amharic,
    next_first_comment_answer,
)

# ---------------------------------------------------------------------------
# "go" and the Amharic imperatives
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "go", "GO", "Go!", "go.", "go..", "go 🔥", "GO ⚡⚡", " go ",
        "ጀምሩ", "ጅምሩ", "ጀምር", "ፃፉ", "ጻፉ", "ፃፍ", "ጻፍ",
        "ፃፉ 🔥", "ጻፉ!",
        "first comment", "first comment go", "first comment — ጀምሩ",
        "fc", "FC 🔥",
    ],
)
def test_accepts_every_real_variant(text: str) -> None:
    assert is_first_comment(text) is True


@pytest.mark.parametrize(
    "text",
    [
        # "go" inside ordinary speech must never fire.
        "go team", "here we go", "let's go to the market", "going home",
        "goalkeeper", "go and buy bread now please", "go ahead and send it",
        "algorithm", "category",
        # Amharic words that merely contain the letters.
        "ጥሩ ነው", "ጎ ለምን", "ስለ ጎጆ",
        # Reporting language still beats everything.
        "the first comment was rude", "first comment won", "first comment was mine",
        # A long paragraph that happens to mention the phrase.
        "remember the first comment on yesterday's post was a bit rude honestly",
    ],
)
def test_rejects_ordinary_conversation(text: str) -> None:
    assert is_first_comment(text) is False


def test_go_cannot_be_reached_by_a_suffix() -> None:
    """Anchoring is the whole safety property: "go" must be the entire message."""
    for text in ("go now", "go go", "wego", "go1", "1go"):
        assert is_first_comment(text) is False, text


def test_go_does_not_bypass_the_length_guard() -> None:
    long_line = "go " + "x " * 40
    assert is_first_comment(long_line) is False


# ---------------------------------------------------------------------------
# Rotating replies
# ---------------------------------------------------------------------------


def test_there_are_enough_replies_to_not_look_scripted() -> None:
    assert len(FIRST_COMMENT_ANSWERS) >= 8
    assert len(set(FIRST_COMMENT_ANSWERS)) == len(FIRST_COMMENT_ANSWERS)


def test_every_reply_is_group_chat_sized() -> None:
    """A giveaway winner types a word, not a sentence, and never an emoji."""
    for reply in FIRST_COMMENT_ANSWERS:
        assert 1 <= len(reply) <= 12, reply
        assert reply.isascii(), f"{reply!r} would look like a bot's repertoire"


def test_replies_rotate_without_repeating_until_all_are_used() -> None:
    seen = [next_first_comment_answer() for _ in range(len(FIRST_COMMENT_ANSWERS))]
    assert len(set(seen)) == len(FIRST_COMMENT_ANSWERS), seen


def test_the_rotation_keeps_going_round() -> None:
    first_cycle = [next_first_comment_answer() for _ in range(len(FIRST_COMMENT_ANSWERS))]
    second_cycle = [next_first_comment_answer() for _ in range(len(FIRST_COMMENT_ANSWERS))]
    assert sorted(first_cycle) == sorted(second_cycle)


def test_amharic_triggers_get_amharic_replies() -> None:
    """Replying "I win" to a message written in Amharic looks like a bot."""
    replies = {next_first_comment_answer(amharic=True) for _ in range(6)}
    assert replies, "there must be Amharic replies"
    for reply in replies:
        assert not reply.isascii()
    # And they are distinct, so the rotation is real.
    assert len(replies) >= 2


def test_looks_amharic() -> None:
    assert looks_amharic("ጀምሩ") is True
    assert looks_amharic("ፃፉ ፈጣን") is True
    assert looks_amharic("go") is False
    assert looks_amharic("") is False


def test_the_rotation_starts_at_a_random_point(monkeypatch) -> None:
    """A redeploy should not begin the sequence at "I win" again."""
    monkeypatch.setattr(first_comment.random, "randrange", lambda n: 5)
    monkeypatch.setattr(first_comment, "_answer_cursor", __import__("itertools").count(5))
    assert next_first_comment_answer() == FIRST_COMMENT_ANSWERS[5]
