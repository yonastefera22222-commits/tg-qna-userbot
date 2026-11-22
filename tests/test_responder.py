"""Tests for the silent-drop bug: admin detection failing must not mute the userbot."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from qna_bot.ai.engine import AIEngine
from qna_bot.storage.crypto import SecretCipher
from qna_bot.storage.database import Database, GroupConfig
from qna_bot.userbot.manager import UserbotManager

CHAT_ID = -1001234567890
SENDER_ID = 4242


# ---------------------------------------------------------------------------
# The core regression: an unavailable admin list must fail OPEN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unverifiable_admin_list_fails_open(tmp_path) -> None:
    """Basic groups / restricted member lists raise. The old code returned an
    empty set and every message was then dropped as 'not an admin'."""
    db = Database(tmp_path / "b.db", SecretCipher.from_env_or_file(None, tmp_path / ".k"))
    await db.connect()
    try:
        mgr = UserbotManager(db, AIEngine())

        # Simulate a connected client whose admin listing always fails.
        client = MagicMock()
        client.is_connected = MagicMock(return_value=True)

        async def _boom(*a, **k):
            raise RuntimeError("Cannot get participants of a basic group")
            yield  # pragma: no cover

        client.iter_participants = MagicMock(side_effect=lambda *a, **k: _boom())
        client.get_participants = AsyncMock(side_effect=RuntimeError("restricted"))
        mgr._client = client

        event = MagicMock()
        event.sender_id = SENDER_ID
        event.message.from_id = None

        # An ordinary participant must still be allowed through.
        assert await mgr._is_admin(CHAT_ID, event) is True

        entry = mgr._admin_cache[CHAT_ID]
        assert entry[2] is False, "an unverified list must be recorded as such"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_verified_admin_list_still_filters(tmp_path) -> None:
    """When admins ARE known, non-admins must still be blocked."""
    db = Database(tmp_path / "b.db", SecretCipher.from_env_or_file(None, tmp_path / ".k"))
    await db.connect()
    try:
        mgr = UserbotManager(db, AIEngine())
        mgr._admin_cache[CHAT_ID] = (0.0, {999}, True)

        event = MagicMock()
        event.sender_id = SENDER_ID
        event.message.from_id = None
        assert await mgr._is_admin(CHAT_ID, event) is False

        admin_event = MagicMock()
        admin_event.sender_id = 999
        admin_event.message.from_id = None
        assert await mgr._is_admin(CHAT_ID, admin_event) is True
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# End-to-end: a plain question in an enabled group must produce a reply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plain_question_is_answered_end_to_end(tmp_path) -> None:
    db = Database(tmp_path / "b.db", SecretCipher.from_env_or_file(None, tmp_path / ".k"))
    await db.connect()
    try:
        ai = AIEngine()
        mgr = UserbotManager(db, ai)

        # Enabled group with the DEFAULT settings (admins-only, aggressive).
        group = await db.upsert_group_metadata(CHAT_ID, "Giveaway Arena")
        group.enabled = True
        await db.save_group(group)
        await mgr.reload_cache()
        mgr._global_enabled = True

        # Admin detection is unavailable -> must fail open.
        mgr._admin_cache[CHAT_ID] = (10e9, set(), False)

        sent: list[str] = []

        async def fake_answer(**kwargs):
            from qna_bot.ai.engine import AnswerResult

            return AnswerResult(
                answered=True, answer="Nairobi", provider="groq",
                model="openai/gpt-oss-120b", latency_ms=42,
            )

        ai.answer_question = fake_answer  # type: ignore[assignment]

        event = MagicMock()
        event.out = False
        event.is_private = False
        event.chat_id = CHAT_ID
        event.id = 77
        event.sender_id = SENDER_ID
        event.raw_text = "What is the capital of Kenya?"
        event.message.from_id = None
        event.get_sender = AsyncMock()
        event.reply = AsyncMock(side_effect=lambda t: sent.append(t))
        event.respond = AsyncMock(side_effect=lambda t: sent.append(t))

        await mgr._on_new_message(event)

        assert sent == ["Nairobi"], "a plain question must be answered"
        assert mgr.counters["answers_sent"] == 1
        assert any(d["outcome"] == "answered" for d in mgr.recent_decisions)
        await ai.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_skip_reason_is_recorded_for_diagnostics(tmp_path) -> None:
    """When the prefilter blocks a message, /diag must be able to say why."""
    db = Database(tmp_path / "b.db", SecretCipher.from_env_or_file(None, tmp_path / ".k"))
    await db.connect()
    try:
        mgr = UserbotManager(db, AIEngine())
        group = await db.upsert_group_metadata(CHAT_ID, "Chat")
        group.enabled = True
        group.trigger_keywords = "quiz"
        await db.save_group(group)
        await mgr.reload_cache()
        mgr._admin_cache[CHAT_ID] = (10e9, set(), False)

        event = MagicMock()
        event.out = False
        event.is_private = False
        event.chat_id = CHAT_ID
        event.id = 12
        event.sender_id = SENDER_ID
        event.raw_text = "What is the capital of Kenya?"
        event.message.from_id = None
        event.get_sender = AsyncMock()

        await mgr._on_new_message(event)

        decisions = mgr.recent_decisions
        assert decisions, "every message must leave a trace"
        last = decisions[-1]
        assert last["outcome"] == "skip:prefilter"
        assert "keyword" in str(last["detail"]).lower()
    finally:
        await db.close()


def test_disabled_group_is_reported_not_silent(tmp_path) -> None:
    """GroupConfig defaults must be understood: admins-only + disabled by default."""
    g = GroupConfig(chat_id=CHAT_ID, title="x")
    assert g.enabled is False
    assert g.sender_filter == "admins"
