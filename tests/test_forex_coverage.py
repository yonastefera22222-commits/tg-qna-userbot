"""Coverage of the Forex question types a giveaway actually asks.

Three things are being protected here:

1. **Recognition** - every vocabulary a Forex host uses must reach the Forex
   path, or the question is answered as general trivia (or dropped).
2. **Chart photos** - a photographed chart carries its own numbers, so the
   "no live feed" refusal must not fire on one.
3. **Exact arithmetic** - pip value, pip distance, lot size and risk % are
   formulas, computed locally with no model call.
"""

from __future__ import annotations

import pytest

from qna_bot.ai.engine import AIEngine
from qna_bot.ai.forex import (
    IMAGE_GUIDANCE,
    LIVE_DATA_IN_IMAGE_GUIDANCE,
    LIVE_DATA_PRESENT_GUIDANCE,
    NO_LIVE_DATA_GUIDANCE,
    group_guidance,
    is_forex_question,
)
from qna_bot.ai.forex_math import LOT_UNITS, pip_size, solve_forex_math
from qna_bot.storage.database import GroupConfig
from qna_bot.userbot.filters import should_evaluate_message as filter_message

# ---------------------------------------------------------------------------
# Recognition: the full vocabulary of a Forex giveaway
# ---------------------------------------------------------------------------

DEFINITIONS = [
    "what is a pip", "what is a pipette", "define leverage", "what is margin",
    "what is free margin", "what is a margin call", "what is the spread",
    "what is slippage", "what is swap", "what is rollover", "what is triple swap",
    "what is lot size", "what is a micro lot", "what is contract size",
    "what is notional", "what is a cross pair", "what is the base currency",
    "what is the quote currency", "what is an exotic pair", "what is a major pair",
    "what is a bull flag", "what is a golden cross", "what is a death cross",
    "what is a doji", "what is a head and shoulders", "what is support and resistance",
    "what is a breakout", "what is a retest", "what is a pullback", "what is fakeout",
    "what is liquidity", "what is order flow", "what is open interest",
]
INDICATORS = [
    "what does RSI above 70 mean", "what is a moving average", "what is MACD",
    "what is bollinger bands", "what is ATR", "what is ichimoku", "what is divergence",
    "what does overbought mean", "what does oversold mean",
]
PATTERNS = [
    "what is a wedge", "what is a pennant", "what is a channel", "what is a trendline",
    "what is a pivot point", "what is a double top", "what is a cup and handle",
    "what is a round number", "what is a psychological level",
]
MACRO = [
    "what is NFP", "what is CPI", "what does hawkish mean", "what does dovish mean",
    "what is quantitative easing", "what is tapering", "what is a dot plot",
    "what is basis point", "what is real yield", "what is bond yield",
    "what is the Fed", "what is the ECB", "what is the BOJ", "what is FOMC",
    "what is risk-off", "what is a safe haven", "what is carry trade",
    "what is current account", "what is trade balance", "what is GDP",
    "what is purchasing power", "what is inflation expectation",
]
EVENTS = [
    "what time is the FOMC", "what is an economic calendar", "what is red news",
    "what is a high impact news", "what is an interest rate decision",
    "what is a press conference", "what is jobless claims", "what is retail sales",
]
SESSIONS = [
    "when is the london session", "what is the tokyo session",
    "which session is most volatile", "what is the new york session",
]
TRADING = [
    "what is a trailing stop", "what is risk management", "what is position size",
    "what is a stop hunt", "what is a whipsaw", "what is a weekend gap",
    "what is a long position", "what is a short position", "what is net long",
    "which pair is the most traded", "what is the most liquid pair",
]
MARKETS = [
    "what is XAUUSD", "gold price", "what is the DXY", "what is the dollar index",
    "BTC high last week", "what is ethereum doing", "silver resistance",
]


@pytest.mark.parametrize(
    "text", DEFINITIONS + INDICATORS + PATTERNS + MACRO + EVENTS + SESSIONS
    + TRADING + MARKETS
)
def test_recognises_forex_vocabulary(text: str) -> None:
    assert is_forex_question(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "what is the capital of Kenya",
        "who won the world cup",
        "how many legs does a spider have",
        "first comment",
        "hello everyone",
        "what is 47 * 83",
        "ስንት ነው?",
    ],
)
def test_non_forex_is_still_ignored(text: str) -> None:
    assert is_forex_question(text) is False


# ---------------------------------------------------------------------------
# Chart photos: the image is the feed
# ---------------------------------------------------------------------------


def test_chart_photo_does_not_get_the_refusal_rule() -> None:
    """A chart photo was answered "No live data" while the price was visible."""
    prompt = AIEngine._build_system_prompt(
        "giveaway", "what was the high here?", forex_group=True, has_image=True
    )
    assert NO_LIVE_DATA_GUIDANCE not in prompt
    assert "image" in prompt.lower()
    # The softened rule allows a visible figure but still forbids inventing one.
    assert "do NOT invent one" in prompt


def test_text_question_still_gets_the_refusal_rule() -> None:
    prompt = AIEngine._build_system_prompt(
        "giveaway", "what was the high last week?", forex_group=True
    )
    assert NO_LIVE_DATA_GUIDANCE in prompt


def test_image_guidance_tells_the_model_what_to_read() -> None:
    for token in ("price axis", "candle", "instrument", "timeframe"):
        assert token in IMAGE_GUIDANCE


def test_image_guidance_warns_that_a_poster_carries_no_price() -> None:
    """The reported case was a *decorative* poster with the question printed on it.

    Treating every attached image as a chart invited the model to read a number
    off the artwork, which is exactly the fabrication the group rules forbid.
    """
    lowered = IMAGE_GUIDANCE.lower()
    assert "poster" in lowered or "decoration" in lowered
    assert "do not read a number off the artwork" in lowered


def test_image_without_a_feed_softens_the_refusal_but_stays_honest() -> None:
    guidance = group_guidance(has_live_data=False, has_image=True)
    assert NO_LIVE_DATA_GUIDANCE not in guidance
    assert LIVE_DATA_IN_IMAGE_GUIDANCE in guidance
    assert "do NOT invent one" in guidance


def test_image_with_a_feed_keeps_the_live_rule_and_adds_the_image_rule() -> None:
    """The two blocks are additive: a chart *and* a feed must both be usable."""
    guidance = group_guidance(has_live_data=True, has_image=True)
    assert LIVE_DATA_PRESENT_GUIDANCE in guidance
    assert IMAGE_GUIDANCE in guidance
    assert NO_LIVE_DATA_GUIDANCE not in guidance


def test_image_rule_never_contradicts_the_feed() -> None:
    guidance = group_guidance(has_live_data=True, has_image=True)
    # "The picture is the data" must not survive alongside "use the feed".
    assert guidance.index(LIVE_DATA_PRESENT_GUIDANCE) < guidance.index(IMAGE_GUIDANCE)


def test_photo_with_caption_passes_the_filter_in_a_forex_group() -> None:
    group = GroupConfig(chat_id=-100, title="FX", enabled=True)
    group.forex_enabled = True
    group.detection_mode = "giveaway_aggressive"
    group.vision_enabled = True

    assert filter_message("what is this level", group, has_image=True).should_process
    # A chart with no caption at all is still a question worth answering.
    assert filter_message("", group, has_image=True).should_process


# ---------------------------------------------------------------------------
# Local arithmetic
# ---------------------------------------------------------------------------


def test_pip_sizes() -> None:
    assert pip_size("EURUSD") == 0.0001
    assert pip_size("GBPJPY") == 0.01
    assert pip_size("USDJPY") == 0.01
    assert pip_size("XAUUSD") == 0.1


def test_lot_sizes() -> None:
    assert LOT_UNITS["standard"] == 100_000
    assert LOT_UNITS["mini"] == 10_000
    assert LOT_UNITS["micro"] == 1_000


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("how many pips from 1.09850 to 1.09950", "10"),
        ("how many pips from 1.0985 to 1.0975", "10"),
        ("how many pips from 150.20 to 150.65 on USDJPY", "45"),
        ("what is 2% of a 5000 account", "100"),
        ("2% risk of 10000", "200"),
        ("what is the pip value on a standard lot EURUSD", "$10.00"),
        ("what is the pip value on a mini lot", "$1.00"),
        ("value of 1 pip on a micro lot", "$0.10"),
        ("how much is 20 pips on a micro lot", "$2.00"),
        ("how much is 50 pips on a standard lot", "$500.00"),
        ("what lot size for 1% risk with a 25 pip stop on a 10000 EURUSD account", "0.40"),
        ("what lot size for 2% risk with a 20 pip stop on a 5000 EURUSD account", "0.50"),
    ],
)
def test_forex_arithmetic(text: str, expected: str) -> None:
    assert solve_forex_math(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "what is a pip",
        "what is the capital of Kenya",
        "who won the match",
        "explain leverage",
        "first comment",
        # Needs a live exchange rate to answer; must fall through to the AI.
        "how much is 20 pips on a standard lot GBPJPY",
    ],
)
def test_forex_arithmetic_declines_when_it_cannot_be_exact(text: str) -> None:
    assert solve_forex_math(text) is None


def test_pip_math_beats_the_generic_solver() -> None:
    """'how many pips from 1.0985 to 1.0995' must not be read as plain math."""
    from qna_bot.ai.math_solver import try_solve_math

    text = "how many pips from 1.09850 to 1.09950"
    assert try_solve_math(text) is None
    assert solve_forex_math(text) == "10"


# ---------------------------------------------------------------------------
# Engine wiring
# ---------------------------------------------------------------------------


class _NoAI(AIEngine):
    def __init__(self) -> None:
        super().__init__()
        self.called = False

    async def _call_provider(self, **kwargs):  # type: ignore[override]
        self.called = True
        return "FROM_MODEL"


async def _ask(engine: AIEngine, text: str, *, forex_group: bool = True):
    return await engine.answer_question(
        text=text,
        forex_group=forex_group,
        answer_style="giveaway",
        primary_provider="groq",
        primary_model="openai/gpt-oss-120b",
        provider_keys={"groq": "k"},
        local_math_enabled=True,
    )


@pytest.mark.asyncio
async def test_forex_arithmetic_never_calls_the_ai() -> None:
    engine = _NoAI()
    try:
        result = await _ask(engine, "how many pips from 1.09850 to 1.09950")
        assert result.answer == "10"
        assert result.provider == "local_math"
        assert engine.called is False
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_plain_math_keeps_its_own_path() -> None:
    engine = _NoAI()
    try:
        result = await _ask(engine, "15% of 240")
        assert result.answer == "36"
        assert result.model == "ast-instant"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_forex_math_is_off_outside_forex_groups() -> None:
    """A non-Forex group must not answer '2% of a 5000 account' as $100."""
    engine = _NoAI()
    try:
        result = await _ask(engine, "2% of a 5000 account", forex_group=False)
        assert result.answer == "FROM_MODEL"
        assert engine.called is True
    finally:
        await engine.close()
