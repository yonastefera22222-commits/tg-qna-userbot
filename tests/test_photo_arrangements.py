"""Every arrangement a photo question can arrive in.

A giveaway post is not one format. The question can be in the caption, printed
across five lines of a poster, numbered among three others, written in Amharic,
or absent entirely with the answer sitting in a chart screenshot. Each of those
was a separate way to lose the answer, so each gets its own case here rather
than a single "photo questions work" test that only ever exercises one of them.

The distinction the engine draws, and the one these tests pin:

* text that IS the question          -> local shortcuts are trusted
* a caption that might contradict it -> local arithmetic is not trusted
* decoration only                    -> the vision model answers
"""

from __future__ import annotations

import httpx
import pytest

from qna_bot.ai.engine import AIEngine
from qna_bot.ai.market import MarketFeed, parse_period_query
from qna_bot.storage.database import GroupConfig
from qna_bot.userbot.filters import should_evaluate_message

JPEG = b"\xff\xd8\xff\xe0 synthetic"
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


def _engine(ocr: str) -> tuple[AIEngine, list[str]]:
    """Engine whose vision model only ever reports `ocr`."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "yahoo" in url:
            seen.append("yahoo")
            return httpx.Response(200, json=PAYLOAD)
        body = request.read().decode("utf-8", "replace")
        if AIEngine.TRANSCRIBE_PROMPT in body:
            seen.append("transcribe")
            return httpx.Response(
                200, json={"choices": [{"message": {"content": ocr}, "finish_reason": "stop"}]}
            )
        seen.append("answer")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "VISION"}, "finish_reason": "stop"}]}
        )

    transport = httpx.MockTransport(handler)
    engine = AIEngine(market_feed=MarketFeed(httpx.AsyncClient(transport=transport)))
    engine._client = httpx.AsyncClient(transport=transport)
    return engine, seen


async def _ask(engine: AIEngine, *, text: str = "", image: bytes | None = JPEG):
    return await engine.answer_question(
        text=text,
        forex_group=True,
        live_data=True,
        answer_style="giveaway",
        primary_provider="groq",
        primary_model="openai/gpt-oss-120b",
        provider_keys={"groq": "k"},
        image=image,
        image_mime="image/jpeg",
    )


#: Posters as OCR would report them, each a real layout a giveaway uses.
POSTER_LAYOUTS = {
    "single line": "On the Exness BTC chart, what was the highest price this week?",
    "stacked lines": (
        "EYUEL FX\nThe 20$ Question\nOn the Exness BTC chart,\n"
        "what was the highest price\nrecorded this week?"
    ),
    "split mid-phrase": "On the Exness\nBTC chart, what was\nthe highest price\nthis week?",
    "numbered among others": (
        "1. What is the capital of Ethiopia?\n"
        "2. On the Exness BTC chart, what was the highest price this week?\n"
        "3. Name two currency pairs"
    ),
    "with dollar prefix": "What was the highest price of $BTC this week?",
    "btcusdt form": "on BTCUSDT what was the high this week",
    "hashtags and dashes": "#Giveaway on the #BTC chart — what was the highest price this week?",
    "no question mark": "On the Exness BTC chart, what was the highest price recorded this week",
    "exclamation": "On the Exness BTC chart, what was the highest price recorded this week!",
}

#: Amharic posters, including one that mixes scripts.
AMHARIC_LAYOUTS = {
    "amharic with latin ticker": "በExness BTC ገበታ ላይ ይህ ሳምንት ከፍተኛው ዋጋ ስንት ነበር?",
    "amharic ticker": "ቢቲሲ ገበታ ላይ ይህ ሳምንት ከፍተኛው ዋጋ ስንት ነው?",
    "amharic last week": "ወርቅ ያለፈው ሳምንት ዝቅተኛው ዋጋ",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", POSTER_LAYOUTS.values(), ids=POSTER_LAYOUTS.keys())
async def test_every_english_poster_layout_is_answered_locally(layout: str) -> None:
    engine, seen = _engine(layout)
    result = await _ask(engine)
    assert result.answer == "93125.75", layout
    assert result.provider == "local_market", layout
    # One transcription, then arithmetic. The model never answers.
    assert seen.count("answer") == 0, seen


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", AMHARIC_LAYOUTS.values(), ids=AMHARIC_LAYOUTS.keys())
async def test_amharic_posters_are_answered_locally(layout: str) -> None:
    """An Amharic giveaway poster asks the same question in Ethiopic script."""
    engine, seen = _engine(layout)
    result = await _ask(engine)
    assert result.provider == "local_market", layout
    assert seen.count("answer") == 0, seen


# ---------------------------------------------------------------------------
# Amharic market vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "period", "aggregation", "instrument"),
    [
        ("ይህ ሳምንት ከፍተኛው የBTC ዋጋ", "this_week", "high", "BTCUSD"),
        ("ያለፈው ሳምንት ዝቅተኛው ዋጋ BTC", "last_week", "low", "BTCUSD"),
        ("ባለፈው ሳምንት የዘጋው ዋጋ ETH", "last_week", "close", "ETHUSD"),
        ("ዛሬ የተከፈተው ዋጋ ወርቅ", "today", "open", "XAUUSD"),
        ("ይህ ወር ከፍተኛው የBTC ዋጋ", "this_month", "high", "BTCUSD"),
    ],
)
def test_amharic_period_questions_parse(
    text: str, period: str, aggregation: str, instrument: str
) -> None:
    query = parse_period_query(text)
    assert query is not None, text
    assert (query.period, query.aggregation, query.instrument) == (
        period,
        aggregation,
        instrument,
    )


def test_ethiopic_prefix_does_not_hide_a_ticker() -> None:
    """"በBTC ገበታ" ("on the BTC chart") has no \\b before the B.

    A word boundary is defined against Ethiopic letters too, so the plain
    pattern silently failed on every Amharic sentence that inflects a ticker
    with a prefix - which Amharic does constantly.
    """
    from qna_bot.ai.market import instruments_in

    assert instruments_in("በBTC ገበታ ላይ") == ["BTCUSD"]
    assert instruments_in("BTC ገበታ") == ["BTCUSD"]


def test_ticker_matching_still_requires_token_boundaries() -> None:
    """The looseness must not turn arbitrary prose into a ticker."""
    from qna_bot.ai.market import instruments_in

    assert instruments_in("the BTCDOM index") == []
    assert instruments_in("ETHOS") == []


# ---------------------------------------------------------------------------
# The caption / image disagreement rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_math_caption_defers_to_the_picture() -> None:
    """The picture can contradict a short caption, so the picture wins.

    A caption "47 * 83" under a poster reading "2 + 2 = ?" must answer 4, not
    3901. The stub rule transcribes it, and the transcribed text is then the one
    the arithmetic is run on - so the answer follows what the image actually
    shows rather than what the wrapper says.
    """
    engine, _ = _engine("2 + 2 = ?")
    result = await _ask(engine, text="47 * 83")
    assert result.answer == "4"


@pytest.mark.asyncio
async def test_long_caption_that_is_a_question_is_not_overridden_by_the_image() -> None:
    """A real question in the caption is left alone.

    Only thin captions are treated as wrappers. A caption that is a genuine
    question longer than the stub ceiling keeps taking the ordinary path, so a
    poster with a real question written *under* it is answered as written.
    """
    caption = "Answer this one for the giveaway: what was the highest price of BTC this week?"
    engine, seen = _engine("2 + 2 = ?")
    result = await _ask(engine, text=caption)
    assert "transcribe" not in seen
    assert result.provider == "local_market"


@pytest.mark.asyncio
async def test_arithmetic_printed_on_the_photo_is_solved_locally() -> None:
    """When the sum is the image's own words, there is nothing to contradict."""
    engine, seen = _engine("47 * 83")
    result = await _ask(engine)
    assert result.answer == "3901"
    assert result.provider == "local_math"
    assert seen.count("answer") == 0


@pytest.mark.asyncio
async def test_text_only_questions_keep_every_shortcut() -> None:
    """No image: nothing changed for the paths that already worked."""
    engine, seen = _engine("unused")
    result = await _ask(engine, text="47 * 83", image=None)
    assert result.provider == "local_math"
    assert seen == []


@pytest.mark.asyncio
async def test_pure_decoration_still_gets_a_vision_answer() -> None:
    """A meme or photo with no readable question must not be refused."""
    engine, seen = _engine("NONE")
    result = await _ask(engine)
    assert result.answer == "VISION"
    assert result.provider == "groq"
    assert "transcribe" in seen


@pytest.mark.asyncio
async def test_ocr_reports_no_text_by_saying_none() -> None:
    for refusal in ("NONE", "none.", "No text", "No legible text at all."):
        engine, seen = _engine(refusal)
        result = await _ask(engine)
        assert result.answer == "VISION", refusal
        assert result.provider == "groq", refusal


# ---------------------------------------------------------------------------
# Filters: photo posts that carry only a stub caption
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "caption",
    ["answer below", "🔥🔥🔥", "📊 @eyuelfx", "Rules in pinned", "👇", "Read the card"],
)
def test_stub_caption_beside_a_photo_is_still_a_photo_question(caption: str) -> None:
    """The caption wraps the media; the question is on the picture.

    Every one of these was dropped as "too short and not a question" before the
    engine ever saw the photo.
    """
    group = GroupConfig(chat_id=-100, title="FX", enabled=True, forex_enabled=True)
    decision = should_evaluate_message(caption, group, has_image=True)
    assert decision.should_process, caption
    assert decision.reason == "image_caption_stub"


def test_stub_rule_does_not_apply_to_typed_text() -> None:
    """Without a photo, a stub is judged by the text rules like anything else.

    "answer below" and "👇" stay dropped; a three-word phrase still passes under
    aggressive mode as it always did. The point is that the new rule changes
    nothing at all when there is no image.
    """
    group = GroupConfig(chat_id=-100, title="FX", enabled=True)
    for caption in ("answer below", "👇"):
        assert not should_evaluate_message(caption, group, has_image=False).should_process

    with_image = should_evaluate_message("👇", group, has_image=True)
    assert with_image.reason == "image_caption_stub"


def test_a_real_question_in_the_caption_still_wins() -> None:
    """A short caption that IS a question gets recognised as one."""
    group = GroupConfig(chat_id=-100, title="FX", enabled=True)
    decision = should_evaluate_message("what was the high today?", group, has_image=True)
    assert decision.should_process
    assert decision.reason == "giveaway_signal"


def test_math_caption_keeps_the_math_reason() -> None:
    group = GroupConfig(chat_id=-100, title="FX", enabled=True, local_math_enabled=True)
    decision = should_evaluate_message("47 * 83", group, has_image=True)
    assert decision.should_process
    assert decision.reason == "math_detected"


def test_long_marketing_caption_beside_a_photo_still_passes() -> None:
    group = GroupConfig(chat_id=-100, title="FX", enabled=True)
    caption = (
        "EYUEL FX presents The 20$ Question. Follow us for more daily market "
        "questions and tag a friend to win. Rules: one answer per person, no "
        "edits, winner announced tomorrow evening. Powered by Exness."
    )
    assert should_evaluate_message(caption, group, has_image=True).should_process


def test_stub_rule_yields_to_photo_answers_being_off() -> None:
    """The per-group vision toggle still wins over everything here."""
    group = GroupConfig(chat_id=-100, title="FX", enabled=True, vision_enabled=False)
    decision = should_evaluate_message("answer below", group, has_image=True)
    assert not decision.should_process
    assert decision.reason == "vision_disabled"


# ---------------------------------------------------------------------------
# What counts as a photo at all
# ---------------------------------------------------------------------------


def _document(mime: str):
    import datetime

    from telethon.tl.types import Document

    return Document(
        id=1,
        access_hash=1,
        file_reference=b"",
        date=datetime.datetime.now(),
        mime_type=mime,
        size=10,
        dc_id=1,
        attributes=[],
    )


def _event_with(document):
    class _Msg:
        document = None
        photo = None

    _Msg.document = document
    return type("_Event", (), {"message": _Msg()})()


@pytest.mark.parametrize(
    "mime",
    ["image/jpeg", "image/png", "image/webp", "image/heic"],
)
def test_common_image_formats_count_as_photos(mime: str) -> None:
    """"Export as image" produces all of these; all can hold a question."""
    from qna_bot.userbot.manager import UserbotManager

    assert UserbotManager._message_has_image(_event_with(_document(mime))), mime


@pytest.mark.parametrize("mime", ["image/gif", "application/x-tgsticker", "video/mp4"])
def test_decoration_formats_are_not_treated_as_photo_questions(mime: str) -> None:
    """A sticker is not a poster.

    Stickers arrive as image/* documents. Now that a short caption beside a photo
    is treated as a real question, a "🔥🔥" caption on a sticker would otherwise
    trigger a vision answer on every reaction in the group.
    """
    from qna_bot.userbot.manager import UserbotManager

    assert not UserbotManager._message_has_image(_event_with(_document(mime))), mime


def test_plain_text_message_is_not_a_photo() -> None:
    from qna_bot.userbot.manager import UserbotManager

    event = type("_Event", (), {"message": type("_Msg", (), {"document": None, "photo": None})()})()
    assert not UserbotManager._message_has_image(event)


# ---------------------------------------------------------------------------
# Captions that are long but say nothing
# ---------------------------------------------------------------------------

PROMO = (
    "EYUEL FX presents The 20$ Question. Follow us for more daily market "
    "questions and tag a friend to win. Rules: one answer per person, no edits, "
    "winner announced tomorrow evening."
)


@pytest.mark.asyncio
async def test_long_promo_caption_still_reads_the_poster() -> None:
    """A caption longer than the stub ceiling is not automatically better.

    Announcement text wrapping a poster can be long and question-free. Judging
    by length alone sent these to a vision model that answered from artwork,
    while the computable question sat printed on the card.
    """
    engine, seen = _engine(POSTER_LAYOUTS["single line"])
    result = await _ask(engine, text=PROMO)
    assert result.provider == "local_market"
    assert result.answer == "93125.75"
    assert "transcribe" in seen


@pytest.mark.asyncio
async def test_long_promo_caption_over_pure_decoration_still_answers() -> None:
    engine, _ = _engine("NONE")
    result = await _ask(engine, text=PROMO)
    assert result.answer == "VISION"
    assert result.provider == "groq"


@pytest.mark.asyncio
async def test_real_question_in_a_long_caption_costs_no_extra_call() -> None:
    """When the caption already holds a question, do not pay for OCR."""
    caption = POSTER_LAYOUTS["single line"] + " " + PROMO
    engine, seen = _engine("NONE")
    result = await _ask(engine, text=caption)
    assert result.provider == "local_market"
    assert "transcribe" not in seen


@pytest.mark.parametrize(
    ("text", "answerable"),
    [
        ("what was the high this week?", True),
        ("47 * 83", True),
        ("btc high last week", True),
        ("EURUSD pip value 1 lot", True),
        ("follow us for more daily questions", False),
        ("", False),
        ("🔥🔥🔥", False),
    ],
)
def test_answerable_text_detection(text: str, answerable: bool) -> None:
    assert AIEngine._has_answerable_text(text) is answerable


# ---------------------------------------------------------------------------
# The prompt the vision model finally sees
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcribed_question_is_flagged_as_coming_from_the_image() -> None:
    """Otherwise the model reads the same words twice and describes the picture.

    The transcribed text goes into the prompt *and* the image is attached. A
    model given both, without being told one is a reading of the other, tends to
    paraphrase the poster instead of answering the question on it.
    """
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "yahoo" in url:
            return httpx.Response(200, json=PAYLOAD)
        import json

        body = json.loads(request.read())
        bodies.append(body)
        if AIEngine.TRANSCRIBE_PROMPT in body["messages"][0]["content"]:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "no readable question"}, "finish_reason": "stop"}
                    ]
                },
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}
        )

    transport = httpx.MockTransport(handler)
    engine = AIEngine(market_feed=MarketFeed(httpx.AsyncClient(transport=transport)))
    engine._client = httpx.AsyncClient(transport=transport)

    result = await _ask(engine, text="answer below")
    assert result.answered
    answer_call = bodies[-1]
    parts = answer_call["messages"][1]["content"]
    text_part = next(p for p in parts if p["type"] == "text")["text"]
    assert "transcribed verbatim" in text_part
    # The image is still attached: the model may need to read values off a chart.
    assert any(p["type"] == "image_url" for p in parts)


def test_user_prompt_marks_image_text_without_context() -> None:
    prompt = AIEngine._build_user_prompt("what was the high?", None, from_image=True)
    assert "transcribed verbatim" in prompt
    assert prompt.rstrip().endswith("what was the high?")


def test_user_prompt_is_unchanged_for_typed_questions() -> None:
    assert AIEngine._build_user_prompt("what was the high?", None) == "what was the high?"


def test_user_prompt_keeps_context_alongside_the_image_note() -> None:
    prompt = AIEngine._build_user_prompt(
        "what was the high?", ["host: question 4 is live"], from_image=True
    )
    assert "Recent group context" in prompt
    assert "transcribed verbatim" in prompt


@pytest.mark.asyncio
async def test_amharic_transcription_gets_amharic_token_headroom() -> None:
    """Language detection must follow the transcribed text, not the empty caption."""
    from qna_bot.ai.engine import AMHARIC_TOKEN_HEADROOM

    seen_tokens: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        if "yahoo" in str(request.url):
            return httpx.Response(200, json=PAYLOAD)
        body = json.loads(request.read())
        if AIEngine.TRANSCRIBE_PROMPT in body["messages"][0]["content"]:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "በዚህ ምስል ላይ ጥያቄ የለም"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        seen_tokens.append(body["max_tokens"])
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "እሺ"}, "finish_reason": "stop"}]}
        )

    transport = httpx.MockTransport(handler)
    engine = AIEngine(market_feed=MarketFeed(httpx.AsyncClient(transport=transport)))
    engine._client = httpx.AsyncClient(transport=transport)

    await _ask(engine)
    assert seen_tokens, "no answer call was made"
    assert seen_tokens[-1] >= 48 + AMHARIC_TOKEN_HEADROOM
