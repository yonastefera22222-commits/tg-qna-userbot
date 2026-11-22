"""Emoji-heavy giveaway questions: they must be understood, not mangled."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from qna_bot.ai.engine import AIEngine
from qna_bot.ai.math_solver import try_solve_math
from qna_bot.ai.text_cleanup import (
    collapse_emoji_runs,
    has_text_content,
    normalize_spaces,
    prepare_incoming,
    strip_emoji,
    strip_leading_emoji,
)
from qna_bot.storage.database import Database, GroupConfig, SecretCipher
from qna_bot.userbot.filters import should_evaluate_message
from qna_bot.userbot.manager import UserbotManager

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def _group(**kwargs) -> GroupConfig:
    group = GroupConfig(chat_id=1, title="Emoji Quiz", enabled=True)
    group.detection_mode = "giveaway_aggressive"
    for key, value in kwargs.items():
        setattr(group, key, value)
    return group


# ---------------------------------------------------------------------------
# Multi-codepoint emoji must not leave broken fragments behind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("🎯 47 * 83 ✅", " 47 * 83 "),
        ("47 * 83 👍🏽", "47 * 83 "),            # skin tone
        ("👨‍👩‍👧 47 * 83", " 47 * 83"),           # ZWJ family
        ("47 * 83 ❤️", "47 * 83 "),              # variation selector
        ("🇪🇹 የኢትዮጵያ", " የኢትዮጵያ"),           # flag = two regional indicators
        ("Nairobi 🇰🇪", "Nairobi "),
        ("47*83™", "47*83"),
    ],
)
def test_emoji_are_removed_whole(text: str, expected: str) -> None:
    assert strip_emoji(text) == expected


def test_keycap_digits_become_the_digit_not_nothing() -> None:
    """2️⃣ is digit + variation selector + combining keycap: the 2 must survive."""
    assert strip_emoji("2️⃣ + 3️⃣") == "2 + 3"


def test_invisible_bidi_and_zero_width_marks_are_removed() -> None:
    assert strip_emoji("\u200f47 * 83\u200e") == "47 * 83"
    assert strip_emoji("\u202e47 * 83") == "47 * 83"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("47\u00a0*\u00a083", "47 * 83"),   # no-break spaces
        ("47\u3000*\u300083", "47 * 83"),   # ideographic spaces
        ("47\r\n*\u202f83", "47\n* 83"),   # CR dropped, narrow nbsp normalised
    ],
)
def test_exotic_spaces_are_normalised(text: str, expected: str) -> None:
    assert normalize_spaces(text) == expected


@pytest.mark.parametrize("text", ["🔥🔥🔥", "🇪🇹", "✅", "🎉🎊🎈", "🔥 🔥"])
def test_emoji_only_text_has_no_content(text: str) -> None:
    assert has_text_content(text) is False


def test_collapse_shortens_spam_but_keeps_text() -> None:
    assert collapse_emoji_runs("Nairobi 🔥🔥🔥🔥🔥") == "Nairobi 🔥🔥"
    assert collapse_emoji_runs("🔥🔥🔥 Capital of Kenya? 🔥🔥🔥") == "🔥🔥 Capital of Kenya? 🔥🔥"


def test_leading_emoji_is_dropped_only_when_text_follows() -> None:
    assert strip_leading_emoji("🎯 Nairobi") == "Nairobi"
    assert strip_leading_emoji("🇰🇪") == "🇰🇪", "an emoji answer may BE the answer"


# ---------------------------------------------------------------------------
# Arithmetic decorated with emoji takes the instant local path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("47 * 83 ✅", "3901"),
        ("🎯 47 * 83 🤔", "3901"),
        ("🔥🔥🔥 47 * 83 🔥🔥🔥", "3901"),
        ("2️⃣ + 3️⃣", "5"),                 # keycap digits
        ("What is 5️⃣ × 4️⃣?", "20"),
        ("1️⃣2️⃣ × 2️⃣", "24"),
        ("47\u00a0*\u00a083", "3901"),     # no-break spaces
        ("\u200f47 * 83", "3901"),         # RTL mark
        ("🎯 ስንት ነው 43 + 92 🔥", "135"),   # Amharic + emoji
    ],
)
def test_emoji_decorated_math_is_solved_locally(text: str, expected: str) -> None:
    assert try_solve_math(text) == expected


@pytest.mark.parametrize("text", ["🔥🔥🔥", "✅✅", "🇪🇹"])
def test_emoji_alone_is_not_math(text: str) -> None:
    assert try_solve_math(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("🎯 47 * 83 ✅", " 47 * 83 "),
        ("\u200f47\u00a0* 83 🔥", "47 * 83 "),
        ("2️⃣ + 3️⃣", "2 + 3"),
    ],
)
def test_prepare_incoming_does_the_full_cleanup(text: str, expected: str) -> None:
    assert prepare_incoming(text) == expected


# ---------------------------------------------------------------------------
# Pre-filter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["🔥🔥🔥", "🇪🇹", "✅✅✅", "🎉🎊🎈", "🔥 🔥 🔥"])
def test_emoji_only_messages_are_skipped(text: str) -> None:
    decision = should_evaluate_message(text, _group())
    assert decision.should_process is False
    assert decision.reason == "non_text"


@pytest.mark.parametrize(
    "text",
    [
        "🇪🇹 Ethiopia?",
        "Which planet is closest to the sun? 🌞🪐✨",
        "Q1️⃣: Capital of Kenya",
        "ማን ነው? 🇪🇹🔥",
        "What is the capital of Kenya? 🇰🇪🔥🔥",
    ],
)
def test_decorated_questions_still_pass(text: str) -> None:
    assert should_evaluate_message(text, _group()).should_process is True


def test_emoji_decorated_math_is_reported_as_math() -> None:
    decision = should_evaluate_message("🎯 47 * 83 ✅", _group())
    assert decision.reason == "math_detected"


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Nairobi 🇰🇪", "Nairobi 🇰🇪"),            # a natural answer keeps its flag
        ("🎯 Nairobi", "Nairobi"),                  # decorative prefix dropped
        ("Nairobi 🔥🔥🔥🔥🔥", "Nairobi 🔥🔥"),         # spam collapsed
        ("🇰🇪", "🇰🇪"),                             # emoji-only answer is the answer
        ("The answer is: Nairobi 🇰🇪", "Nairobi 🇰🇪"),
        ("መልስ፦ አዲስ አበባ 🌍", "አዲስ አበባ 🌍"),
    ],
)
def test_answers_are_cleaned_but_not_stripped_of_meaning(raw: str, expected: str) -> None:
    assert AIEngine._clean_output(raw, "giveaway") == expected


@pytest.mark.parametrize("raw", ["SKIP", "SKIP 🔥", "Skip ✅", "SKIP 🎯🔥", "SKIP\n🔥"])
def test_decorated_skip_is_still_a_skip(raw: str) -> None:
    """A decorated SKIP used to be posted into the group as an answer."""
    assert AIEngine._clean_output(raw, "giveaway") is None


def test_prompt_tells_the_model_emoji_are_decoration() -> None:
    prompt = AIEngine._build_system_prompt("giveaway", "🎯 47 * 83 ✅")
    assert "never repeat" in prompt.lower()


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def _event(chat_id: int, text: str, mid: int) -> MagicMock:
    event = MagicMock()
    event.out = False
    event.is_private = False
    event.chat_id = chat_id
    event.id = mid
    event.sender_id = 555
    event.raw_text = text
    event.message.from_id = None
    event.message.photo = None
    event.message.document = None
    event.reply = AsyncMock()
    event.respond = AsyncMock()
    return event


async def _run(tmp_path, text: str, model_reply: str, mid: int = 1):
    db = Database(tmp_path / "b.db", SecretCipher.from_env_or_file(None, tmp_path / ".k"))
    await db.connect()
    await db.set_provider_key("groq", "k")
    await db.set_setting("primary_provider", "groq")
    await db.set_setting("primary_model", "openai/gpt-oss-120b")
    ai = AIEngine()
    await ai.start()
    try:
        manager = UserbotManager(db, ai)
        group = await db.upsert_group_metadata(-100, "Emoji Quiz")
        group.enabled = True
        group.detection_mode = "giveaway_aggressive"
        group.cooldown_seconds = 0
        await db.save_group(group)
        await manager.reload_cache()
        manager._admin_cache[-100] = (time.monotonic(), set(), False)

        with respx.mock:
            route = respx.post(GROQ_URL).mock(
                return_value=httpx.Response(
                    200, json={"choices": [{"message": {"content": model_reply}}]}
                )
            )
            event = _event(-100, text, mid)
            await manager._on_new_message(event)
            sent = event.reply.await_args.args[0] if event.reply.await_args else None
            seen = (
                json.loads(route.calls[0].request.content)["messages"][1]["content"]
                if route.called
                else None
            )
        return sent, seen
    finally:
        await ai.close()
        await db.close()


@pytest.mark.asyncio
async def test_emoji_math_question_is_answered_without_the_api(tmp_path) -> None:
    sent, seen = await _run(tmp_path, "🎯 47 * 83 ✅🔥🔥🔥", "3901")
    assert sent == "3901"
    assert seen is None, "decorated arithmetic must not cost an API call"


@pytest.mark.asyncio
async def test_emoji_decorated_question_reaches_the_model_clean(tmp_path) -> None:
    sent, seen = await _run(tmp_path, "What is the capital of Kenya? 🇰🇪🔥🔥", "Nairobi 🇰🇪")
    assert sent == "Nairobi 🇰🇪"
    assert seen == "What is the capital of Kenya?"


@pytest.mark.asyncio
async def test_emoji_only_message_is_never_sent_to_the_model(tmp_path) -> None:
    sent, seen = await _run(tmp_path, "🔥🔥🔥", "Nairobi")
    assert sent is None
    assert seen is None
