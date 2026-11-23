"""Live market feed: extraction, freshness labelling, prompting, fallbacks.

The payloads below are the real shapes returned by each source when this was
written (Yahoo chart API, gold-api.com, Frankfurter/ECB). They are frozen here
so the parsing contract is tested without touching the network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from qna_bot.ai import forex
from qna_bot.ai.engine import AIEngine
from qna_bot.ai.market import (
    STALE_AFTER_MINUTES,
    MarketFeed,
    MarketSnapshot,
    Quote,
    instruments_in,
    normalise_instrument,
)

# --- recorded live payloads ------------------------------------------------
YAHOO_EURUSD = {
    "chart": {
        "result": [
            {
                "meta": {
                    "currency": "USD",
                    "symbol": "EURUSD=X",
                    "regularMarketPrice": 1.0984,
                    "regularMarketChangePercent": 0.0879,
                    "regularMarketTime": 1795410105,
                },
                "timestamp": [1792710000],
                "indicators": {"quote": [{"close": [1.0984]}]},
            }
        ],
        "error": None,
    }
}

GOLD_API = {
    "currency": "USD",
    "exchangeRate": 1.0,
    "name": "Gold",
    "price": 3607.800049,
    "symbol": "XAU",
    "updatedAt": "2026-11-23T05:00:11Z",
}

FRANKFURTER = {"amount": 1.0, "base": "EUR", "date": "2026-11-20", "rates": {"USD": 1.099}}


class _Resp:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """Routes a URL to a recorded payload, and records what was asked for."""

    def __init__(self, routes: dict[str, _Resp | Exception]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    async def get(self, url: str, params: dict | None = None, **_: object) -> _Resp:
        self.calls.append(url)
        for fragment, response in self.routes.items():
            if fragment in url:
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"unexpected URL {url}")

    async def aclose(self) -> None:
        return None


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


def test_stale_quote_says_market_closed_without_a_clock() -> None:
    old = _quote(as_of=datetime.now(UTC) - timedelta(minutes=STALE_AFTER_MINUTES + 5))
    label = old.label()
    # The state still has to be honest - Friday's close is not the live price -
    # but the timestamp itself is not shown.
    assert "market closed" in label
    assert "UTC" not in label
    assert "2026" not in label


def test_ecb_fix_is_never_called_current() -> None:
    fix = _quote(kind="daily_fix", source="frankfurter/ecb", as_of=datetime(2026, 11, 20, tzinfo=UTC))
    label = fix.label()
    assert "daily fix" in label
    assert "official" not in label
    assert "2026-11-20" not in label


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


def test_prompt_block_warns_about_daily_fixes() -> None:
    snapshot = MarketSnapshot(
        quotes=[_quote(kind="daily_fix", as_of=datetime(2026, 11, 20, tzinfo=UTC))]
    )
    block = snapshot.as_prompt_block()
    assert "daily fix" in block
    assert "never call them the current price" in block
    assert "2026-11-20" not in block


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


def test_prompt_block_labels_gold_as_futures() -> None:
    snapshot = MarketSnapshot(
        quotes=[_quote(instrument="XAUUSD", price=3611.4, kind="futures", decimals=2)]
    )
    block = snapshot.as_prompt_block()
    assert "not spot" in block


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
async def test_fx_uses_live_yahoo_quote() -> None:
    client = _FakeClient({"query1.finance.yahoo.com": _Resp(YAHOO_EURUSD)})
    feed = MarketFeed(client=client)  # type: ignore[arg-type]
    snapshot = await feed.snapshot("EURUSD?")
    assert len(snapshot.quotes) == 1
    quote = snapshot.quotes[0]
    assert quote.price == pytest.approx(1.0984)
    assert quote.kind == "live"
    assert quote.source == "yahoo"


@pytest.mark.asyncio
async def test_falls_back_to_ecb_fix_when_live_source_fails() -> None:
    client = _FakeClient(
        {
            "query1.finance.yahoo.com": RuntimeError("boom"),
            "api.frankfurter.dev": _Resp(FRANKFURTER),
        }
    )
    feed = MarketFeed(client=client)  # type: ignore[arg-type]
    snapshot = await feed.snapshot("EURUSD?")
    assert len(snapshot.quotes) == 1
    assert snapshot.quotes[0].kind == "daily_fix"
    assert snapshot.quotes[0].price == pytest.approx(1.099)


@pytest.mark.asyncio
async def test_gold_falls_back_to_gold_api() -> None:
    client = _FakeClient(
        {"query1.finance.yahoo.com": RuntimeError("boom"), "api.gold-api.com": _Resp(GOLD_API)}
    )
    feed = MarketFeed(client=client)  # type: ignore[arg-type]
    snapshot = await feed.snapshot("gold price")
    assert len(snapshot.quotes) == 1
    assert snapshot.quotes[0].source == "gold-api"
    assert snapshot.quotes[0].price == pytest.approx(3607.80, abs=0.01)


@pytest.mark.asyncio
async def test_total_failure_yields_empty_snapshot_not_exception() -> None:
    client = _FakeClient({"": RuntimeError("network down")})
    feed = MarketFeed(client=client)  # type: ignore[arg-type]
    snapshot = await feed.snapshot("EURUSD?")
    assert snapshot.is_empty()
    assert snapshot.failed == ["EURUSD"]


@pytest.mark.asyncio
async def test_non_forex_question_makes_no_requests() -> None:
    client = _FakeClient({})
    feed = MarketFeed(client=client)  # type: ignore[arg-type]
    snapshot = await feed.snapshot("capital of Kenya?")
    assert snapshot.is_empty()
    assert client.calls == []


@pytest.mark.asyncio
async def test_cache_is_per_instrument_set() -> None:
    client = _FakeClient(
        {
            "query1.finance.yahoo.com/v8/finance/chart/EURUSD=X": _Resp(YAHOO_EURUSD),
            "query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB": _Resp(
                {
                    "chart": {
                        "result": [
                            {
                                "meta": {
                                    "regularMarketPrice": 103.773,
                                    "regularMarketTime": 1795410105,
                                }
                            }
                        ]
                    }
                }
            ),
        }
    )
    feed = MarketFeed(client=client)  # type: ignore[arg-type]

    first = await feed.snapshot("EURUSD?")
    assert [q.instrument for q in first.quotes] == ["EURUSD"]

    # A different instrument must not be served the cached EUR digest.
    second = await feed.snapshot("DXY?")
    assert [q.instrument for q in second.quotes] == ["DXY"]

    # A repeat of the same set is served from cache.
    calls_before = len(client.calls)
    third = await feed.snapshot("EURUSD?")
    assert [q.instrument for q in third.quotes] == ["EURUSD"]
    assert len(client.calls) == calls_before


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
    from qna_bot.ai.market import MarketFeed, parse_period_query

    client = _FakeClient({"BTC-USD": _Resp(BTC_PAYLOAD)})
    feed = MarketFeed(client=client)  # type: ignore[arg-type]
    answer = await feed.period_answer(parse_period_query(
        "On the exness BTC chart, what was the highest price recorded last week?"
    ))
    assert answer is not None
    assert answer.formatted() == "66055.10"


@pytest.mark.asyncio
async def test_period_answers_use_price_precision_not_fx_precision() -> None:
    """66055.10000 is a tell; crypto quotes two decimals."""
    from qna_bot.ai.market import MarketFeed, parse_period_query

    client = _FakeClient({"BTC-USD": _Resp(BTC_PAYLOAD)})
    feed = MarketFeed(client=client)  # type: ignore[arg-type]
    answer = await feed.period_answer(parse_period_query("BTC high last week"))
    assert answer is not None
    assert not answer.formatted().endswith("000")


@pytest.mark.asyncio
async def test_last_week_excludes_todays_forming_bar() -> None:
    """An incomplete candle must not become "the high last week"."""
    from qna_bot.ai.market import MarketFeed, parse_period_query

    client = _FakeClient({"BTC-USD": _Resp(BTC_PAYLOAD)})
    feed = MarketFeed(client=client)  # type: ignore[arg-type]
    answer = await feed.period_answer(parse_period_query("BTC high last week"))
    assert answer is not None
    # Today's bar high is 61674.92 and is NOT the weekly high.
    assert answer.high == pytest.approx(66055.10)


@pytest.mark.asyncio
async def test_period_answer_is_computed_without_ai() -> None:
    """The model must never be asked to eyeball a series for a max/min."""
    from qna_bot.ai.market import MarketFeed

    client = _FakeClient({"BTC-USD": _Resp(BTC_PAYLOAD)})
    feed = MarketFeed(client=client)  # type: ignore[arg-type]
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
    client = _FakeClient({"query1.finance.yahoo.com": _Resp(YAHOO_EURUSD)})
    feed = MarketFeed(client=client)  # type: ignore[arg-type]
    engine = _CapturingEngine(feed)

    # A normal group must never pay for a market call.
    prompt = await _ask(engine, forex_group=False)
    assert client.calls == []
    assert "LIVE MARKET DATA" not in prompt

    prompt = await _ask(engine, forex_group=True)
    assert client.calls != []
    assert "LIVE MARKET DATA" in prompt


@pytest.mark.asyncio
async def test_live_data_toggle_stops_fetching() -> None:
    client = _FakeClient({"query1.finance.yahoo.com": _Resp(YAHOO_EURUSD)})
    feed = MarketFeed(client=client)  # type: ignore[arg-type]
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
    assert client.calls == []
    assert "LIVE MARKET DATA" not in engine.prompts[-1]
    assert forex.NO_LIVE_DATA_GUIDANCE in engine.prompts[-1]


def test_live_data_defaults_on() -> None:
    import inspect

    sig = inspect.signature(AIEngine.answer_question)
    assert sig.parameters["live_data"].default is True
