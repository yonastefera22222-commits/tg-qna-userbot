"""Live market feed: extraction, freshness labelling, prompting, sources.

Every number here comes from the Exness Trader API, so the fixtures below are
the documented Exness response shapes (access-point discovery -> instruments ->
candles) rather than another vendor's JSON. They are frozen so the parsing and
signing contract is tested without touching the network - which is required,
not merely convenient: the sandbox has no outbound TLS.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from exness_fixtures import (
    ACCOUNT_ID,
    API_KEY,
    GATEWAY,
    TEST_PRIVATE_KEY,
    FakeExness,
    candle_payload,
)

from qna_bot.ai import forex
from qna_bot.ai.engine import AIEngine
from qna_bot.ai.exness import ExnessCredentials
from qna_bot.ai.market import (
    STALE_AFTER_MINUTES,
    MarketFeed,
    MarketSnapshot,
    Quote,
    instruments_in,
    normalise_instrument,
)

CREDS = ExnessCredentials(
    api_key=API_KEY,
    private_key=TEST_PRIVATE_KEY,
    account_id=ACCOUNT_ID,
    gateway_url=GATEWAY,
)


def _settings(**overrides) -> dict:
    values = {
        "exness_api_key": CREDS.api_key,
        "exness_private_key": CREDS.private_key,
        "exness_account_id": CREDS.account_id,
        "exness_gateway_url": CREDS.gateway_url,
    }
    values.update(overrides)
    return values


def _exness_feed(fake: FakeExness, **overrides) -> MarketFeed:
    """A feed wired to a stubbed Exness account."""

    async def load() -> dict:
        return _settings(**overrides)

    return MarketFeed(fake.client(), settings_loader=load)


#: Nine daily BTC bars ending today (today's is still forming). Closes rise to
#: 61605.06 and the high of the *completed* week before today is 66055.10.
BTC_CLOSES = [
    81074.19, 80790.84, 80024.01, 80006.66, 80805.08, 81267.59, 77775.11, 77198.21,
    77434.75, 78248.45, 76677.30, 77345.05, 80405.29, 75942.13, 75746.93, 75747.58,
    74584.65, 75223.15, 73720.12, 73964.47, 71342.97, 68249.27, 64128.05, 65305.06,
    64855.84, 64893.95, 62987.27, 61901.82, 58798.98, 60094.09, 64000.00, 61605.06,
]
#: Highs sit just above their closes, with one deliberate spike inside the last
#: completed week (index 27 of 32, where 31 is today) so the expected answer is
#: unambiguous: 66055.10 - and demonstrably not today's 61674.92.
BTC_HIGHS = [c + 100 for c in BTC_CLOSES[:-1]] + [61674.92]
BTC_HIGHS[27] = 66055.10


def btc_payload(*, days_back: int = 0) -> list[dict]:
    now = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    base = now - timedelta(days=days_back)
    total = len(BTC_CLOSES)
    bars = []
    for idx, close in enumerate(BTC_CLOSES):
        day = base - timedelta(days=total - 1 - idx)
        bars.append(
            {
                "time": day.isoformat(),
                "open": BTC_CLOSES[idx - 1] if idx else close,
                "high": BTC_HIGHS[idx],
                "low": close - 800,
                "close": close,
                "volume": 100.0,
            }
        )
    return bars


# --- instrument extraction -------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("EURUSD?", ["EURUSD"]),
        ("eur/usd now", ["EURUSD"]),
        ("GU bias", ["GBPUSD"]),
        ("cable direction", ["GBPUSD"]),
        ("XAU support", ["XAUUSD"]),
        ("gold price", ["XAUUSD"]),
        ("silver?", ["XAGUSD"]),
        ("DXY correlation", ["DXY"]),
        ("US10Y yield", ["US10Y"]),
        ("EURUSD vs DXY", ["EURUSD", "DXY"]),
        ("what is a pip", []),
        ("capital of Kenya?", []),
        ("", []),
    ],
)
def test_instruments_in(text: str, expected: list[str]) -> None:
    assert instruments_in(text) == expected


def test_instruments_are_capped() -> None:
    # A paragraph must not trigger ten HTTP calls before the answer.
    text = "EURUSD GBPUSD USDJPY AUDUSD USDCAD DXY XAUUSD"
    assert len(instruments_in(text)) <= 4


def test_normalise_handles_shorthand_and_pairs() -> None:
    assert normalise_instrument("EU") == "EURUSD"
    assert normalise_instrument("GU") == "GBPUSD"
    assert normalise_instrument("eurusd") == "EURUSD"
    assert normalise_instrument("XAU") == "XAUUSD"
    assert normalise_instrument("nonsense") is None


# --- freshness labelling ---------------------------------------------------
def _quote(**kwargs) -> Quote:
    defaults = {
        "instrument": "EURUSD",
        "price": 1.0984,
        "kind": "live",
        "source": "yahoo",
        "as_of": datetime.now(UTC),
        "decimals": 5,
    }
    return Quote(**{**defaults, **kwargs})


def test_live_quote_label_has_no_timestamp() -> None:
    """A group reply must not reveal that a feed was consulted, or when.

    "EURUSD 1.09840 (2026-11-23 05:01 UTC)" tells the whole group that a bot
    fetched a price. The bare figure is what a person would type.
    """
    label = _quote().label()
    assert label == "EURUSD 1.09840"
    assert "UTC" not in label
    assert "2026" not in label


def test_closed_market_quote_says_market_closed_without_a_clock() -> None:
    closed = _quote(
        kind="market_closed",
        as_of=datetime.now(UTC) - timedelta(minutes=STALE_AFTER_MINUTES + 5),
    )
    label = closed.label()
    # The state still has to be honest - Friday's close is not the live price -
    # but the timestamp itself is not shown.
    assert "market closed" in label
    assert "UTC" not in label
    assert "2026" not in label


def test_detailed_label_names_exness_and_the_symbol() -> None:
    """The owner's diagnostics get the venue and the exact account symbol."""
    quote = _quote(instrument="EURUSD", symbol="EURUSDm")
    detailed = quote.label(detailed=True)
    assert "Exness" in detailed
    assert "EURUSDm" in detailed
    # A group reply never mentions the venue.
    assert "Exness" not in quote.label()


def test_detailed_label_keeps_the_timestamp() -> None:
    # The owner's own diagnostics still need the clock; group replies do not.
    label = _quote().label(detailed=True)
    assert "UTC" in label
    fix = _quote(kind="daily_fix", as_of=datetime(2026, 11, 20, tzinfo=UTC))
    assert "2026-11-20" in fix.label(detailed=True)


def test_jpy_pairs_use_three_decimals() -> None:
    assert _quote(instrument="USDJPY", price=158.6412, decimals=3).formatted() == "158.641"


# --- prompt rendering ------------------------------------------------------
def test_empty_snapshot_renders_nothing() -> None:
    assert MarketSnapshot().as_prompt_block() == ""
    assert MarketSnapshot().is_empty()


def test_prompt_block_has_no_daily_fix_caveat_to_offer() -> None:
    """There is no once-a-day reference source any more, so no caveat for one."""
    snapshot = MarketSnapshot(quotes=[_quote()])
    block = snapshot.as_prompt_block()
    assert "daily fix" not in block
    assert "ECB" not in block


def test_prompt_block_forbids_dates_and_provenance() -> None:
    block = MarketSnapshot(quotes=[_quote()]).as_prompt_block()
    # The model is told not to decorate the number, whatever it can see.
    assert "Never add a date, a time" in block
    assert "never mention data feeds" in block
    assert "UTC" not in block


def test_prompt_block_warns_about_closed_market() -> None:
    stale = _quote(as_of=datetime.now(UTC) - timedelta(minutes=STALE_AFTER_MINUTES + 5))
    block = MarketSnapshot(quotes=[stale]).as_prompt_block()
    assert "market is closed" in block


def test_prompt_block_no_longer_calls_gold_a_future() -> None:
    """Gold is now the account's own spot CFD, not a COMEX front month."""
    snapshot = MarketSnapshot(
        quotes=[_quote(instrument="XAUUSD", price=3611.4, kind="live", decimals=2)]
    )
    block = snapshot.as_prompt_block()
    assert "not spot" not in block
    assert "3611.40" in block


# --- guidance swap ---------------------------------------------------------
def test_guidance_forbids_refusal_when_data_exists() -> None:
    with_data = forex.group_guidance(has_live_data=True)
    without = forex.group_guidance(has_live_data=False)
    # The refusal rule must be replaced, not joined by a contradictory one.
    assert forex.NO_LIVE_DATA_GUIDANCE not in with_data
    assert forex.LIVE_DATA_PRESENT_GUIDANCE in with_data
    assert forex.NO_LIVE_DATA_GUIDANCE in without
    assert "the ONLY prices you may state" in with_data


def test_prompt_with_data_contains_numbers_and_does_not_contradict() -> None:
    snapshot = MarketSnapshot(quotes=[_quote(price=1.0984)])
    prompt = AIEngine._build_system_prompt(
        "giveaway", "EURUSD now?", forex_group=True, market=snapshot
    )
    assert "1.09840" in prompt
    assert "LIVE MARKET DATA" in prompt
    # The "you have no feed, reply No live data" rule must be gone.
    assert forex.NO_LIVE_DATA_GUIDANCE not in prompt


def test_prompt_without_data_keeps_the_refusal_rule() -> None:
    prompt = AIEngine._build_system_prompt(
        "giveaway", "EURUSD now?", forex_group=True, market=MarketSnapshot()
    )
    assert forex.NO_LIVE_DATA_GUIDANCE in prompt
    assert "LIVE MARKET DATA" not in prompt


# --- fetching --------------------------------------------------------------
@pytest.mark.asyncio
async def test_fx_quote_comes_from_exness() -> None:
    fake = FakeExness(candles={"EURUSD": candle_payload(price=1.0984)})
    feed = _exness_feed(fake)
    try:
        snapshot = await feed.snapshot("EURUSD?")
    finally:
        await feed.close()

    assert len(snapshot.quotes) == 1
    quote = snapshot.quotes[0]
    assert quote.price == pytest.approx(1.0984)
    assert quote.kind == "live"
    # The single source. Anything else here would be the bug this replaced.
    assert quote.source == "exness"


@pytest.mark.asyncio
async def test_failed_fetch_does_not_borrow_another_venue() -> None:
    """The old chain fell through to an ECB daily fix. That is now forbidden."""
    fake = FakeExness(candles_status=400, error_code=4000)
    feed = _exness_feed(fake)
    try:
        snapshot = await feed.snapshot("EURUSD?")
    finally:
        await feed.close()

    assert snapshot.is_empty()
    assert snapshot.failed == ["EURUSD"]
    # No daily_fix, no futures, no second host - just an honest gap.
    assert not any(q.kind in ("daily_fix", "futures") for q in snapshot.quotes)


@pytest.mark.asyncio
async def test_gold_is_quoted_from_exness_not_a_spot_api() -> None:
    fake = FakeExness(
        instruments=["EURUSD", "XAUUSD"],
        candles={"XAUUSD": candle_payload(price=3607.80)},
    )
    feed = _exness_feed(fake)
    try:
        snapshot = await feed.snapshot("gold price")
    finally:
        await feed.close()

    assert snapshot.quotes[0].source == "exness"
    assert snapshot.quotes[0].price == pytest.approx(3607.80, abs=0.01)


@pytest.mark.asyncio
async def test_total_failure_yields_empty_snapshot_not_exception() -> None:
    fake = FakeExness(discovery_status=503, error_code=2)
    feed = _exness_feed(fake)
    try:
        snapshot = await feed.snapshot("EURUSD?")
    finally:
        await feed.close()

    assert snapshot.is_empty()
    assert snapshot.failed == ["EURUSD"]


@pytest.mark.asyncio
async def test_unconfigured_feed_makes_no_requests_at_all() -> None:
    fake = FakeExness()
    feed = MarketFeed(fake.client(), settings_loader=None)
    try:
        snapshot = await feed.snapshot("EURUSD?")
    finally:
        await feed.close()

    assert snapshot.is_empty()
    assert fake.calls == []


@pytest.mark.asyncio
async def test_non_forex_question_makes_no_requests() -> None:
    fake = FakeExness()
    feed = _exness_feed(fake)
    try:
        snapshot = await feed.snapshot("capital of Kenya?")
    finally:
        await feed.close()

    assert snapshot.is_empty()
    assert fake.calls == []


@pytest.mark.asyncio
async def test_cache_is_per_instrument_set() -> None:
    fake = FakeExness(
        instruments=["EURUSD", "DXY"],
        candles={
            "EURUSD": candle_payload(price=1.0984),
            "DXY": candle_payload(price=103.773),
        },
    )
    feed = _exness_feed(fake)
    try:
        first = await feed.snapshot("EURUSD?")
        assert [q.instrument for q in first.quotes] == ["EURUSD"]

        # A different instrument must not be served the cached EUR digest.
        second = await feed.snapshot("DXY?")
        assert [q.instrument for q in second.quotes] == ["DXY"]

        # A repeat of the same set is served from cache.
        calls_before = len(fake.calls)
        third = await feed.snapshot("EURUSD?")
        assert [q.instrument for q in third.quotes] == ["EURUSD"]
        assert len(fake.calls) == calls_before
    finally:
        await feed.close()


# --- period questions (crypto / history) -----------------------------------
# The series below is the real BTC-USD daily data captured from the live
# endpoint: 32 bars, a null weekend gap, and today's still-forming candle last.
_BTC_CLOSES = [
    81074.19, 80790.84, 80024.01, 80006.66, 80805.08, 81267.59, 77775.11, 77198.21,
    77434.75, 78248.45, 76677.30, 77345.05, 80405.29, 75942.13, 75746.93, 75747.58,
    74584.65, 75223.15, 73720.12, 73964.47, 71342.97, 68249.27, 64128.05, 65305.06,
    64855.84, 64893.95, 62987.27, 61901.82, 58798.98, 60094.09, None, 61605.06,
]
_BTC_HIGHS = [
    81956.34, 81098.24, 80928.27, 80709.98, 81180.92, 81703.20, 81889.95, 78647.39,
    77629.73, 78313.26, 78724.39, 77964.98, 81079.79, 80833.62, 76291.06, 76138.68,
    76096.69, 75505.34, 75320.91, 74703.45, 74115.22, 71428.53, 68377.54, 66055.10,
    65604.60, 65365.13, 64928.80, 64017.27, 62540.21, 60843.76, None, 61674.92,
]
_BTC_LOWS = [
    80421.74, 79145.54, 79766.83, 79278.71, 79380.36, 80300.30, 77737.19, 76189.88,
    77000.65, 77384.48, 76572.23, 76270.0, 75627.98, 75114.29, 74795.11, 75485.69,
    73691.77, 73664.54, 72689.38, 73141.53, 71084.05, 67653.07, 63329.43, 62017.88,
    63834.67, 63284.46, 62017.38, 61380.88, 57708.70, 55149.57, None, 60367.81,
]
_BTC_TIMESTAMPS = [
    1792713600, 1792800000, 1792886400, 1792972800, 1793059200, 1793145600, 1793232000,
    1793318400, 1793404800, 1793491200, 1793577600, 1793664000, 1793750400, 1793836800,
    1793923200, 1794009600, 1794096000, 1794182400, 1794268800, 1794355200, 1794441600,
    1794528000, 1794614400, 1794700800, 1794787200, 1794873600, 1794960000, 1795046400,
    1795132800, 1795219200, 1795305600, 1795392000,
]
BTC_PAYLOAD = {
    "chart": {
        "result": [
            {
                "meta": {"symbol": "BTC-USD", "regularMarketPrice": 61605.06},
                "timestamp": _BTC_TIMESTAMPS,
                "indicators": {
                    "quote": [
                        {
                            "high": _BTC_HIGHS,
                            "low": _BTC_LOWS,
                            # 32 entries, aligned to the timestamps; the forming
                            # candle's open is present (Yahoo sometimes omits it).
                            "open": [
                                81311.89, *[c for c in _BTC_CLOSES if c][:30], 61162.20
                            ],
                            "close": _BTC_CLOSES,
                        }
                    ]
                },
            }
        ]
    }
}


@pytest.mark.parametrize(
    ("text", "period", "aggregation", "instrument"),
    [
        ("On the exness BTC chart, what was the highest price recorded last week?",
         "last_week", "high", "BTCUSD"),
        ("BTC lowest last week?", "last_week", "low", "BTCUSD"),
        ("btc high this week", "this_week", "high", "BTCUSD"),
        ("bitcoin lowest last month", "last_month", "low", "BTCUSD"),
        ("what was btc highest yesterday", "yesterday", "high", "BTCUSD"),
        ("BTC change last week", "last_week", "change", "BTCUSD"),
        ("eth high last week", "last_week", "high", "ETHUSD"),
    ],
)
def test_parse_period_query(text, period, aggregation, instrument) -> None:
    from qna_bot.ai.market import parse_period_query

    pq = parse_period_query(text)
    assert pq is not None
    assert (pq.period, pq.aggregation, pq.instrument) == (period, aggregation, instrument)


@pytest.mark.parametrize(
    "text",
    [
        "what is a pip",
        "EURUSD price now?",
        "capital of Kenya?",
        "first comment",
        "BTC is bullish",
        "",
    ],
)
def test_parse_period_query_ignores_non_period_questions(text: str) -> None:
    from qna_bot.ai.market import parse_period_query

    assert parse_period_query(text) is None


@pytest.mark.asyncio
async def test_btc_last_week_high_is_computed_exactly() -> None:
    """The user's actual question: "highest BTC price last week"."""
    from qna_bot.ai.market import parse_period_query

    fake = FakeExness(instruments=["BTCUSD"], candles={"D1:BTCUSD": btc_payload()})
    feed = _exness_feed(fake)
    try:
        answer = await feed.period_answer(parse_period_query(
            "On the exness BTC chart, what was the highest price recorded last week?"
        ))
    finally:
        await feed.close()
    assert answer is not None
    assert answer.formatted() == "66055.10"


@pytest.mark.asyncio
async def test_period_answers_use_price_precision_not_fx_precision() -> None:
    """66055.10000 is a tell; crypto quotes two decimals."""
    from qna_bot.ai.market import parse_period_query

    fake = FakeExness(instruments=["BTCUSD"], candles={"D1:BTCUSD": btc_payload()})
    feed = _exness_feed(fake)
    try:
        answer = await feed.period_answer(parse_period_query("BTC high last week"))
    finally:
        await feed.close()
    assert answer is not None
    assert not answer.formatted().endswith("000")


@pytest.mark.asyncio
async def test_last_week_excludes_todays_forming_bar() -> None:
    """An incomplete candle must not become "the high last week"."""
    from qna_bot.ai.market import parse_period_query

    fake = FakeExness(instruments=["BTCUSD"], candles={"D1:BTCUSD": btc_payload()})
    feed = _exness_feed(fake)
    try:
        answer = await feed.period_answer(parse_period_query("BTC high last week"))
    finally:
        await feed.close()
    assert answer is not None
    # Today's bar high is 61674.92 and is NOT the weekly high.
    assert answer.high == pytest.approx(66055.10)


@pytest.mark.asyncio
async def test_period_answer_is_computed_without_ai() -> None:
    """The model must never be asked to eyeball a series for a max/min."""

    fake = FakeExness(instruments=["BTCUSD"], candles={"D1:BTCUSD": btc_payload()})
    feed = _exness_feed(fake)
    engine = _CapturingEngine(feed)

    result = await engine.answer_question(
        text="On the exness BTC chart, what was the highest price last week?",
        forex_group=True,
        answer_style="giveaway",
        primary_provider="groq",
        primary_model="openai/gpt-oss-120b",
        provider_keys={"groq": "gsk_test"},
        local_math_enabled=False,
    )
    assert result.answer == "66055.10"
    assert result.provider == "local_market"
    assert engine.prompts == [], "a period question must not reach the AI"


# --- engine integration ----------------------------------------------------
class _CapturingEngine(AIEngine):
    """Records the system prompt without calling any AI provider."""

    def __init__(self, feed: MarketFeed) -> None:
        super().__init__(market_feed=feed)
        self.prompts: list[str] = []

    async def _call_provider(self, **kwargs):  # type: ignore[override]
        self.prompts.append(kwargs["system_prompt"])
        return "1.0984"


async def _ask(engine: _CapturingEngine, *, forex_group: bool) -> str:
    await engine.answer_question(
        text="EURUSD?",
        forex_group=forex_group,
        answer_style="giveaway",
        primary_provider="groq",
        primary_model="openai/gpt-oss-120b",
        provider_keys={"groq": "gsk_test"},
        local_math_enabled=False,
    )
    return engine.prompts[-1]


@pytest.mark.asyncio
async def test_engine_fetches_for_forex_groups_only() -> None:
    fake = FakeExness(candles={"EURUSD": candle_payload(price=1.0984)})
    feed = _exness_feed(fake)
    engine = _CapturingEngine(feed)

    # A normal group must never pay for a market call.
    prompt = await _ask(engine, forex_group=False)
    assert fake.calls == []
    assert "LIVE MARKET DATA" not in prompt

    prompt = await _ask(engine, forex_group=True)
    assert fake.calls != []
    assert "LIVE MARKET DATA" in prompt
    await feed.close()


@pytest.mark.asyncio
async def test_live_data_toggle_stops_fetching() -> None:
    fake = FakeExness(candles={"EURUSD": candle_payload(price=1.0984)})
    feed = _exness_feed(fake)
    engine = _CapturingEngine(feed)

    await engine.answer_question(
        text="EURUSD?",
        forex_group=True,
        live_data=False,
        answer_style="giveaway",
        primary_provider="groq",
        primary_model="openai/gpt-oss-120b",
        provider_keys={"groq": "gsk_test"},
        local_math_enabled=False,
    )
    assert fake.calls == []
    assert "LIVE MARKET DATA" not in engine.prompts[-1]
    assert forex.NO_LIVE_DATA_GUIDANCE in engine.prompts[-1]
    await feed.close()


def test_live_data_defaults_on() -> None:
    import inspect

    sig = inspect.signature(AIEngine.answer_question)
    assert sig.parameters["live_data"].default is True
