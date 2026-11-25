"""Photo questions that are really market questions.

The reported case was a giveaway post: a decorative bull/bear poster with the
text *"On the Exness BTC chart, what was the highest price recorded this week?"*
overlaid on it.

The bot was already able to answer that question exactly - it computes the high
from a real OHLC series with no AI call - and the bot was already able to send a
photo to a vision model. Both halves worked. What failed is the join between
them: every local shortcut in the engine carried `and not image`, so attaching a
picture *disabled the accurate path* and handed the question to a model that was
looking at artwork. The one answer that could be proven was the one that got
guessed.

Three separate faults are pinned here:

1. `and not image` on the period-question fast path, so a photo skipped it.
2. The same guard on the market snapshot, so a photo question never saw the feed.
3. `group_guidance` *replacing* the live-data rule with a chart-only rule, so a
   decorative poster was described to the model as "the picture is the data".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from exness_fixtures import (
    daily_bars_from_yahoo_shaped,
    exness_response,
    is_exness_request,
    settings_loader,
)

from qna_bot.ai.engine import AIEngine
from qna_bot.ai.forex import (
    NO_LIVE_DATA_GUIDANCE,
    group_guidance,
)
from qna_bot.ai.market import MarketFeed, PeriodQuery, parse_period_query

#: The question from the screenshot, word for word.
REPORTED_QUESTION = "On the Exness BTC chart, what was the highest price recorded this week?"

#: A tiny stand-in for a JPEG. Nothing decodes it; the engine only needs bytes.
FAKE_POSTER = b"\xff\xd8\xff\xe0 poster-with-a-question-on-it"

MONDAY = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(
    days=datetime.now(UTC).weekday()
)


def _bar(day_offset: int, high: float, low: float, open_: float, close: float) -> dict:
    return {
        "ts": int((MONDAY + timedelta(days=day_offset)).timestamp()),
        "high": high,
        "low": low,
        "open": open_,
        "close": close,
    }


def _payload(bars: list[dict]) -> dict:
    return {
        "chart": {
            "result": [
                {
                    "timestamp": [b["ts"] for b in bars],
                    "indicators": {
                        "quote": [
                            {
                                "high": [b["high"] for b in bars],
                                "low": [b["low"] for b in bars],
                                "open": [b["open"] for b in bars],
                                "close": [b["close"] for b in bars],
                            }
                        ]
                    },
                }
            ]
        }
    }


def _transport(payload: dict, calls: list[str]) -> httpx.MockTransport:
    """Exness answers with the OHLC series; any model call returns WRONG."""

    daily = daily_bars_from_yahoo_shaped(payload)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if is_exness_request(str(request.url)):
            return exness_response(request, daily)
        # No model may be called for a question that can be computed.
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "WRONG"}, "finish_reason": "stop"}]}
        )

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# The exact reported case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_photo_market_question_is_computed_not_guessed() -> None:
    """"highest price this week" over a *poster image* must use the real series."""
    badges = [
        _bar(0, 91000.0, 89500.0, 90000.0, 90900.0),
        _bar(1, 92550.5, 90200.0, 91000.0, 92500.0),
        _bar(2, 91800.0, 90500.0, 92550.5, 91700.0),
    ]
    calls: list[str] = []
    feed = MarketFeed(
        httpx.AsyncClient(transport=_transport(_payload(badges), calls)),
        settings_loader=settings_loader(),
    )
    engine = AIEngine(market_feed=feed)
    engine._client = httpx.AsyncClient(transport=_transport(_payload(badges), calls))

    result = await engine.answer_question(
        text=REPORTED_QUESTION,
        forex_group=True,
        live_data=True,
        answer_style="giveaway",
        primary_provider="groq",
        primary_model="openai/gpt-oss-120b",
        provider_keys={"groq": "k"},
        image=FAKE_POSTER,
    )

    assert result.answered is True
    assert result.answer == "92550.50"
    assert result.provider == "local_market"
    # Proof that the answer came from the series, not a model looking at artwork.
    assert any(is_exness_request(url) for url in calls), calls
    assert not any("groq" in url or "/chat/completions" in url for url in calls), calls


@pytest.mark.asyncio
async def test_photo_does_not_disable_the_period_shortcut_but_text_still_works() -> None:
    """The same question with no image must keep taking the same path."""
    bars = [_bar(0, 70000.0, 69000.0, 69500.0, 69900.0)]
    calls: list[str] = []
    feed = MarketFeed(
        httpx.AsyncClient(transport=_transport(_payload(bars), calls)), settings_loader=settings_loader()
    )
    engine = AIEngine(market_feed=feed)
    engine._client = httpx.AsyncClient(transport=_transport(_payload(bars), calls))

    result = await engine.answer_question(
        text=REPORTED_QUESTION,
        forex_group=True,
        live_data=True,
        answer_style="giveaway",
        primary_provider="groq",
        primary_model="openai/gpt-oss-120b",
        provider_keys={"groq": "k"},
    )
    assert result.answer == "70000.00"
    assert result.provider == "local_market"


# ---------------------------------------------------------------------------
# "this week" is a calendar week, not a trailing seven days
# ---------------------------------------------------------------------------


def test_this_week_is_a_calendar_week() -> None:
    query = parse_period_query("btc high this week")
    assert query is not None
    assert query.period == "this_week"
    assert query.is_calendar_week is True


def test_last_week_is_also_a_calendar_week() -> None:
    """"Last week" used to be a trailing seven days ending on an arbitrary
    weekday - the bug that returned the wrong week's high. It is now the
    previous Monday-to-Friday, exactly like "this week" is the current one."""
    query = parse_period_query("btc high last week")
    assert query is not None
    assert query.is_calendar_week is True


@pytest.mark.asyncio
async def test_this_week_excludes_the_weekend_before_it() -> None:
    """On a Monday, "this week" must not include last Friday's high.

    This is the failure a trailing-seven window produces, and it is the worst
    kind for a competition: plausible, and wrong.
    """
    last_friday = MONDAY - timedelta(days=3)
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [
                        int(last_friday.timestamp()),
                        int(last_friday.replace(hour=23).timestamp()),
                        int(MONDAY.timestamp()),
                    ],
                    "indicators": {
                        "quote": [
                            {
                                # Friday's high is much larger and must NOT win.
                                "high": [99000.0, 98000.0, 71000.0],
                                "low": [96000.0, 97000.0, 70000.0],
                                "open": [97000.0, 98500.0, 70500.0],
                                "close": [98500.0, 97500.0, 70800.0],
                            }
                        ]
                    },
                }
            ]
        }
    }
    calls: list[str] = []
    feed = MarketFeed(
        httpx.AsyncClient(transport=_transport(payload, calls)), settings_loader=settings_loader()
    )
    answer = await feed.period_answer(
        PeriodQuery(period="this_week", aggregation="high", instrument="BTCUSD")
    )
    assert answer is not None
    assert answer.high == pytest.approx(71000.0)


@pytest.mark.asyncio
async def test_this_week_with_no_bars_yet_falls_back_to_latest_session() -> None:
    """A brand-new week must still produce a number rather than losing the race."""
    last_week = MONDAY - timedelta(days=2)
    payload = _payload([_bar(0, 100.0, 90.0, 95.0, 99.0)])
    payload["chart"]["result"][0]["timestamp"] = [int(last_week.timestamp())]
    calls: list[str] = []
    feed = MarketFeed(
        httpx.AsyncClient(transport=_transport(payload, calls)), settings_loader=settings_loader()
    )
    answer = await feed.period_answer(
        PeriodQuery(period="this_week", aggregation="high", instrument="BTCUSD")
    )
    assert answer is not None
    assert answer.high == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Prompt composition: a photo must not silence the market rules
# ---------------------------------------------------------------------------


def test_prompt_with_photo_and_feed_contains_both_rules() -> None:
    class _Snapshot:
        def as_prompt_block(self) -> str:
            return "LIVE MARKET DATA (the only prices you may state):\n- BTCUSD 66120.50"

    prompt = AIEngine._build_system_prompt(
        "giveaway",
        REPORTED_QUESTION,
        forex_group=True,
        market=_Snapshot(),  # type: ignore[arg-type]
        has_image=True,
    )
    assert "LIVE MARKET DATA" in prompt
    assert "price axis" in prompt
    assert "poster" in prompt.lower()
    # The refusal rule must be gone in both cases.
    assert NO_LIVE_DATA_GUIDANCE not in prompt


def test_prompt_with_photo_and_no_feed_is_honest_about_posters() -> None:
    prompt = AIEngine._build_system_prompt(
        "giveaway", "what is gold doing now?", forex_group=True, has_image=True
    )
    assert "do NOT invent one" in prompt
    assert "poster" in prompt.lower()


def test_photo_guidance_never_calls_the_picture_the_feed() -> None:
    """The old rule said "the picture IS the data", which invited a made-up price."""
    guidance = group_guidance(has_live_data=False, has_image=True)
    assert "image is the data" not in guidance.lower()
    assert "ARE the data" not in guidance


# ---------------------------------------------------------------------------
# Filters: a caption + photo is a question
# ---------------------------------------------------------------------------


def test_captioned_photo_passes_in_a_forex_group() -> None:
    from qna_bot.storage.database import GroupConfig
    from qna_bot.userbot.filters import should_evaluate_message

    group = GroupConfig(chat_id=-100, title="FX", enabled=True, forex_enabled=True)
    for mode in ("giveaway_aggressive", "balanced", "strict"):
        group.detection_mode = mode
        decision = should_evaluate_message(REPORTED_QUESTION, group, has_image=True)
        assert decision.should_process, (mode, decision.reason)


def test_photo_without_a_caption_is_still_a_candidate() -> None:
    from qna_bot.storage.database import GroupConfig
    from qna_bot.userbot.filters import should_evaluate_message

    group = GroupConfig(chat_id=-100, title="FX", enabled=True, forex_enabled=True)
    decision = should_evaluate_message("", group, has_image=True)
    assert decision.should_process
    assert decision.reason == "image_only"


def test_photo_is_ignored_when_photo_answers_are_off() -> None:
    from qna_bot.storage.database import GroupConfig
    from qna_bot.userbot.filters import should_evaluate_message

    group = GroupConfig(chat_id=-100, title="FX", enabled=True, vision_enabled=False)
    decision = should_evaluate_message(REPORTED_QUESTION, group, has_image=True)
    assert not decision.should_process
    assert decision.reason == "vision_disabled"


# ---------------------------------------------------------------------------
# The question printed inside the picture, with no caption at all
# ---------------------------------------------------------------------------


def _vision_transport(question: str, payload: dict, calls: list[str]) -> httpx.MockTransport:
    """Exness answers with OHLC; the model only ever transcribes."""

    daily = daily_bars_from_yahoo_shaped(payload)

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8", "replace")
        calls.append(str(request.url))
        if is_exness_request(str(request.url)):
            return exness_response(request, daily)
        if AIEngine.TRANSCRIBE_PROMPT in body:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": question}, "finish_reason": "stop"}]},
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "GUESS"}, "finish_reason": "stop"}]}
        )

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_uncaptioned_poster_question_is_read_then_computed() -> None:
    """The reported poster: no caption, question printed on the artwork."""
    bars = [
        _bar(0, 91000.0, 89500.0, 90000.0, 90900.0),
        _bar(1, 92550.5, 90200.0, 91000.0, 92500.0),
        _bar(2, 91800.0, 90500.0, 92550.5, 91700.0),
    ]
    payload = _payload(bars)
    calls: list[str] = []
    transport = _vision_transport(REPORTED_QUESTION, payload, calls)
    feed = MarketFeed(httpx.AsyncClient(transport=transport), settings_loader=settings_loader())
    engine = AIEngine(market_feed=feed)
    engine._client = httpx.AsyncClient(transport=transport)

    result = await engine.answer_question(
        text="",
        forex_group=True,
        live_data=True,
        answer_style="giveaway",
        primary_provider="groq",
        primary_model="openai/gpt-oss-120b",
        provider_keys={"groq": "k"},
        image=FAKE_POSTER,
    )

    assert result.answer == "92550.50"
    assert result.provider == "local_market"
    # One transcription call, no "real" answer call.
    model_calls = [url for url in calls if "groq.com" in url]
    assert len(model_calls) == 1


@pytest.mark.asyncio
async def test_uncaptioned_decorative_image_still_gets_a_vision_answer() -> None:
    """When the picture holds no question, fall through to the old behaviour."""
    calls: list[str] = []
    transport = _vision_transport("NONE", _payload([_bar(0, 100.0, 90.0, 95.0, 99.0)]), calls)
    feed = MarketFeed(httpx.AsyncClient(transport=transport), settings_loader=settings_loader())
    engine = AIEngine(market_feed=feed)
    engine._client = httpx.AsyncClient(transport=transport)

    result = await engine.answer_question(
        text="",
        forex_group=True,
        live_data=True,
        answer_style="giveaway",
        primary_provider="groq",
        primary_model="openai/gpt-oss-120b",
        provider_keys={"groq": "k"},
        image=FAKE_POSTER,
    )
    assert result.answer == "GUESS"
    assert result.provider == "groq"


@pytest.mark.asyncio
async def test_ocr_is_not_attempted_when_a_caption_exists() -> None:
    """A caption is free text already; a picture of one must not cost a call."""
    calls: list[str] = []
    bars = [_bar(0, 70000.0, 69000.0, 69500.0, 69900.0)]
    transport = _vision_transport(REPORTED_QUESTION, _payload(bars), calls)
    feed = MarketFeed(httpx.AsyncClient(transport=transport), settings_loader=settings_loader())
    engine = AIEngine(market_feed=feed)
    engine._client = httpx.AsyncClient(transport=transport)

    result = await engine.answer_question(
        text=REPORTED_QUESTION,
        forex_group=True,
        live_data=True,
        answer_style="giveaway",
        primary_provider="groq",
        primary_model="openai/gpt-oss-120b",
        provider_keys={"groq": "k"},
        image=FAKE_POSTER,
    )
    assert result.provider == "local_market"
    assert not [url for url in calls if "groq.com" in url]


@pytest.mark.asyncio
async def test_transcription_failure_falls_back_instead_of_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if is_exness_request(str(request.url)):
            return exness_response(
                request, daily_bars_from_yahoo_shaped(_payload([_bar(0, 100.0, 90.0, 95.0, 99.0)]))
            )
        body = request.read().decode("utf-8", "replace")
        if AIEngine.TRANSCRIBE_PROMPT in body:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "FALLBACK"}, "finish_reason": "stop"}]}
        )

    transport = httpx.MockTransport(handler)
    feed = MarketFeed(httpx.AsyncClient(transport=transport), settings_loader=settings_loader())
    engine = AIEngine(market_feed=feed)
    engine._client = httpx.AsyncClient(transport=transport)

    result = await engine.answer_question(
        text="",
        forex_group=True,
        live_data=True,
        answer_style="giveaway",
        primary_provider="groq",
        primary_model="openai/gpt-oss-120b",
        provider_keys={"groq": "k"},
        image=FAKE_POSTER,
    )
    assert result.answer == "FALLBACK"


@pytest.mark.asyncio
async def test_transcribe_returns_empty_without_a_vision_model() -> None:
    engine = AIEngine(market_feed=MarketFeed())
    engine._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    out = await engine._transcribe_image(
        attempts=[("groq", "openai/gpt-oss-20b", "k")], image=FAKE_POSTER
    )
    # gpt-oss-20b has no vision sibling: nothing to call, so nothing returned.
    assert out in ("", "NONE")
