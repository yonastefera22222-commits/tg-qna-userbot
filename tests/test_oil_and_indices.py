"""USOIL and the US Tech 100 must return a live price, not "No live data".

The reported failure was exact: asking for oil or the Nasdaq 100 in a Forex
group produced "No live data". The feed was working - the *recognition* layer
was not. Neither name existed in :data:`FEEDABLE_INSTRUMENTS`, so
``instruments_in()`` returned nothing, the snapshot was empty, the prompt got no
LIVE MARKET DATA block, and the model correctly refused to invent a price.

So these tests drive the whole path with a stubbed Exness account that quotes
only *suffixed* symbols (``USOILm``, ``USTECm``), which is what a real mini
account does, and assert the answer is a number.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from exness_fixtures import ACCOUNT_ID, API_KEY, GATEWAY, TEST_PRIVATE_KEY, FakeExness

from qna_bot.ai.engine import AIEngine
from qna_bot.ai.exness import ExnessClient, ExnessCredentials, ExnessError
from qna_bot.ai.market import (
    FEEDABLE_INSTRUMENTS,
    MarketFeed,
    _decimals_for,
    asks_for_mechanics,
    asks_for_quote,
    format_quote_answer,
    instruments_in,
    normalise_instrument,
)
from qna_bot.ai.market_calendar import market_class_for

CREDS = ExnessCredentials(
    api_key=API_KEY,
    private_key=TEST_PRIVATE_KEY,
    account_id=ACCOUNT_ID,
    gateway_url=GATEWAY,
)

#: What a mini account actually offers: every symbol carries the ``m`` suffix,
#: and the index is called USTEC rather than "US Tech 100".
MINI_ACCOUNT = ["EURUSDm", "XAUUSDm", "USOILm", "UKOILm", "USTECm", "US500m", "GER40m"]


def _settings() -> dict:
    return {
        "exness_api_key": CREDS.api_key,
        "exness_private_key": CREDS.private_key,
        "exness_account_id": CREDS.account_id,
        "exness_gateway_url": CREDS.gateway_url,
    }


def _feed(fake: FakeExness) -> MarketFeed:
    async def load() -> dict:
        return _settings()

    return MarketFeed(fake.client(), settings_loader=load)


@pytest.fixture(autouse=True)
def _an_open_market(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin "now" to a weekday session.

    The weekend rule is a real behaviour with its own tests; these tests are
    about *recognition*, and they must not start failing on a Saturday because
    the feed correctly reported a shut market. So the clock the feed reads is
    frozen to a Wednesday 12:00 UTC.
    """
    import qna_bot.ai.market as market_mod
    import qna_bot.ai.sessions as sessions_mod

    # A *datetime subclass*, not a stub: the modules under test also use
    # ``datetime.combine`` and friends, which a bare fake would not provide.
    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001 - matches datetime.now
            frozen = cls(2026, 11, 25, 12, 0, tzinfo=UTC)  # a Wednesday
            return frozen if tz is not None else frozen.replace(tzinfo=None)

    monkeypatch.setattr(market_mod, "datetime", _Clock)
    monkeypatch.setattr(sessions_mod, "datetime", _Clock)


def _quote_payload(price: float, *, minutes_ago: float = 1.0) -> list[dict]:
    """Three M1 candles, the newest closing at ``price``."""
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    return [
        {
            "time": (now - timedelta(minutes=minutes_ago + (2 - idx))).isoformat(),
            "open": price,
            "high": price + 0.05,
            "low": price - 0.05,
            "close": price,
            "volume": 10.0,
        }
        for idx in range(3)
    ]


def _mini_account(**candles: list[dict]) -> FakeExness:
    return FakeExness(instruments=list(MINI_ACCOUNT), candles=dict(candles))


# ---------------------------------------------------------------------------
# Recognition: the layer that was actually missing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("USOIL price?", "USOIL"),
        ("usoil", "USOIL"),
        ("US Oil price?", "USOIL"),
        ("USOILm", "USOIL"),
        ("XTIUSD", "USOIL"),
        ("WTI crude", "USOIL"),
        ("crude oil", "USOIL"),
        ("brent", "UKOIL"),
        ("UKOIL", "UKOIL"),
        ("natural gas", "NATGAS"),
        ("US Tech 100", "USTEC"),
        ("us tech 100 now?", "USTEC"),
        ("US Tech 100 index", "USTEC"),
        ("USTEC", "USTEC"),
        ("NAS100", "USTEC"),
        ("US100", "USTEC"),
        ("Nasdaq 100", "USTEC"),
        ("S&P 500", "US500"),
        ("SPX500", "US500"),
        ("Dow Jones", "US30"),
        ("GER40", "GER40"),
        ("DAX", "GER40"),
        ("UK100", "UK100"),
        ("FTSE 100", "UK100"),
        ("JP225", "JP225"),
        ("Nikkei 225", "JP225"),
    ],
)
def test_names_a_trader_types_are_recognised(text: str, expected: str) -> None:
    assert expected in instruments_in(text)


@pytest.mark.parametrize(
    "name",
    ["USOIL", "UKOIL", "NATGAS", "USTEC", "US500", "US30", "GER40", "UK100", "JP225"],
)
def test_new_instruments_are_feedable(name: str) -> None:
    # The bug in one line: this used to be False, so the feed answered nothing.
    assert name in FEEDABLE_INSTRUMENTS


def test_normalise_folds_broker_synonyms_onto_one_instrument() -> None:
    assert normalise_instrument("NAS100") == "USTEC"
    assert normalise_instrument("US100") == "USTEC"
    assert normalise_instrument("WTI") == "USOIL"
    assert normalise_instrument("SPX500") == "US500"


def test_equity_talk_still_reaches_the_model() -> None:
    # Recognition must not swallow analysis: "support", "prediction" and
    # "why did it fall" are not prices and have no feed answer.
    assert asks_for_quote("USOIL price?") is True
    assert asks_for_quote("US Tech 100 now?") is True
    assert asks_for_quote("what is the price of USOIL") is True
    assert asks_for_quote("USOIL price prediction") is False
    assert asks_for_quote("USOIL price action analysis") is False
    assert asks_for_quote("why did USOIL fall") is False
    assert asks_for_quote("USOIL buy or sell") is False


# ---------------------------------------------------------------------------
# Symbol resolution against the account's own instrument list
# ---------------------------------------------------------------------------


def test_alias_resolution_finds_the_suffixed_symbol() -> None:
    available = set(MINI_ACCOUNT)
    client = ExnessClient(credentials=CREDS, client=None)
    assert client._resolve_symbol("USOIL", available) == "USOILm"
    assert client._resolve_symbol("USTEC", available) == "USTECm"
    # A broker synonym normalised before the resolver ever sees it.
    assert client._resolve_symbol(normalise_instrument("NAS100"), available) == "USTECm"


def test_alias_resolution_prefers_an_offered_synonym() -> None:
    # An account that quotes the index as NAS100 rather than USTEC.
    available = {"NAS100m", "XTIUSDm"}
    client = ExnessClient(credentials=CREDS, client=None)
    assert client._resolve_symbol("USTEC", available) == "NAS100m"
    assert client._resolve_symbol("USOIL", available) == "XTIUSDm"


def test_unoffered_instrument_raises_rather_than_guessing() -> None:
    client = ExnessClient(credentials=CREDS, client=None)
    with pytest.raises(ExnessError):
        client._resolve_symbol("USOIL", {"EURUSDm"})


# ---------------------------------------------------------------------------
# Decoration: a price a trader would type
# ---------------------------------------------------------------------------


def test_decimals_are_a_price_not_a_tick_chart() -> None:
    assert _decimals_for("USOIL") == 2
    assert _decimals_for("USTEC") == 2
    assert _decimals_for("US500") == 2
    # Nymex quotes gas to 3 dp.
    assert _decimals_for("NATGAS") == 3
    # Existing instruments keep their conventions.
    assert _decimals_for("EURUSD") == 5
    assert _decimals_for("USDJPY") == 3
    assert _decimals_for("XAUUSD") == 2


def test_oil_is_a_commodity_not_a_currency_pair() -> None:
    # Classified as FOREX, a Sunday-evening oil quote could claim to be live
    # while the oil market had not reopened.
    assert market_class_for("USOIL").key == "commodities"
    assert market_class_for("USTEC").key == "indices"
    assert market_class_for("EURUSD").key == "forex"


# ---------------------------------------------------------------------------
# The reported symptom, end to end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usoil_returns_a_live_price() -> None:
    fake = _mini_account()
    fake.candles["M1:USOILm"] = _quote_payload(71.42)
    feed = _feed(fake)

    snapshot = await feed.snapshot("USOIL price?")

    assert not snapshot.is_empty(), "USOIL produced no quote - the original bug"
    quote = snapshot.quotes[0]
    assert quote.instrument == "USOIL"
    assert quote.formatted() == "71.42"
    assert quote.symbol == "USOILm"


@pytest.mark.asyncio
async def test_us_tech_100_returns_a_live_price() -> None:
    fake = _mini_account()
    fake.candles["M1:USTECm"] = _quote_payload(24817.35)
    feed = _feed(fake)

    snapshot = await feed.snapshot("what is the US Tech 100 now?")

    assert not snapshot.is_empty(), "US Tech 100 produced no quote - the original bug"
    quote = snapshot.quotes[0]
    assert quote.instrument == "USTEC"
    assert quote.formatted() == "24817.35"
    assert quote.symbol == "USTECm"


@pytest.mark.asyncio
async def test_the_prompt_block_carries_the_price() -> None:
    # An empty prompt block is what made the model answer "No live data", so
    # this asserts on the exact string the model is handed.
    fake = _mini_account()
    fake.candles["M1:USTECm"] = _quote_payload(24817.35)
    feed = _feed(fake)

    block = (await feed.snapshot("US Tech 100 price?")).as_prompt_block()

    assert "USTEC 24817.35" in block
    assert "LIVE MARKET DATA" in block


@pytest.mark.asyncio
async def test_local_quote_answer_needs_no_model() -> None:
    fake = _mini_account()
    fake.candles["M1:USOILm"] = _quote_payload(71.42)
    feed = _feed(fake)

    answer = await feed.live_quote_answer("USOIL price?")

    assert answer == "USOIL 71.42"
    # One instrument, one candle read, nothing else.
    assert len(fake.candle_requests()) == 1


@pytest.mark.asyncio
async def test_local_quote_answer_declines_a_comparison() -> None:
    fake = _mini_account()
    feed = _feed(fake)

    # Two instruments is a sentence ("EURUSD vs USOIL"), not a number.
    assert await feed.live_quote_answer("USOIL vs EURUSD price?") is None


@pytest.mark.asyncio
async def test_local_quote_answer_declines_when_the_feed_fails() -> None:
    fake = _mini_account()
    fake.candles_status = 500
    feed = _feed(fake)

    # None, so the caller falls through to the normal path - never a guess.
    assert await feed.live_quote_answer("USOIL price?") is None


@pytest.mark.asyncio
async def test_an_unoffered_instrument_is_not_invented() -> None:
    fake = FakeExness(instruments=["EURUSDm"])  # account quotes no oil
    feed = _feed(fake)

    snapshot = await feed.snapshot("USOIL price?")

    assert snapshot.is_empty()
    assert snapshot.failed == ["USOIL"]


def test_closed_market_is_stated_not_hidden() -> None:
    quote_kwargs = dict(
        instrument="USOIL",
        price=71.42,
        source="exness",
        as_of=datetime.now(UTC),
        decimals=2,
    )
    from qna_bot.ai.market import Quote

    assert format_quote_answer("USOIL", Quote(kind="live", **quote_kwargs)) == "USOIL 71.42"
    assert (
        format_quote_answer("USOIL", Quote(kind="market_closed", **quote_kwargs))
        == "USOIL 71.42 (market closed)"
    )


# ---------------------------------------------------------------------------
# The engine path: answered locally, and fast
# ---------------------------------------------------------------------------


class _RecordingEngine(AIEngine):
    """An engine that records whether any model call was attempted."""

    def __init__(self, feed) -> None:  # noqa: ANN001
        super().__init__(market_feed=feed)
        self.model_calls: list[dict] = []

    async def _call_provider(self, **kwargs):  # type: ignore[override]
        self.model_calls.append(kwargs)
        return "no live data"


async def _ask(engine, text: str, *, live_data: bool = True):  # noqa: ANN001
    return await engine.answer_question(
        text=text,
        forex_group=True,
        live_data=live_data,
        answer_style="giveaway",
        primary_provider="groq",
        primary_model="openai/gpt-oss-120b",
        provider_keys={"groq": "gsk_test"},
        local_math_enabled=False,
    )


@pytest.mark.asyncio
async def test_engine_answers_a_price_question_without_a_model() -> None:
    fake = _mini_account()
    fake.candles["M1:USTECm"] = _quote_payload(24817.35)
    engine = _RecordingEngine(_feed(fake))

    result = await _ask(engine, "US Tech 100 now?")

    assert result.answered is True
    assert result.answer == "USTEC 24817.35"
    # The whole point of the request: no model round trip, so the answer is as
    # fast as the local arithmetic path.
    assert engine.model_calls == []
    assert result.provider == "local_market"
    assert result.model == "exness-quote"


@pytest.mark.asyncio
async def test_engine_still_uses_the_model_for_analysis() -> None:
    fake = _mini_account()
    fake.candles["M1:USOILm"] = _quote_payload(71.42)
    engine = _RecordingEngine(_feed(fake))

    result = await _ask(engine, "USOIL buy or sell?")

    # Analysis has no local answer, so it must reach the model with the price
    # in hand rather than being answered with a bare number.
    assert engine.model_calls != []
    assert result.provider != "local_market"
    assert result.answer == "no live data"  # the stub's reply, echoed back


@pytest.mark.asyncio
async def test_live_data_off_disables_the_local_quote_path() -> None:
    fake = _mini_account()
    fake.candles["M1:USOILm"] = _quote_payload(71.42)
    engine = _RecordingEngine(_feed(fake))

    await _ask(engine, "USOIL price?", live_data=False)

    # "Live data" switched off must mean no feed read at all, not a hidden one.
    assert fake.calls == []
    assert engine.model_calls != []


@pytest.mark.asyncio
async def test_an_image_question_does_not_take_the_bare_quote_path() -> None:
    # A chart screenshot can contradict a caption, so the model has to see it.
    engine = _RecordingEngine(_feed(_mini_account()))

    result = await engine.answer_question(
        text="USOIL price?",
        forex_group=True,
        answer_style="giveaway",
        primary_provider="groq",
        primary_model="openai/gpt-oss-120b",
        provider_keys={"groq": "gsk_test"},
        image=b"\xff\xd8\xff\xe0fake-jpeg",
        local_math_enabled=False,
    )

    assert result.provider != "local_market"


@pytest.mark.asyncio
async def test_oil_history_questions_are_computed_not_guessed() -> None:
    """"Highest USOIL last week" is arithmetic over real bars, as for FX."""
    import test_market as tm

    fake = _mini_account()
    # Same daily fixture the FX period tests use, so the expected high is the
    # one already proven there - the only difference is the instrument.
    fake.candles["D1:USOILm"] = tm.btc_payload()
    engine = _RecordingEngine(_feed(fake))

    result = await _ask(engine, "What was the highest USOIL price last week?")

    assert engine.model_calls == [], "a window high must never be eyeballed"
    assert result.provider == "local_market"
    assert result.model == "ohlc-history"
    # Two decimals, as oil is quoted - not the five of an FX pair.
    assert result.answer == "66055.10"



@pytest.mark.parametrize(
    ("text", "takes_fast_path"),
    [
        # A price, and nothing else: answered locally.
        ("USOIL price?", True),
        ("US Tech 100 now?", True),
        ("what is the price of oil today", True),
        ("what is the value of USOIL", True),
        # Mechanics: the words match a price question, the answer is an
        # explanation. These must keep reaching a model.
        ("what is the value of a pip in USOIL", False),
        ("how much is a pip in gold", False),
        ("what is a lot size for USOIL", False),
        ("USOIL price prediction", False),
        ("USOIL support and resistance", False),
    ],
)
def test_fast_path_is_taken_only_for_actual_prices(text: str, takes_fast_path: bool) -> None:
    takes = asks_for_quote(text) and not asks_for_mechanics(text) and len(instruments_in(text)) == 1
    assert takes is takes_fast_path


@pytest.mark.asyncio
async def test_a_mechanics_question_with_a_price_word_still_reaches_the_model() -> None:
    fake = _mini_account()
    fake.candles["M1:USOILm"] = _quote_payload(71.42)
    engine = _RecordingEngine(_feed(fake))

    result = await _ask(engine, "what is the value of a pip in USOIL?")

    assert engine.model_calls != [], "a pip-value question must not be answered 'USOIL 71.42'"
    assert result.provider != "local_market"
