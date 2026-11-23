"""Questions posted on behalf of a channel, and numbered question series.

From the screenshot: group "Cc", a card asking "Who is this" with no answer, and
four Amharic questions from "TM Tips" posted 9:00–9:17 pm. The bot answered none
of them, and there was nothing in the log to explain it.

The filter is not the problem - all five pass in every detection mode. The
blocker was the **sender filter**: those messages are channel posts.

Telethon resolves a message sent on behalf of a channel (a forward, or a channel
admin posting anonymously) to the *channel's* id, which is negative and can
therefore never appear in a group's admin list. With the default
``sender_filter="admins"``, every one of them was dropped as "not an admin" -
a statement that is technically true and completely useless, because the owner
never made that channel an admin and cannot.

Two smaller faults made it worse: the log said nothing about *why*, and context
was one message stale so a numbered series could not be recognised as a series.
"""

from __future__ import annotations

import pathlib
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.tl.types import PeerChannel

from qna_bot.ai.engine import AIEngine
from qna_bot.storage.crypto import SecretCipher
from qna_bot.storage.database import Database, GroupConfig
from qna_bot.userbot.manager import UserbotManager

CHAT_ID = -1001234567890
CHANNEL_ID = -1009999999999
MEMBER_ID = 4242
ADMIN_ID = 999

#: Verbatim from the screenshot.
AMHARIC_QUESTIONS = [
    "1 በChampions League ታሪክ ብዙ ጊዜ ሻምፒዮን የሆነው ክለብ ከReal Madrid በኋላ የትኛው ነው?",
    "2 በ1977 የመጀመሪያውን FIFA U-20 World Cup ያሸነፈው ሀገር የትኛው ናት?",
    "3 በፕሪሚየር ሊግ ታሪክ ጎል ያስቆጠረ የመጀመሪያው ግብ ጠባቂ ማን ነው?",
    (
        "4 በአገር ነፃ ታሪክ በዓለም ዋንጫ (World Cup) ላይ በነጭ ካርድ ሳይቀበል "
        "በጣም ብዙ ጨዋታዎችን ያተመነው ተጫዋች ማን ነው?"
    ),
]


def _manager(admins: set[int], verified: bool = True) -> UserbotManager:
    mgr = UserbotManager.__new__(UserbotManager)
    mgr._admin_cache = {CHAT_ID: (10e9, admins, verified)}
    mgr.ADMIN_CACHE_TTL = 600
    return mgr


def _event(*, sender_id: int, from_id=None, fwd_user_id: int | None = None):
    event = MagicMock()
    event.sender_id = sender_id
    event.message.from_id = from_id
    event.message.fwd_from = None
    if fwd_user_id is not None:
        fwd = MagicMock()
        fwd.from_id = MagicMock()
        fwd.from_id.user_id = fwd_user_id
        event.message.fwd_from = fwd
    return event


# ---------------------------------------------------------------------------
# Channel senders are not admins, and never could be
# ---------------------------------------------------------------------------


def test_channel_senders_are_recognised() -> None:
    assert UserbotManager._is_channel_sender(CHANNEL_ID) is True
    assert UserbotManager._is_channel_sender(CHAT_ID) is True
    # A person's id is positive; that is the whole distinction.
    assert UserbotManager._is_channel_sender(MEMBER_ID) is False
    assert UserbotManager._is_channel_sender(ADMIN_ID) is False
    assert UserbotManager._is_channel_sender(None) is False


@pytest.mark.asyncio
async def test_a_forwarded_post_is_judged_by_who_forwarded_it() -> None:
    """The reported case: a channel post forwarded by an admin.

    Judging the channel produces a permanent "not an admin"; judging the
    forwarder produces the right answer, because the forwarder is a person who
    can be - and here is - an admin.
    """
    mgr = _manager({ADMIN_ID})
    event = _event(
        sender_id=CHANNEL_ID,
        from_id=PeerChannel(channel_id=9999999999),
        fwd_user_id=ADMIN_ID,
    )
    assert await mgr._is_admin(CHAT_ID, event) is True


@pytest.mark.asyncio
async def test_a_forward_by_a_non_admin_is_still_rejected() -> None:
    """Fixing the lookup must not make the admin filter meaningless."""
    mgr = _manager({ADMIN_ID})
    event = _event(sender_id=CHANNEL_ID, from_id=PeerChannel(channel_id=1), fwd_user_id=MEMBER_ID)
    assert await mgr._is_admin(CHAT_ID, event) is False


@pytest.mark.asyncio
async def test_a_channel_admin_with_no_forwarder_is_rejected() -> None:
    """A channel posting anonymously has no person to judge, so it is not an admin."""
    mgr = _manager({ADMIN_ID})
    event = _event(sender_id=CHANNEL_ID, from_id=PeerChannel(channel_id=9999999999))
    assert await mgr._is_admin(CHAT_ID, event) is False


@pytest.mark.asyncio
async def test_an_unverifiable_admin_list_still_fails_open_for_channels() -> None:
    """The existing fail-open behaviour must survive the channel handling."""
    mgr = _manager(set(), verified=False)
    event = _event(sender_id=CHANNEL_ID, from_id=PeerChannel(channel_id=9999999999))
    assert await mgr._is_admin(CHAT_ID, event) is True


# ---------------------------------------------------------------------------
# End to end: the four Amharic questions must be answered
# ---------------------------------------------------------------------------


async def _run_message(
    *,
    text: str,
    sender_id: int,
    fwd_user_id: int | None,
    sender_filter: str,
    context_messages: int = 0,
    message_id: int = 1,
):
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = Database(tmp / "b.db", SecretCipher.from_env_or_file(None, tmp / ".k"))
    await db.connect()
    try:
        engine = AIEngine()
        mgr = UserbotManager(db, engine)
        group = await db.upsert_group_metadata(CHAT_ID, "Cc")
        group.enabled = True
        group.sender_filter = sender_filter
        group.context_messages = context_messages
        await db.save_group(group)
        await db.set_provider_key("groq", "k")
        await mgr.reload_cache()
        mgr._global_enabled = True
        mgr._admin_cache[CHAT_ID] = (10e9, {ADMIN_ID}, True)

        seen: dict[str, object] = {}

        async def fake_answer(**kwargs):
            from qna_bot.ai.engine import AnswerResult

            seen.update(kwargs)
            return AnswerResult(
                answered=True, answer="Real Madrid", provider="groq", model="m", latency_ms=9
            )

        engine.answer_question = fake_answer  # type: ignore[assignment]

        event = _event(
            sender_id=sender_id,
            from_id=PeerChannel(channel_id=9999999999),
            fwd_user_id=fwd_user_id,
        )
        event.out = False
        event.is_private = False
        event.chat_id = CHAT_ID
        event.id = message_id
        event.raw_text = text
        event.download_media = AsyncMock(return_value=None)
        event.get_reply_message = AsyncMock(return_value=None)
        sent: list[str] = []
        event.reply = AsyncMock(side_effect=lambda t: sent.append(t))
        event.respond = AsyncMock(side_effect=lambda t: sent.append(t))

        await mgr._on_new_message(event)
        decisions = [(d["outcome"], str(d["detail"])) for d in mgr.recent_decisions]
        await engine.close()
        return sent, decisions, seen
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("question", AMHARIC_QUESTIONS)
async def test_channel_forwarded_question_is_answered(question: str) -> None:
    """Admins-only must still answer a channel post forwarded by an admin."""
    sent, decisions, _ = await _run_message(
        text=question, sender_id=CHANNEL_ID, fwd_user_id=ADMIN_ID, sender_filter="admins"
    )
    assert sent == ["Real Madrid"], decisions
    assert decisions[0][0] == "answered", decisions


@pytest.mark.asyncio
async def test_everyone_mode_answers_an_anonymous_channel_post() -> None:
    """"TM Tips" posting directly is a channel, not a member, so admins-only
    cannot ever match it. Everyone mode is the setting that answers it."""
    sent, decisions, _ = await _run_message(
        text=AMHARIC_QUESTIONS[0],
        sender_id=CHANNEL_ID,
        fwd_user_id=None,
        sender_filter="all",
    )
    assert sent == ["Real Madrid"], decisions


@pytest.mark.asyncio
async def test_a_rejected_channel_post_explains_why() -> None:
    """The silent drop is why this took a screenshot to diagnose."""
    sent, decisions, _ = await _run_message(
        text=AMHARIC_QUESTIONS[0],
        sender_id=CHANNEL_ID,
        fwd_user_id=None,
        sender_filter="admins",
    )
    assert sent == []
    outcome, detail = decisions[0]
    assert outcome == "skip:not_admin"
    assert "channel" in detail
    assert "Everyone" in detail


@pytest.mark.asyncio
async def test_a_plain_member_is_still_rejected_in_admins_mode() -> None:
    """The narrow fix must not turn admins-only into everyone."""
    sent, decisions, _ = await _run_message(
        text=AMHARIC_QUESTIONS[0], sender_id=MEMBER_ID, fwd_user_id=None, sender_filter="admins"
    )
    assert sent == []
    assert decisions[0][0] == "skip:not_admin"
    assert "channel" not in decisions[0][1]


# ---------------------------------------------------------------------------
# A numbered series must reach the model as a series
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_includes_the_current_message_not_the_previous_one() -> None:
    """The buffer was read before the append, so context was always one behind.

    With context_messages=1 the "latest message to evaluate" was the *previous*
    question, and the one being asked was missing entirely.
    """
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = Database(tmp / "b.db", SecretCipher.from_env_or_file(None, tmp / ".k"))
    await db.connect()
    try:
        engine = AIEngine()
        mgr = UserbotManager(db, engine)
        group = await db.upsert_group_metadata(CHAT_ID, "Cc")
        group.enabled = True
        group.sender_filter = "all"
        group.context_messages = 2
        group.cooldown_seconds = 0
        await db.save_group(group)
        await db.set_provider_key("groq", "k")
        await mgr.reload_cache()
        mgr._global_enabled = True
        mgr._admin_cache[CHAT_ID] = (10e9, {ADMIN_ID}, True)

        calls: list[list[str] | None] = []

        async def fake_answer(**kwargs):
            from qna_bot.ai.engine import AnswerResult

            calls.append(kwargs.get("context_lines"))
            return AnswerResult(answered=True, answer="x", provider="g", model="m", latency_ms=1)

        engine.answer_question = fake_answer  # type: ignore[assignment]

        for i, question in enumerate(AMHARIC_QUESTIONS[:3], start=1):
            event = _event(sender_id=MEMBER_ID)
            event.out = False
            event.is_private = False
            event.chat_id = CHAT_ID
            event.id = 100 + i
            event.raw_text = question
            event.download_media = AsyncMock(return_value=None)
            event.get_reply_message = AsyncMock(return_value=None)
            event.reply = AsyncMock()
            event.respond = AsyncMock()
            await mgr._on_new_message(event)

        assert len(calls) == 3
        # First question: nothing before it, so no context at all.
        assert not calls[0]
        # Second: one line of context, and it is question 1.
        assert calls[1] and AMHARIC_QUESTIONS[0] in calls[1][-1]
        # Third: two lines, questions 1 and 2.
        assert calls[2] and AMHARIC_QUESTIONS[1] in calls[2][-1]
        await engine.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_context_never_contains_a_duplicate_of_the_current_message() -> None:
    """Appending first and then slicing must not send the question twice."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = Database(tmp / "b.db", SecretCipher.from_env_or_file(None, tmp / ".k"))
    await db.connect()
    try:
        engine = AIEngine()
        mgr = UserbotManager(db, engine)
        group = await db.upsert_group_metadata(CHAT_ID, "Cc")
        group.enabled = True
        group.sender_filter = "all"
        group.context_messages = 3
        group.cooldown_seconds = 0
        await db.save_group(group)
        await db.set_provider_key("groq", "k")
        await mgr.reload_cache()
        mgr._global_enabled = True
        mgr._admin_cache[CHAT_ID] = (10e9, {ADMIN_ID}, True)

        captured: list[list[str] | None] = []

        async def fake_answer(**kwargs):
            from qna_bot.ai.engine import AnswerResult

            captured.append(kwargs.get("context_lines"))
            return AnswerResult(answered=True, answer="x", provider="g", model="m", latency_ms=1)

        engine.answer_question = fake_answer  # type: ignore[assignment]

        question = AMHARIC_QUESTIONS[0]
        event = _event(sender_id=MEMBER_ID)
        event.out = False
        event.is_private = False
        event.chat_id = CHAT_ID
        event.id = 1
        event.raw_text = question
        event.download_media = AsyncMock(return_value=None)
        event.get_reply_message = AsyncMock(return_value=None)
        event.reply = AsyncMock()
        event.respond = AsyncMock()
        await mgr._on_new_message(event)

        context = captured[0] or []
        assert question[:200] not in context
        await engine.close()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# The filter was never the problem - pinned so it stays that way
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["giveaway_aggressive", "balanced", "strict"])
@pytest.mark.parametrize("question", AMHARIC_QUESTIONS)
def test_amharic_qa_questions_pass_the_filter(question: str, mode: str) -> None:
    from qna_bot.userbot.filters import should_evaluate_message

    group = GroupConfig(chat_id=CHAT_ID, title="Cc", enabled=True, detection_mode=mode)
    decision = should_evaluate_message(question, group)
    assert decision.should_process, (mode, decision.reason)


# ---------------------------------------------------------------------------
# Testing from a second account (the reporter's actual setup)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_account_that_is_a_group_admin_gets_answered() -> None:
    """The realistic setup: the userbot runs as account A, the owner tests from
    account B, and B is an admin of the group. That path works."""
    sent, decisions, _ = await _run_message(
        text=AMHARIC_QUESTIONS[0], sender_id=ADMIN_ID, fwd_user_id=None, sender_filter="admins"
    )
    assert sent == ["Real Madrid"], decisions


@pytest.mark.asyncio
async def test_second_account_that_is_only_a_member_is_silently_rejected() -> None:
    """The likely cause of the screenshot: the tester is not in the admin list.

    Nothing is wrong with the bot - "Admins Only" means what it says. But the
    log line must name the sender so this is diagnosable instead of looking like
    a dead bot.
    """
    sent, decisions, _ = await _run_message(
        text=AMHARIC_QUESTIONS[0], sender_id=MEMBER_ID, fwd_user_id=None, sender_filter="admins"
    )
    assert sent == []
    outcome, detail = decisions[0]
    assert outcome == "skip:not_admin"
    assert "is not an admin" in detail


@pytest.mark.asyncio
async def test_everyone_mode_answers_a_non_admin_tester() -> None:
    sent, decisions, _ = await _run_message(
        text=AMHARIC_QUESTIONS[0], sender_id=MEMBER_ID, fwd_user_id=None, sender_filter="all"
    )
    assert sent == ["Real Madrid"], decisions


@pytest.mark.asyncio
async def test_own_messages_are_counted_not_answered() -> None:
    """A userbot answering its own posts would loop forever."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = Database(tmp / "b.db", SecretCipher.from_env_or_file(None, tmp / ".k"))
    await db.connect()
    try:
        engine = AIEngine()
        mgr = UserbotManager(db, engine)
        group = await db.upsert_group_metadata(CHAT_ID, "Cc")
        group.enabled = True
        group.sender_filter = "all"
        await db.save_group(group)
        await db.set_provider_key("groq", "k")
        await mgr.reload_cache()
        mgr._global_enabled = True

        called = False

        async def fake_answer(**kwargs):
            nonlocal called
            called = True
            from qna_bot.ai.engine import AnswerResult

            return AnswerResult(answered=True, answer="x", provider="g", model="m", latency_ms=1)

        engine.answer_question = fake_answer  # type: ignore[assignment]

        event = _event(sender_id=ADMIN_ID)
        event.out = True
        event.is_private = False
        event.chat_id = CHAT_ID
        event.id = 1
        event.raw_text = AMHARIC_QUESTIONS[0]
        event.reply = AsyncMock()
        event.respond = AsyncMock()

        await mgr._on_new_message(event)
        assert called is False
        assert mgr.counters["answers_sent"] == 0
        assert mgr.counters["messages_seen"] == 0
        assert mgr.counters["own_messages"] == 1
        await engine.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_private_chat_messages_are_ignored_and_not_counted_as_own() -> None:
    """A DM to the userbot is neither a group question nor an own message."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = Database(tmp / "b.db", SecretCipher.from_env_or_file(None, tmp / ".k"))
    await db.connect()
    try:
        engine = AIEngine()
        mgr = UserbotManager(db, engine)
        await mgr.reload_cache()
        mgr._global_enabled = True

        event = _event(sender_id=MEMBER_ID)
        event.out = False
        event.is_private = True
        event.chat_id = MEMBER_ID
        event.raw_text = "hello"
        event.reply = AsyncMock()
        event.respond = AsyncMock()

        await mgr._on_new_message(event)
        assert mgr.counters["own_messages"] == 0
        assert mgr.counters["messages_seen"] == 0
        await engine.close()
    finally:
        await db.close()
