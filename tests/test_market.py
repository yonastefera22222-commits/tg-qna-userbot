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
