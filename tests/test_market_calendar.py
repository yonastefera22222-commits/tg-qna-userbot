"""Which markets shut at the weekend, and the two bugs that came from not knowing.

1. The bot held one boolean, "is this crypto?". Every other family inherited
   whatever the crypto check said, which classified indices as weekend markets
   only by accident and could not tell gold from EURUSD.
2. A live quote was declared "market closed" purely on the age of its last tick.
   On Saturday at 01:00 Friday's 20:00 close is five hours old - inside the
   staleness window - so a market that had shut four hours earlier was quoted as
   the current price, on the day of the week most period questions are asked.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from qna_bot.ai import sessions as trading_sessions
from qna_bot.ai.market_calendar import (
    CRYPTO,
    FOREX,
    INDICES,
    METALS,
    describe,
    is_session,
    market_class_for,
)

# ---------------------------------------------------------------------------
# The classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("instrument", ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "DOGEUSD"])
def test_crypto_is_the_only_family_open_at_the_weekend(instrument: str) -> None:
    market = market_class_for(instrument)
    assert market.trades_weekends is True


@pytest.mark.parametrize(
    "instrument",
    [
        "EURUSD", "GBPJPY", "USDJPY", "USDSGD", "USDZAR",  # forex
        "XAUUSD", "XAGUSD",  # metals
        "DXY", "US10Y",  # indices and yields
    ],
)
def test_everything_else_shuts_at_the_weekend(instrument: str) -> None:
    market = market_class_for(instrument)
    assert market.trades_weekends is False


def test_metals_and_indices_are_not_crypto() -> None:
    """The regression that mattered: they used to inherit the crypto answer."""
    assert market_class_for("XAUUSD") is METALS
    assert market_class_for("DXY") is INDICES
    assert market_class_for("DXY") is not CRYPTO
    assert market_class_for("XAUUSD") is not CRYPTO


def test_an_unknown_symbol_is_treated_as_a_weekday_market() -> None:
    """Fail safe: dropping a real bar costs a session, inventing one fabricates a high."""
    market = market_class_for("SOMETHINGNEW")
    assert market.trades_weekends is False
    assert market is FOREX


def test_lookup_is_case_insensitive() -> None:
    assert market_class_for("btcusd") is CRYPTO
    assert market_class_for("xauusd") is METALS


def test_each_family_can_describe_itself() -> None:
    for market in (CRYPTO, FOREX, METALS, INDICES):
        line = describe(market)
        assert market.key in line
        assert len(line) > len(market.key) + 2


# ---------------------------------------------------------------------------
# Sessions by day
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("weekday", range(5))
def test_weekdays_are_sessions_for_every_market(weekday: int) -> None:
    monday = datetime(2026, 11, 16, tzinfo=UTC).date()
    day = monday.fromordinal(monday.toordinal() + weekday)
    assert day.weekday() == weekday
    for market in (CRYPTO, FOREX, METALS, INDICES):
        assert is_session(day, market) is True


@pytest.mark.parametrize("weekday", [5, 6])
def test_weekend_days_are_sessions_only_for_crypto(weekday: int) -> None:
    day = datetime(2026, 11, 21, tzinfo=UTC).date()  # Saturday
    day = day.fromordinal(day.toordinal() + (weekday - 5))
    assert day.weekday() >= 5
    assert is_session(day, CRYPTO) is True
    for market in (FOREX, METALS, INDICES):
        assert is_session(day, market) is False


# ---------------------------------------------------------------------------
# The weekend closure window
# ---------------------------------------------------------------------------


def test_the_window_is_friday_night_to_sunday_night() -> None:
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)  # Friday midday
    closed_from, open_from = trading_sessions.weekend_window(FOREX, now)
    assert closed_from.isoformat() == "2026-11-20T22:00:00+00:00"
    assert open_from.isoformat() == "2026-11-22T22:00:00+00:00"
    assert closed_from.weekday() == 4  # Friday
    assert open_from.weekday() == 6  # Sunday


def test_friday_midday_is_open() -> None:
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    assert trading_sessions.is_closed_for_weekend(FOREX, now) is False


def test_friday_just_after_the_close_is_shut() -> None:
    now = datetime(2026, 11, 20, 22, 30, tzinfo=UTC)
    assert trading_sessions.is_closed_for_weekend(FOREX, now) is True


def test_saturday_morning_is_shut() -> None:
    """The precise case the age check got wrong.

    Friday's last tick was 20:00; at 01:00 Saturday it is five hours old, which
    the staleness window happily calls live. The market shut three hours earlier.
    """
    now = datetime(2026, 11, 21, 1, 0, tzinfo=UTC)
    assert now.weekday() == 5
    assert trading_sessions.is_closed_for_weekend(FOREX, now) is True


def test_sunday_evening_is_open_again() -> None:
    now = datetime(2026, 11, 22, 22, 30, tzinfo=UTC)
    assert trading_sessions.is_closed_for_weekend(FOREX, now) is False


def test_crypto_is_never_shut_for_the_weekend() -> None:
    for moment in (
        datetime(2026, 11, 20, 23, 0, tzinfo=UTC),
        datetime(2026, 11, 21, 12, 0, tzinfo=UTC),
        datetime(2026, 11, 22, 12, 0, tzinfo=UTC),
    ):
        assert trading_sessions.is_closed_for_weekend(CRYPTO, moment) is False
        assert trading_sessions.closed_since(CRYPTO, moment) is None
        assert trading_sessions.next_open(CRYPTO, moment) is None


def test_a_wednesday_question_names_the_coming_weekend() -> None:
    """Midweek, the window still describes the next closure, not the last one."""
    now = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)  # Wednesday
    closed_from, open_from = trading_sessions.weekend_window(FOREX, now)
    assert closed_from.isoformat() == "2026-11-20T22:00:00+00:00"
    assert trading_sessions.is_closed_for_weekend(FOREX, now) is False


def test_closed_since_and_next_open_agree_with_the_window() -> None:
    now = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)  # Saturday
    since = trading_sessions.closed_since(FOREX, now)
    until = trading_sessions.next_open(FOREX, now)
    assert since is not None and until is not None
    assert since < now < until
    assert (until - since).days == 2


# ---------------------------------------------------------------------------
# End to end: a quote's "live" claim now needs the market to be open
# ---------------------------------------------------------------------------


def _freeze_market_now(monkeypatch, moment: datetime) -> None:
    """Pin ``datetime.now`` inside market.py to a fixed instant."""
    from qna_bot.ai import market as market_mod

    real_datetime = datetime

    class _Frozen(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(market_mod, "datetime", _Frozen)


@pytest.mark.asyncio
async def test_saturday_early_hours_do_not_call_fridays_close_live(monkeypatch) -> None:
    """The bug the age check caused, on the exact clock where it fired.

    Friday's last tick is 20:00 UTC. At 01:00 Saturday it is five hours old, so
    the staleness window says "live" - for a market that shut three hours ago.
    """
    from exness_fixtures import FakeExness, settings_loader

    # Friday 2026-11-20 20:00 UTC close, asked at Saturday 01:00 UTC.
    close_time = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
    asked_at = datetime(2026, 11, 21, 1, 0, tzinfo=UTC)
    assert (asked_at - close_time).total_seconds() / 3600 < 12  # inside staleness

    _freeze_market_now(monkeypatch, asked_at)
    fake = FakeExness(
        candles={"EURUSD": [
            {
                "time": close_time.isoformat(),
                "open": 1.09,
                "high": 1.10,
                "low": 1.08,
                "close": 1.0984,
                "volume": 1.0,
            }
        ]}
    )
    from qna_bot.ai.market import MarketFeed

    feed = MarketFeed(fake.client(), settings_loader=settings_loader())
    try:
        snapshot = await feed.snapshot("EURUSD?")
    finally:
        await feed.close()

    quote = snapshot.quotes[0]
    assert quote.kind == "market_closed", "a shut market must never read as live"
    assert "market closed" in quote.label()


@pytest.mark.asyncio
async def test_crypto_on_a_saturday_stays_live(monkeypatch) -> None:
    """The complement: crypto really does trade, so the calendar must not gag it."""
    from exness_fixtures import FakeExness, settings_loader

    from qna_bot.ai.market import MarketFeed

    asked_at = datetime(2026, 11, 21, 1, 0, tzinfo=UTC)
    _freeze_market_now(monkeypatch, asked_at)
    fake = FakeExness(
        candles={"BTCUSD": [
            {
                "time": datetime(2026, 11, 21, 0, 50, tzinfo=UTC).isoformat(),
                "open": 64000.0,
                "high": 64100.0,
                "low": 63900.0,
                "close": 64050.0,
                "volume": 1.0,
            }
        ]}
    )
    feed = MarketFeed(fake.client(), settings_loader=settings_loader())
    try:
        snapshot = await feed.snapshot("BTC?")
    finally:
        await feed.close()

    assert snapshot.quotes[0].kind == "live"
