"""The "first comment" race: instant, local, and never an AI call.

This is a latency feature. A model round-trip loses the race by definition, so
the tests care about two things above all: that the variants a host actually
types are recognised, and that answering them touches no provider.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from qna_bot.ai.engine import AIEngine
from qna_bot.storage.crypto import SecretCipher
from qna_bot.storage.database import Database
from qna_bot.userbot import first_comment
from qna_bot.userbot.first_comment import (
    FIRST_COMMENT_ANSWER,
    FIRST_COMMENT_ANSWERS,
    is_first_comment,
)
from qna_bot.userbot.manager import UserbotManager

CHAT_ID = -1001234567890
SENDER_ID = 4242


# ---------------------------------------------------------------------------
# Recognition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "first comment",
        "First Comment",
        "FIRST COMMENT!",
        "⚡️First Comment⚡️",
        "First Comment 🔥🔥",
        "1st comment",
        "1 st comment",
        "first cmt",
        "first cmnt",
        "first com",
        "first-comment",
        "firstcomment",
        "frst comment",
        "firts comment",
        "comment first",
        "first comment wins",
        "first comment below",
        "drop first comment now",
        "drop your first comment below",
        "just type first comment",
        "1st comment gets it",
        "first comment win",
        "FC",
        "fc",
        "F.C.",
        "f.c",
        "f/c",
        "f-c",
        "ፊርስት ኮመንት",
        "የመጀመሪያ አስተያየት",
    ],
)
def test_recognises_every_variation(text: str) -> None:
    assert is_first_comment(text) is True


@pytest.mark.parametrize(
    "text",
    [
        # Not a race signal: a club, a score, chatter, a question.
        "FC Barcelona won",
        "FC 2-1",
        "hello",
        "first",
        "comments",
        "second comment",
        "What is the capital of Kenya?",
        "47 * 83",
        "who won the match yesterday in the stadium",
        "what is a pip",
        # Sentences ABOUT a first comment, not an instruction to post one.
        "the first comment on yesterday's post was rude",
        "first comment on yesterday was rude",
        "the first comment section is always a mess",
        "I left the first comment on their channel last week and nobody replied",
        "first comment was mine",
        "first comment won last time",
        "",
    ],
)
def test_rejects_prose_and_lookalikes(text: str) -> None:
    assert is_first_comment(text) is False


def test_answer_is_two_clean_words() -> None:
    # No emoji, no punctuation, no full stop: it has to read like a person.
    assert FIRST_COMMENT_ANSWER == "I win"
    assert FIRST_COMMENT_ANSWER.isascii()


def test_trigger_is_cheap() -> None:
    """The matcher runs before every AI decision, so it must stay microseconds."""
    import time

    text = "⚡️First Comment 🔥🔥"
    start = time.perf_counter()
    for _ in range(1000):
        is_first_comment(text)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.2, f"1000 matches took {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# End to end: replies without any AI involvement
# ---------------------------------------------------------------------------


def _event(text: str, *, reply_to: bool = True):
    event = MagicMock()
    event.out = False
    event.is_private = False
    event.chat_id = CHAT_ID
    event.id = 101
    event.sender_id = SENDER_ID
    event.raw_text = text
    event.message.from_id = None
    event.get_sender = AsyncMock()
    sent: list[str] = []
    event.reply = AsyncMock(side_effect=lambda t: sent.append(t))
    event.respond = AsyncMock(side_effect=lambda t: sent.append(t))
    return event, sent


async def _manager(tmp_path, *, enabled: bool = True):
    db = Database(tmp_path / "b.db", SecretCipher.from_env_or_file(None, tmp_path / ".k"))
    await db.connect()
    ai = AIEngine()
    mgr = UserbotManager(db, ai)
    group = await db.upsert_group_metadata(CHAT_ID, "Giveaway Arena")
    group.enabled = True
    group.sender_filter = "all"
    group.first_comment_enabled = enabled
    await db.save_group(group)
    await mgr.reload_cache()
    mgr._global_enabled = True
    return db, ai, mgr


@pytest.mark.asyncio
async def test_first_comment_is_answered_without_calling_ai(tmp_path) -> None:
    db, ai, mgr = await _manager(tmp_path)
    try:
        async def boom(**kwargs):  # pragma: no cover - must never run
            raise AssertionError("the AI must not be called for a first-comment race")

        ai.answer_question = boom  # type: ignore[assignment]

        event, sent = _event("First Comment 🔥")
        await mgr._on_new_message(event)

        # The reply now rotates through a pool of short human lines, so assert it
        # is one of them rather than that it is the old constant.
        assert len(sent) == 1 and sent[0] in FIRST_COMMENT_ANSWERS
        assert mgr.counters["answers_sent"] == 1
        assert any(
            d["outcome"] == "answered" and "first_comment" in str(d.get("detail"))
            for d in mgr.recent_decisions
        )
        await ai.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_abbreviation_wins_the_race(tmp_path) -> None:
    db, ai, mgr = await _manager(tmp_path)
    try:
        ai.answer_question = AsyncMock(side_effect=AssertionError("no AI"))  # type: ignore[assignment]
        event, sent = _event("FC")
        await mgr._on_new_message(event)
        assert len(sent) == 1 and sent[0] in FIRST_COMMENT_ANSWERS
        await ai.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_deduplication_stops_double_posting(tmp_path) -> None:
    db, ai, mgr = await _manager(tmp_path)
    try:
        ai.answer_question = AsyncMock(side_effect=AssertionError("no AI"))  # type: ignore[assignment]
        event, sent = _event("first comment")
        await mgr._on_new_message(event)
        await mgr._on_new_message(event)  # same message id delivered twice
        assert len(sent) == 1, "a duplicate update must not post twice"
        await ai.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cooldown_does_not_block_the_race(tmp_path) -> None:
    """A cooldown is for rate-limiting answers, not for losing a one-shot race."""
    db, ai, mgr = await _manager(tmp_path)
    try:
        group = await db.get_group(CHAT_ID)
        group.cooldown_seconds = 30.0
        await db.save_group(group)
        await mgr.reload_cache()
        mgr._last_reply_ts[CHAT_ID] = 1e12  # just answered something else

        ai.answer_question = AsyncMock(side_effect=AssertionError("no AI"))  # type: ignore[assignment]
        event, sent = _event("1st comment wins")
        await mgr._on_new_message(event)
        assert len(sent) == 1 and sent[0] in FIRST_COMMENT_ANSWERS
        await ai.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_disabled_by_default(tmp_path) -> None:
    """It posts on its own, so it must be opt-in."""
    db, ai, mgr = await _manager(tmp_path, enabled=False)
    try:
        called = AsyncMock()
        ai.answer_question = called  # type: ignore[assignment]
        event, sent = _event("first comment")
        await mgr._on_new_message(event)
        assert sent == [], "disabled groups must not auto-post"
        await ai.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_plain_question_still_goes_to_the_ai(tmp_path) -> None:
    """The fast path must not swallow ordinary questions."""
    db, ai, mgr = await _manager(tmp_path)
    try:
        async def fake_answer(**kwargs):
            from qna_bot.ai.engine import AnswerResult

            return AnswerResult(
                answered=True, answer="Nairobi", provider="groq",
                model="openai/gpt-oss-120b", latency_ms=42,
            )

        ai.answer_question = fake_answer  # type: ignore[assignment]
        event, sent = _event("What is the capital of Kenya?")
        await mgr._on_new_message(event)
        assert sent == ["Nairobi"]
        await ai.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_image_message_is_not_treated_as_a_race_signal(tmp_path) -> None:
    """A photo captioned "first comment" is a picture entry, not a text race.

    The fast path is gated on ``not has_image``; letting an image through would
    post "I win" for every photo a group shares.
    """
    from telethon.tl.types import MessageMediaPhoto

    db, ai, mgr = await _manager(tmp_path)
    try:
        ai.answer_question = AsyncMock(return_value=None)  # type: ignore[assignment]
        event, sent = _event("first comment")
        event.message.photo = MessageMediaPhoto(photo=MagicMock())
        event.message.document = None
        assert mgr._message_has_image(event) is True

        await mgr._on_new_message(event)
        assert "I win" not in sent, "an image message must not trigger the race reply"
        await ai.close()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Persistence and dashboard
# ---------------------------------------------------------------------------


def test_field_defaults_off() -> None:
    from qna_bot.storage.database import GroupConfig

    assert GroupConfig(chat_id=-1, title="x").first_comment_enabled is False


@pytest.mark.asyncio
async def test_toggle_round_trips(tmp_path) -> None:
    db = Database(tmp_path / "b.db", SecretCipher.from_env_or_file(None, tmp_path / ".k"))
    await db.connect()
    try:
        group = await db.upsert_group_metadata(CHAT_ID, "GA")
        group.first_comment_enabled = True
        await db.save_group(group)
        assert (await db.get_group(CHAT_ID)).first_comment_enabled is True
    finally:
        await db.close()


def test_dashboard_button_exists() -> None:
    from qna_bot.dashboard.keyboards import group_config_kb
    from qna_bot.storage.database import GroupConfig

    group = GroupConfig(chat_id=-100, title="GA", enabled=True)
    buttons = [b for row in group_config_kb(group, 0, "d").inline_keyboard for b in row]
    button = next(b for b in buttons if "First Comment" in b.text)
    assert button.callback_data == "gcfg:-100:firstcmt:0:d"
    assert "OFF" in button.text

    group.first_comment_enabled = True
    buttons = [b for row in group_config_kb(group, 0, "d").inline_keyboard for b in row]
    assert "ON" in next(b for b in buttons if "First Comment" in b.text).text


def test_module_exposes_the_contract() -> None:
    assert first_comment.FIRST_COMMENT_ANSWER == "I win"
    assert callable(first_comment.is_first_comment)
