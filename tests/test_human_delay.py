"""The human delay must mean one thing for every answer type.

The bug this pins: the delay used to be ``sleep(delay)`` applied *after* the
answer had been produced. Instant arithmetic therefore posted at ~1.5 s while an
image question (download + vision model + send) posted at ~5.5 s, and the first
comment ignored the setting entirely. To the owner that reads as "the delay only
works on some requests".

The rule now: a message that arrived at T is posted no earlier than ``T + delay``
and as soon as possible after that. Every path - local math, Forex formulas, OHLC
history, model answers, image questions and the first comment - is held to the
same clock.
"""

from __future__ import annotations

import time

import pytest

from qna_bot.userbot.human_delay import (
    FIRST_COMMENT_MAX_DELAY_MS,
    effective_delay_ms,
    wait_for_send,
)

# ---------------------------------------------------------------------------
# effective_delay_ms - the one place the per-path rule lives
# ---------------------------------------------------------------------------


def test_a_normal_answer_uses_the_setting_in_full() -> None:
    assert effective_delay_ms(0) == 0
    assert effective_delay_ms(300) == 300
    assert effective_delay_ms(1500) == 1500
    assert effective_delay_ms(5000) == 5000


def test_the_first_comment_delay_is_capped_not_ignored() -> None:
    """"First comment" is a race against every other bot in the group.

    Honouring a 5 s delay there means never winning it, but silently ignoring
    the setting is what the owner complained about. It is bounded instead, and
    the bound is small enough to still be first.
    """
    assert FIRST_COMMENT_MAX_DELAY_MS <= 250
    assert effective_delay_ms(5000, first_comment=True) == FIRST_COMMENT_MAX_DELAY_MS
    # Below the cap it is used as configured - the setting is not a no-op.
    assert effective_delay_ms(120, first_comment=True) == 120
    assert effective_delay_ms(0, first_comment=True) == 0


def test_a_negative_setting_is_treated_as_off() -> None:
    """The number is owner-editable; a bad one must not stop answers."""
    assert effective_delay_ms(-1) == 0
    assert effective_delay_ms(-5000, first_comment=True) == 0


# ---------------------------------------------------------------------------
# wait_for_send - the floor is measured from arrival, not added to the work
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fast_path_waits_out_the_whole_delay(monkeypatch) -> None:
    """Instant arithmetic must not skip the delay just because it was instant."""
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("qna_bot.userbot.human_delay.asyncio.sleep", _sleep)
    arrival = time.monotonic()
    waited = await wait_for_send(arrival, 1500)
    assert waited == pytest.approx(1.5, abs=0.05)
    assert slept and slept[0] == pytest.approx(1.5, abs=0.05)


@pytest.mark.asyncio
async def test_a_slow_path_does_not_sleep_again(monkeypatch) -> None:
    """An answer that already took longer than the delay posts immediately.

    This is the half of the bug that made image questions feel like 5 s: work
    finished late, and then the full delay was added on top.
    """
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:  # pragma: no cover - must not run
        slept.append(seconds)

    monkeypatch.setattr("qna_bot.userbot.human_delay.asyncio.sleep", _sleep)
    arrival = time.monotonic() - 4.0  # work already took four seconds
    waited = await wait_for_send(arrival, 1500)
    assert waited == 0.0
    assert slept == []


@pytest.mark.asyncio
async def test_the_total_time_to_post_is_the_delay_on_every_path(monkeypatch) -> None:
    """The property the owner actually cares about.

    Two very different amounts of work - 5 ms and 900 ms - must both post at the
    same wall-clock offset from arrival when the delay is 1500 ms.
    """
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("qna_bot.userbot.human_delay.asyncio.sleep", _sleep)

    arrival = time.monotonic()
    # Simulate the fast path: 5 ms of work.
    time.sleep(0.005)
    fast = await wait_for_send(arrival, 1500)
    assert fast == pytest.approx(1.495, abs=0.05)

    slept.clear()
    arrival = time.monotonic()
    # Simulate the slow path: 900 ms of work (an AI call).
    time.sleep(0.9)
    slow = await wait_for_send(arrival, 1500)
    assert slow == pytest.approx(0.6, abs=0.05)

    # Both land at ~1.5 s after arrival, which is the point.
    assert (fast + 0.005) == pytest.approx(slow + 0.9, abs=0.05)


@pytest.mark.asyncio
async def test_delay_off_never_sleeps(monkeypatch) -> None:
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:  # pragma: no cover - must not run
        slept.append(seconds)

    monkeypatch.setattr("qna_bot.userbot.human_delay.asyncio.sleep", _sleep)
    assert await wait_for_send(time.monotonic(), 0) == 0.0
    assert slept == []


@pytest.mark.asyncio
async def test_a_missing_arrival_time_does_not_block_the_answer() -> None:
    """Defensive: a caller that cannot supply arrival time still answers."""
    assert await wait_for_send(None, 1500) == 0.0


@pytest.mark.asyncio
async def test_first_comment_uses_the_cap(monkeypatch) -> None:
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("qna_bot.userbot.human_delay.asyncio.sleep", _sleep)
    waited = await wait_for_send(time.monotonic(), 5000, first_comment=True)
    assert waited == pytest.approx(FIRST_COMMENT_MAX_DELAY_MS / 1000.0, abs=0.05)


# ---------------------------------------------------------------------------
# The handler paths: the unit above is useless if a path never calls it
# ---------------------------------------------------------------------------


async def _manager(tmp_path, *, delay_ms: int, first_comment: bool = False):
    from qna_bot.ai.engine import AIEngine
    from qna_bot.storage.crypto import SecretCipher
    from qna_bot.storage.database import Database
    from qna_bot.userbot.manager import UserbotManager

    db = Database(tmp_path / "b.db", SecretCipher.from_env_or_file(None, tmp_path / ".k"))
    await db.connect()
    ai = AIEngine()
    mgr = UserbotManager(db, ai)
    group = await db.upsert_group_metadata(-1001234567890, "Giveaway Arena")
    group.enabled = True
    group.sender_filter = "all"
    group.human_delay_ms = delay_ms
    group.first_comment_enabled = first_comment
    group.local_math_enabled = True
    await db.save_group(group)
    await mgr.reload_cache()
    mgr._global_enabled = True
    return db, ai, mgr


def _event(text: str):
    from unittest.mock import AsyncMock, MagicMock

    event = MagicMock()
    event.out = False
    event.is_private = False
    event.chat_id = -1001234567890
    event.id = 101
    event.sender_id = 4242
    event.raw_text = text
    event.message.from_id = None
    event.get_sender = AsyncMock()
    sent: list[str] = []
    event.reply = AsyncMock(side_effect=lambda t: sent.append(t))
    event.respond = AsyncMock(side_effect=lambda t: sent.append(t))
    return event, sent


@pytest.fixture
def record_sleeps(monkeypatch):
    """Record the delay the handler asked for, without actually waiting."""
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("qna_bot.userbot.human_delay.asyncio.sleep", _sleep)
    # time.monotonic is left alone, so "work already took X" is real.
    return slept


@pytest.mark.asyncio
async def test_instant_local_math_waits_the_full_delay(tmp_path, record_sleeps) -> None:
    """The reported case: the instant math answer must not skip the delay.

    The real engine is used, not a stub - local arithmetic is solved inside
    ``answer_question``, so replacing that method would replace the very path
    under test. No API key is configured, so a model call would fail loudly.
    """
    db, ai, mgr = await _manager(tmp_path, delay_ms=700)
    try:
        event, sent = _event("what is 34% of 250")
        await mgr._on_new_message(event)

        assert sent == ["85"], f"expected the local arithmetic answer, got {sent}"
        assert record_sleeps, "the delay must apply to the instant path"
        assert record_sleeps[0] == pytest.approx(0.7, abs=0.1)
        await ai.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_forex_formula_waits_the_full_delay(tmp_path, record_sleeps) -> None:
    """Forex pip/position math takes the same local path and the same delay."""
    db, ai, mgr = await _manager(tmp_path, delay_ms=700)
    try:
        group = await db.get_group(-1001234567890)
        group.forex_enabled = True
        group.forex_live_data = False
        await db.save_group(group)
        await mgr.reload_cache()

        event, sent = _event("what is the pip value of 1 lot EURUSD")
        await mgr._on_new_message(event)

        assert sent, "the Forex formula answer must still be posted"
        assert record_sleeps, "the delay must apply to the Forex path"
        assert record_sleeps[0] == pytest.approx(0.7, abs=0.1)
        await ai.close()
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,model",
    [
        ("local_math", "ast-instant"),
        ("local_math", "forex-formula"),
        ("local_market", "ohlc-history"),
        ("groq", "llama-3.3-70b"),
    ],
)
async def test_every_answer_provider_meets_the_same_clock(
    tmp_path, record_sleeps, provider, model
) -> None:
    """One send site, so the answer's origin cannot change the delay.

    This is the structural guarantee: ast-instant, forex-formula, ohlc-history
    and a plain model answer all come back as the same ``AnswerResult`` through
    the same return point, so there is no per-provider branch that could skip
    the wait.
    """
    from qna_bot.ai.engine import AnswerResult

    db, ai, mgr = await _manager(tmp_path, delay_ms=700)
    try:
        async def _answer(*args, **kwargs):
            return AnswerResult(
                answered=True,
                answer="42",
                provider=provider,
                model=model,
                latency_ms=1,
            )

        ai.answer_question = _answer  # type: ignore[assignment]
        event, sent = _event("what is the answer")
        await mgr._on_new_message(event)

        assert sent == ["42"]
        assert record_sleeps, f"{provider}/{model} skipped the delay"
        assert record_sleeps[0] == pytest.approx(0.7, abs=0.1)
        await ai.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_there_is_exactly_one_delay_site(tmp_path) -> None:
    """A second, unconditional sleep somewhere would double the delay.

    Guards against the old shape coming back: a per-path ``asyncio.sleep``.
    """
    import inspect

    from qna_bot.userbot.manager import UserbotManager

    source = inspect.getsource(UserbotManager._on_new_message)
    assert source.count("wait_for_send(") == 1
    assert "asyncio.sleep(group.human_delay_ms" not in source

    first_comment = inspect.getsource(UserbotManager._answer_first_comment)
    assert first_comment.count("wait_for_send(") == 1
    assert "asyncio.sleep(group.human_delay_ms" not in first_comment


@pytest.mark.asyncio
async def test_first_comment_waits_but_only_briefly(tmp_path, record_sleeps) -> None:
    """A first-comment race honours the delay, capped so it can still win."""
    db, ai, mgr = await _manager(tmp_path, delay_ms=5000, first_comment=True)
    try:
        event, sent = _event("First Comment 🔥")
        await mgr._on_new_message(event)

        # The reply rotates through a pool, so any of the pool is correct here;
        # what this test is about is the delay.
        assert len(sent) == 1
        assert record_sleeps, "the first comment must not ignore the delay"
        assert record_sleeps[0] == pytest.approx(
            FIRST_COMMENT_MAX_DELAY_MS / 1000.0, abs=0.1
        )
        await ai.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delay_off_posts_immediately_on_both_paths(tmp_path, record_sleeps) -> None:
    db, ai, mgr = await _manager(tmp_path, delay_ms=0, first_comment=True)
    try:
        event, sent = _event("First Comment")
        await mgr._on_new_message(event)
        assert len(sent) == 1
        assert record_sleeps == []
        await ai.close()
    finally:
        await db.close()
