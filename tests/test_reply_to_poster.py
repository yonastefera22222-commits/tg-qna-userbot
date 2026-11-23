"""The giveaway shape from the screenshot: members answer *under* the card.

A host posts a card asking the question. Members then reply to it, or post
under it, writing only "What is the answer". Nothing is wrong with that message
- it is exactly how a giveaway works - but read on its own it is a pointer to a
picture the bot never sees, so it got answered literally:

    Mom: What is the answer   (as a reply to the card)
    bot: No live data

Three separate faults produced that, and each one alone is enough to break it:

1. "What is the answer" reads as a question, so the caption was treated as the
   question and the card was never transcribed.
2. With a picture attached, a refusal was accepted as an answer - and a refusal
   cannot be right when the figure is legible in the attachment.
3. A reply carries no photo of its own, so unless the bot looks at the message
   being replied to it has nothing to read at all.

These tests pin all three, plus the boundaries that keep the fixes from
attaching an unrelated picture to an unrelated question.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from qna_bot.ai.engine import AIEngine
from qna_bot.ai.forex import is_refusal
from qna_bot.ai.market import MarketFeed
from qna_bot.ai.text_cleanup import (
    is_answer_pointer,
    is_photo_caption_stub,
    refers_to_an_image,
    wants_inherited_image,
)
from qna_bot.storage.crypto import SecretCipher
from qna_bot.storage.database import Database
from qna_bot.userbot.manager import UserbotManager

CHAT_ID = -1001234567890
JPEG = b"\xff\xd8\xff\xe0 poster"

#: What the card in the screenshot says, as OCR reports it.
CARD_TEXT = (
    "EYUEL FX\n"
    "The 20$ Question\n"
    "On the Exness BTC chart, what was the highest price recorded this week?\n"
    "LET'S GO!"
)

PAYLOAD = {
    "chart": {
        "result": [
            {
                "timestamp": [1764201600],
                "indicators": {
                    "quote": [
                        {
                            "high": [93125.75],
                            "low": [91000.0],
                            "open": [91800.0],
                            "close": [93000.0],
                        }
                    ]
                },
            }
        ]
    }
}


# ---------------------------------------------------------------------------
# 1. "What is the answer" is a pointer, not a question
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "caption",
    [
        "What is the answer",
        "what is the answer?",
        "What's the answer",
        "Whats the answer",
        "Answer below",
        "The answer is in the image",
        "solve this",
        "Read the card",
        "First to answer",
    ],
)
def test_comment_prompts_are_pointers_not_questions(caption: str) -> None:
    """These are the caption on every giveaway card. They ask nothing.

    Treated as questions, the bot answers the nudge itself ("No live data") and
    never reads the card it is pointing at.
    """
    assert is_answer_pointer(caption), caption
    assert is_photo_caption_stub(caption), caption


@pytest.mark.parametrize(
    "caption",
    [
        "what was the high this week?",
        "what is a pip",
        "what is the capital of Ethiopia",
        "Answer this one for the giveaway: what was the highest price of BTC this week?",
        "How many members does NATO have?",
    ],
)
def test_real_questions_are_not_pointers(caption: str) -> None:
    """A caption that actually asks something must still be answered as written.

    The last one is the boundary: it *contains* a nudge, but it goes on to ask
    the question, so re-reading the picture would be answering the wrong thing.
    """
    assert not is_answer_pointer(caption), caption


@pytest.mark.asyncio
async def test_pointer_caption_triggers_a_read_of_the_card() -> None:
    """The reported message, end to end through the engine."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "yahoo" in str(request.url):
            calls.append("yahoo")
            return httpx.Response(200, json=PAYLOAD)
        body = request.read().decode("utf-8", "replace")
        if AIEngine.TRANSCRIBE_PROMPT in body:
            calls.append("transcribe")
            return httpx.Response(
                200, json={"choices": [{"message": {"content": CARD_TEXT}, "finish_reason": "stop"}]}
            )
        calls.append("answer")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "No live data"}, "finish_reason": "stop"}]}
        )

    transport = httpx.MockTransport(handler)
    engine = AIEngine(market_feed=MarketFeed(httpx.AsyncClient(transport=transport)))
    engine._client = httpx.AsyncClient(transport=transport)

    result = await engine.answer_question(
        text="What is the answer",
        forex_group=True,
        live_data=True,
        answer_style="giveaway",
        primary_provider="groq",
        primary_model="openai/gpt-oss-120b",
        provider_keys={"groq": "k"},
        image=JPEG,
    )

    assert "transcribe" in calls
    assert result.answer == "93125.75"
    assert result.provider == "local_market"


# ---------------------------------------------------------------------------
# 2. A refusal is not an answer when a picture is attached
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    ["No live data", "no live data.", "No live market data", "I don't have live data"],
)
def test_refusals_are_recognised(answer: str) -> None:
    assert is_refusal(answer), answer


@pytest.mark.parametrize(
    "answer",
    ["66120.50", "No live data for DXY, but BTC is 66120.50", "", "The high was 93125.75"],
)
def test_real_answers_are_not_refusals(answer: str) -> None:
    """A partial refusal that goes on to answer must not be retried away."""
    assert not is_refusal(answer), answer


@pytest.mark.asyncio
async def test_refusal_with_an_image_is_retried_once() -> None:
    """The model declined to read a legible card; one retry asks it to look."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8", "replace")
        if AIEngine.TRANSCRIBE_PROMPT in body:
            calls.append("transcribe")
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "NONE"}, "finish_reason": "stop"}]}
            )
        if AIEngine.REFUSAL_RETRY_GUIDANCE in body:
            calls.append("retry")
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "93300.00"}, "finish_reason": "stop"}]},
            )
        calls.append("answer")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "No live data"}, "finish_reason": "stop"}]}
        )

    transport = httpx.MockTransport(handler)
    engine = AIEngine(market_feed=MarketFeed(httpx.AsyncClient(transport=transport)))
    engine._client = httpx.AsyncClient(transport=transport)

    result = await engine.answer_question(
        text="what does this chart show about gold?",
        forex_group=False,
        live_data=True,
        answer_style="giveaway",
        primary_provider="groq",
        primary_model="openai/gpt-oss-120b",
        provider_keys={"groq": "k"},
        image=JPEG,
    )
    assert result.answer == "93300.00"
    assert calls.count("retry") == 1


@pytest.mark.asyncio
async def test_refusal_without_an_image_is_never_retried() -> None:
    """With nothing to look at, a refusal is the honest answer."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append("answer")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "No live data"}, "finish_reason": "stop"}]}
        )

    transport = httpx.MockTransport(handler)
    engine = AIEngine(market_feed=MarketFeed(httpx.AsyncClient(transport=transport)))
    engine._client = httpx.AsyncClient(transport=transport)

    result = await engine.answer_question(
        text="what is gold doing right now?",
        forex_group=False,
        live_data=True,
        answer_style="giveaway",
        primary_provider="groq",
        primary_model="openai/gpt-oss-120b",
        provider_keys={"groq": "k"},
    )
    assert result.answer == "No live data"
    assert calls == ["answer"]


@pytest.mark.asyncio
async def test_a_repeated_refusal_is_kept_not_error_padded() -> None:
    """If the second look also finds nothing, the refusal stands."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "No live data"}, "finish_reason": "stop"}]}
        )

    transport = httpx.MockTransport(handler)
    engine = AIEngine(market_feed=MarketFeed(httpx.AsyncClient(transport=transport)))
    engine._client = httpx.AsyncClient(transport=transport)

    result = await engine.answer_question(
        text="",
        forex_group=False,
        live_data=True,
        answer_style="giveaway",
        primary_provider="groq",
        primary_model="openai/gpt-oss-120b",
        provider_keys={"groq": "k"},
        image=JPEG,
    )
    assert result.answer == "No live data"
    assert result.error is None


# ---------------------------------------------------------------------------
# 3. A reply inherits the picture it is replying to
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "wants"),
    [
        ("What is the answer", True),
        ("answer below", True),
        ("🔥🔥", True),
        ("", True),
        ("on this chart what was the high", True),
        ("what does the picture show", True),
        # A self-contained question must not inherit an unrelated picture.
        ("what is the capital of Ethiopia", False),
        ("who wrote Things Fall Apart", False),
    ],
)
def test_inheritance_only_for_messages_that_need_the_picture(
    text: str, wants: bool
) -> None:
    assert wants_inherited_image(text) is wants, text


def test_image_references_are_detected() -> None:
    for text in (
        "on this chart what was the high",
        "in the picture above",
        "what does the card say",
        "shown here",
    ):
        assert refers_to_an_image(text), text
    assert not refers_to_an_image("what is a pip")


def _reply_event(
    *,
    text: str,
    reply_to_msg_id: int | None,
    target_image: bool,
    image_bytes: bytes | None = JPEG,
):
    """An event whose reply-to message may or may not carry a photo."""
    from telethon.tl.types import MessageMediaPhoto

    target = MagicMock()
    # A real media type, not a bare MagicMock: the check is an isinstance and a
    # mock would silently pass or fail for the wrong reason.
    target.photo = MessageMediaPhoto(photo=MagicMock()) if target_image else None
    target.document = None
    # The size guard compares against an int, so it must be an int.
    target.file.size = len(image_bytes or b"")

    event = MagicMock()
    event.out = False
    event.is_private = False
    event.chat_id = CHAT_ID
    event.id = 501
    event.sender_id = 4242
    event.raw_text = text
    event.message.from_id = None
    event.message.photo = None
    event.message.document = None
    event.message.reply_to_msg_id = reply_to_msg_id

    async def downloadable(*args, **kwargs):
        return image_bytes

    event.download_media = AsyncMock(side_effect=downloadable)
    event.get_reply_message = AsyncMock(return_value=target)
    return event


@pytest.mark.asyncio
async def test_reply_to_a_poster_is_answered_from_the_poster(tmp_path) -> None:
    """The screenshot: "What is the answer" as a reply, answered from the card."""
    db = Database(tmp_path / "b.db", SecretCipher.from_env_or_file(None, tmp_path / ".k"))
    await db.connect()
    try:
        engine = AIEngine(market_feed=MarketFeed())
        mgr = UserbotManager(db, engine)
        group = await db.upsert_group_metadata(CHAT_ID, "Cc")
        group.enabled = True
        group.forex_enabled = True
        await db.save_group(group)
        await mgr.reload_cache()
        mgr._global_enabled = True
        mgr._admin_cache[CHAT_ID] = (10e9, set(), False)

        seen: dict[str, object] = {}

        async def fake_answer(**kwargs):
            from qna_bot.ai.engine import AnswerResult

            seen.update(kwargs)
            return AnswerResult(
                answered=True, answer="93125.75", provider="local_market",
                model="ohlc-history", latency_ms=5,
            )

        engine.answer_question = fake_answer  # type: ignore[assignment]

        event = _reply_event(text="What is the answer", reply_to_msg_id=499, target_image=True)
        sent: list[str] = []
        event.reply = AsyncMock(side_effect=lambda t: sent.append(t))
        event.respond = AsyncMock(side_effect=lambda t: sent.append(t))

        await mgr._on_new_message(event)

        assert seen["image"] == JPEG, "the replied-to poster must be used as the image"
        assert sent == ["93125.75"]
        await engine.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reply_to_plain_text_does_not_invent_a_picture(tmp_path) -> None:
    """Replying to a text message must not attach anything."""
    db = Database(tmp_path / "b.db", SecretCipher.from_env_or_file(None, tmp_path / ".k"))
    await db.connect()
    try:
        engine = AIEngine(market_feed=MarketFeed())
        mgr = UserbotManager(db, engine)
        group = await db.upsert_group_metadata(CHAT_ID, "Cc")
        group.enabled = True
        await db.save_group(group)
        await mgr.reload_cache()
        mgr._global_enabled = True
        mgr._admin_cache[CHAT_ID] = (10e9, set(), False)

        seen: dict[str, object] = {}

        async def fake_answer(**kwargs):
            from qna_bot.ai.engine import AnswerResult

            seen.update(kwargs)
            return AnswerResult(
                answered=True, answer="ok", provider="groq", model="m", latency_ms=5
            )

        engine.answer_question = fake_answer  # type: ignore[assignment]

        event = _reply_event(
            text="on this chart what was the high", reply_to_msg_id=499, target_image=False
        )
        event.reply = AsyncMock()
        event.respond = AsyncMock()
        await mgr._on_new_message(event)

        assert seen.get("image") is None
        await engine.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reply_inheritance_respects_the_photo_toggle(tmp_path) -> None:
    """A group that does not want photo answers must not fetch the poster."""
    db = Database(tmp_path / "b.db", SecretCipher.from_env_or_file(None, tmp_path / ".k"))
    await db.connect()
    try:
        engine = AIEngine(market_feed=MarketFeed())
        mgr = UserbotManager(db, engine)
        group = await db.upsert_group_metadata(CHAT_ID, "Cc")
        group.enabled = True
        group.vision_enabled = False
        await db.save_group(group)
        await mgr.reload_cache()
        mgr._global_enabled = True
        mgr._admin_cache[CHAT_ID] = (10e9, set(), False)

        event = _reply_event(text="What is the answer", reply_to_msg_id=499, target_image=True)
        event.reply = AsyncMock()
        event.respond = AsyncMock()
        await mgr._on_new_message(event)

        event.download_media.assert_not_called()
        await engine.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_no_reply_target_means_no_lookup(tmp_path) -> None:
    """A plain message must not pay for a reply lookup it cannot use."""
    db = Database(tmp_path / "b.db", SecretCipher.from_env_or_file(None, tmp_path / ".k"))
    await db.connect()
    try:
        engine = AIEngine(market_feed=MarketFeed())
        mgr = UserbotManager(db, engine)
        group = await db.upsert_group_metadata(CHAT_ID, "Cc")
        group.enabled = True
        await db.save_group(group)
        await mgr.reload_cache()
        mgr._global_enabled = True
        mgr._admin_cache[CHAT_ID] = (10e9, set(), False)

        async def fake_answer(**kwargs):
            from qna_bot.ai.engine import AnswerResult

            return AnswerResult(answered=True, answer="x", provider="g", model="m", latency_ms=1)

        engine.answer_question = fake_answer  # type: ignore[assignment]

        event = _reply_event(text="What is the answer", reply_to_msg_id=None, target_image=True)
        event.reply = AsyncMock()
        event.respond = AsyncMock()
        await mgr._on_new_message(event)

        event.get_reply_message.assert_not_called()
        await engine.close()
    finally:
        await db.close()


def test_payload_image_check_handles_a_message_without_media() -> None:
    assert not UserbotManager._payload_has_image(None)
    plain = MagicMock()
    plain.photo = None
    plain.document = None
    assert not UserbotManager._payload_has_image(plain)
