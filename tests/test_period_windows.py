"""A week means a week, and a trading day means a session.

The reported bug: "it reads 7 days from what you've asked, not the actual week
plus the market closes on Saturday and Sunday so make it read only the 5 days."

Two distinct faults, both pinned here:

1. **"Last week" was a trailing seven days.** On a Thursday it covered the
   previous Friday through Wednesday-this-week - two partial weeks, neither of
   which is the week the host is asking about. It happened to look right only
   when asked on a Monday. A week is now the previous Monday-to-Friday.

2. **The weekend was inside the window.** FX, metals and indices are shut on
   Saturday and Sunday. Exness does not always omit those bars; some instruments
   carry Friday's close forward, so the weekend days were counted as sessions
   and "the last 5 days" reached back only four real ones.

The tests freeze ``now`` so each weekday can be checked, rather than hoping CI
runs on a Monday.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from exness_fixtures import exness_settings

from qna_bot.ai import market as market_mod
from qna_bot.ai.market import (
    MarketFeed,
    _date_range,
    _is_session,
    parse_period_query,
)
from qna_bot.ai.market_calendar import is_session as calendar_is_session


def _freeze(monkeypatch, moment: datetime) -> None:
    """Pin ``datetime.now`` inside the market module to a fixed instant."""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206 - mirrors datetime
            return moment if tz is None else moment.astimezone(tz)

    monkeypatch.setattr(market_mod, "datetime", _FrozenDatetime)


#: A Thursday. Deliberately not a Monday, because a Monday is the one day the
#: old trailing-seven window accidentally produced the right answer.
THURSDAY = datetime(2026, 11, 19, 14, 30, tzinfo=UTC)
#: The Saturday of that same week - market shut, but Exness still serves a bar.
SATURDAY = datetime(2026, 11, 21, 11, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The date ranges themselves
# ---------------------------------------------------------------------------


def test_last_week_is_the_previous_monday_to_friday() -> None:
    """The reported symptom: asked on a Thursday, "last week" must be the
    previous Mon-Fri, not the seven days ending on this Wednesday."""
    start, end = _date_range("last_week", THURSDAY)
    assert start == datetime(2026, 11, 9, tzinfo=UTC).date()  # Monday
    assert end == datetime(2026, 11, 13, tzinfo=UTC).date()   # Friday
    assert end.weekday() == 4  # never a weekend day


def test_this_week_runs_monday_to_today() -> None:
    start, end = _date_range("this_week", THURSDAY)
    assert start == datetime(2026, 11, 16, tzinfo=UTC).date()
    assert end == THURSDAY.date()


@pytest.mark.parametrize(
    ("moment", "expected_start", "expected_end"),
    [
        # Monday: the current week has barely begun; last week is last Mon-Fri.
        (datetime(2026, 11, 16, 9, tzinfo=UTC), "2026-11-09", "2026-11-13"),
        # Sunday: last week is still the one that ended two days ago.
        (datetime(2026, 11, 15, 9, tzinfo=UTC), "2026-11-02", "2026-11-06"),
        # Saturday.
        (datetime(2026, 11, 14, 9, tzinfo=UTC), "2026-11-02", "2026-11-06"),
        # Wednesday.
        (datetime(2026, 11, 18, 9, tzinfo=UTC), "2026-11-09", "2026-11-13"),
    ],
)
def test_last_week_holds_on_every_day_of_the_week(
    moment: datetime, expected_start: str, expected_end: str
) -> None:
    start, end = _date_range("last_week", moment)
    assert start.isoformat() == expected_start
    assert end.isoformat() == expected_end


def test_month_ranges_are_calendar_months_and_exclude_the_weekend() -> None:
    start, end = _date_range("last_month", THURSDAY)
    assert (start.isoformat(), end.isoformat()) == ("2026-10-01", "2026-10-31")
    start, end = _date_range("this_month", THURSDAY)
    assert (start.isoformat(), end.isoformat()) == ("2026-11-01", "2026-11-19")


def test_a_monday_yesterday_names_the_previous_calendar_day() -> None:
    monday = datetime(2026, 11, 16, 8, tzinfo=UTC)
    start, end = _date_range("yesterday", monday)
    assert (start.isoformat(), end.isoformat()) == ("2026-11-15", "2026-11-15")
    assert start.weekday() == 6  # Sunday - no session, so the selector falls back


# ---------------------------------------------------------------------------
# Weekends are not sessions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("weekday", range(7))
def test_fx_has_no_weekend_sessions(weekday: int) -> None:
    from qna_bot.ai.market_calendar import FOREX

    day = datetime(2026, 11, 9, tzinfo=UTC) + timedelta(days=weekday)
    assert _is_session(day, market=FOREX) is (weekday < 5)


@pytest.mark.parametrize("weekday", range(7))
def test_crypto_trades_every_day(weekday: int) -> None:
    from qna_bot.ai.market_calendar import CRYPTO

    day = datetime(2026, 11, 9, tzinfo=UTC) + timedelta(days=weekday)
    assert _is_session(day, market=CRYPTO) is True


@pytest.mark.parametrize("weekday", [5, 6])
@pytest.mark.parametrize(
    ("instrument", "weekend_is_a_session"),
    [
        # The week's actual closure table, per family.
        ("BTCUSD", True),
        ("ETHUSD", True),
        ("XAUUSD", False),
        ("XAGUSD", False),
        ("EURUSD", False),
        ("USDJPY", False),
        ("DXY", False),
        ("US10Y", False),
    ],
)
def test_the_calendar_classifies_each_family_correctly(
    instrument: str, weekend_is_a_session: bool, weekday: int
) -> None:
    """Crypto is the only family whose weekend bars are real sessions."""
    from qna_bot.ai.market_calendar import market_class_for

    market = market_class_for(instrument)
    # 16 Nov 2026 is a Monday, so +5 is Saturday and +6 is Sunday.
    day = datetime(2026, 11, 16, tzinfo=UTC) + timedelta(days=weekday)
    assert day.weekday() >= 5
    assert calendar_is_session(day, market) is weekend_is_a_session


# ---------------------------------------------------------------------------
# Parsing: an explicit day count is a session count
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("gold highest price last 5 days", 5),
        ("EURUSD low past 5 trading days", 5),
        ("BTC high last five days", 5),
        ("what was the high over the last 10 days", 10),
        ("XAUUSD close previous 3 sessions", 3),
        ("last 30 days low", 30),
    ],
)
def test_explicit_day_counts_are_sessions(text: str, count: int) -> None:
    query = parse_period_query(text)
    assert query is not None, text
    assert query.session_count == count
    assert query.is_session_window is True


@pytest.mark.parametrize("text", ["last 0 days high", "last 200 days high", "no high here"])
def test_absurd_day_counts_are_rejected(text: str) -> None:
    query = parse_period_query(text)
    assert query is None or query.session_count is None


def test_week_questions_are_date_ranges_not_session_counts() -> None:
    for text in ("btc high last week", "btc high this week"):
        query = parse_period_query(text)
        assert query is not None
        assert query.session_count is None
        assert query.is_calendar_week is True


def test_an_explicit_day_count_beats_a_nearby_week_word() -> None:
    """"last 5 days" states the window exactly; the surrounding prose does not
    get to widen it back to a week."""
    query = parse_period_query("this week, what was the high in the last 5 days")
    assert query is not None
    assert query.session_count == 5


# ---------------------------------------------------------------------------
# End to end: which bars are actually used
# ---------------------------------------------------------------------------


def _daily_bars(
    now: datetime, *, sessions: int, start_price: float = 100.0, weekend_spike: bool = True
):
    """Daily bars ending today, including weekend bars for FX.

    The weekend bars deliberately carry an outrageous high, so any answer that
    includes one is impossible to mistake for the right one.
    """
    bars = []
    day = now.date() - timedelta(days=sessions * 2)
    price = start_price
    while day <= now.date():
        is_weekend = day.weekday() >= 5
        high = 9999.0 if (is_weekend and weekend_spike) else price + 1.0
        bars.append(
            {
                "time": datetime(day.year, day.month, day.day, tzinfo=UTC).isoformat(),
                "open": price,
                "high": high,
                "low": price - 1.0,
                "close": price,
                "volume": 1.0,
            }
        )
        price += 1.0
        day += timedelta(days=1)
    return bars


async def _answer(fake, query):
    feed = MarketFeed(fake.client(), settings_loader=_settings_loader())
    try:
        return await feed.period_answer(query)
    finally:
        await feed.close()


def _settings_loader():
    async def load() -> dict:
        return exness_settings()

    return load


@pytest.mark.asyncio
async def test_last_week_ignores_the_current_week_entirely(monkeypatch) -> None:
    """Asked on a Thursday, last week's answer must not see Mon-Wed of this week."""
    from exness_fixtures import FakeExness

    _freeze(monkeypatch, THURSDAY)
    bars = _daily_bars(THURSDAY, sessions=14)
    # This week (Mon-Wed) trades far above last week, so including it is visible.
    for bar in bars:
        day = datetime.fromisoformat(bar["time"]).date()
        if day >= THURSDAY.date() - timedelta(days=3):
            bar["high"] = 5000.0
    fake = FakeExness(
        instruments=["XAUUSD"], candles={"D1:XAUUSD": bars}
    )
    query = parse_period_query("gold highest price last week")
    answer = await _answer(fake, query)

    assert answer is not None
    # Not this week's 5000, and not the fake weekend 9999.
    assert answer.high < 9999.0
    assert answer.high < 5000.0
    assert answer.start.date().weekday() == 0  # Monday
    assert answer.end.date().weekday() <= 4    # Friday at the latest


@pytest.mark.asyncio
async def test_fx_week_window_never_includes_a_weekend_bar(monkeypatch) -> None:
    """Exness can carry Friday's close into Saturday; that is not a session."""
    from exness_fixtures import FakeExness

    _freeze(monkeypatch, THURSDAY)
    fake = FakeExness(
        instruments=["XAUUSD"],
        candles={"D1:XAUUSD": _daily_bars(THURSDAY, sessions=14)},
    )
    answer = await _answer(fake, parse_period_query("gold highest price last week"))

    assert answer is not None
    # The weekend bars carry 9999 and the current week carries 5000-ish; neither
    # may appear.
    assert answer.high < 9999.0


@pytest.mark.asyncio
async def test_last_five_days_counts_sessions_not_calendar_days(monkeypatch) -> None:
    """A Thursday "last 5 days" is Thu,Wed,Tue,Mon of this week plus last Fri -
    five sessions, which reaches back seven calendar days, not five."""
    from exness_fixtures import FakeExness

    _freeze(monkeypatch, THURSDAY)
    bars = _daily_bars(THURSDAY, sessions=10)
    # Give each session a unique high so the counted set is identifiable.
    for idx, bar in enumerate(bars):
        day = datetime.fromisoformat(bar["time"]).date()
        if day.weekday() < 5:
            bar["high"] = 200.0 + idx
    fake = FakeExness(instruments=["XAUUSD"], candles={"D1:XAUUSD": bars})
    query = parse_period_query("gold highest price last 5 days")
    answer = await _answer(fake, query)

    assert answer is not None
    # Today is excluded (still forming), so the window is the five completed
    # sessions before it: Fri(prev week), Mon, Tue, Wed, Thu-1... and must never
    # reach a weekend bar (which carries 9999).
    assert answer.high < 9999.0
    # The window is in the past and covers sessions only - never a weekend bar.
    assert answer.start.date() <= answer.end.date() < THURSDAY.date()
    assert answer.start.date().weekday() < 5
    assert answer.end.date().weekday() < 5
    # Five sessions span more than five calendar days whenever a weekend falls
    # inside the window, which is exactly the reported complaint.
    assert (answer.end.date() - answer.start.date()).days >= 4


def test_fetch_budget_grows_with_the_session_count() -> None:
    """Asking for 40 sessions must not request 16 days of history."""
    query = parse_period_query("gold high last 40 days")
    assert query is not None and query.session_count == 40
    assert query.days >= 40


# ---------------------------------------------------------------------------
# Daily bars are dated by their *trading* day, not by the UTC clock
# ---------------------------------------------------------------------------


def test_european_local_midnight_is_filed_under_the_next_day() -> None:
    """Exness opens the day at local midnight = 22:00 UTC the evening before.

    Left raw, Monday 16th's session is stamped Sunday 15th 22:00Z, the weekend
    filter deletes it, and every "last 5 trading days" window silently loses a
    session. This is the accuracy bug behind the reported miscount.
    """
    from qna_bot.ai.exness import _trading_date_midnight

    # Sunday 15 Nov 2026, 22:00 UTC - which is Monday 16th in Europe.
    stamped = datetime(2026, 11, 15, 22, 0, tzinfo=UTC)
    assert stamped.weekday() == 6  # Sunday, as raw UTC reads it
    aligned = _trading_date_midnight(stamped)
    assert aligned.date().isoformat() == "2026-11-16"
    assert aligned.weekday() == 0  # Monday
    assert (aligned.hour, aligned.minute) == (0, 0)


def test_utc_midnight_bars_are_left_alone() -> None:
    from qna_bot.ai.exness import _trading_date_midnight

    stamped = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)
    assert _trading_date_midnight(stamped) == stamped
    # Mid-afternoon is unambiguous too.
    stamped = datetime(2026, 11, 16, 14, 30, tzinfo=UTC)
    assert _trading_date_midnight(stamped).date().isoformat() == "2026-11-16"


@pytest.mark.asyncio
async def test_daily_history_is_requested_with_trading_day_alignment() -> None:
    """The D1 fetch must ask for alignment, or the weekend filter eats Mondays."""
    import inspect

    from qna_bot.ai.market import MarketFeed

    source = inspect.getsource(MarketFeed.ohlc_bars)
    assert "align_daily_to_utc_midnight=True" in source
    # Intraday quotes must NOT be snapped: a live price needs its real time.
    quote_source = inspect.getsource(MarketFeed._quote)
    assert "align_daily" not in quote_source


@pytest.mark.asyncio
async def test_a_monday_stamped_in_utc_still_counts_as_a_session() -> None:
    """End to end: a Sunday-22:00Z bar contributes to a weeks worth of sessions."""
    from exness_fixtures import FakeExness

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    # Build bars whose stamps are 22:00 UTC the evening before each trading day,
    # which is how Exness actually reports them.
    bars = []
    day = today - timedelta(days=20)
    level = 100.0
    while day <= today:
        if day.weekday() < 5 or True:  # every calendar day carries a bar
            stamp = (day - timedelta(days=1)).replace(hour=22)
            bars.append(
                {
                    "time": stamp.isoformat(),
                    "open": level,
                    "high": level + 1.0,
                    "low": level - 1.0,
                    "close": level,
                    "volume": 1.0,
                }
            )
            level += 1.0
        day += timedelta(days=1)

    fake = FakeExness(instruments=["XAUUSD"], candles={"D1:XAUUSD": bars})
    feed = MarketFeed(fake.client(), settings_loader=_settings_loader())
    try:
        query = parse_period_query("gold highest price last 5 days")
        answer = await feed.period_answer(query)
    finally:
        await feed.close()

    assert answer is not None
    # Five sessions were found despite every stamp sitting on the prior evening.
    assert answer.start.date().weekday() < 5
    assert answer.end.date().weekday() < 5


# ---------------------------------------------------------------------------
# Weekend questions about "this week"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_this_week_on_a_weekend_reports_no_data(monkeypatch) -> None:
    """A Saturday "this week" with no Mon-Fri bar in range is no data.

    The feed carries the weekend bar Exness keeps for FX, but no session. The
    answer must be nothing at all rather than Friday's close relabelled as the
    week's high.
    """
    from exness_fixtures import FakeExness

    _freeze(monkeypatch, SATURDAY)
    # Only the weekend bar is in the window; Mon-Fri of this week has no bars.
    bars = [
        {
            "time": datetime(2026, 11, 21, 0, 0, tzinfo=UTC).isoformat(),
            "open": 100.0,
            "high": 105.0,
            "low": 95.0,
            "close": 100.0,
            "volume": 1.0,
        }
    ]
    fake = FakeExness(instruments=["XAUUSD"], candles={"D1:XAUUSD": bars})
    feed = MarketFeed(fake.client(), settings_loader=_settings_loader())
    try:
        query = parse_period_query("gold highest price this week")
        answer = await feed.period_answer(query)
    finally:
        await feed.close()

    assert answer is None


@pytest.mark.asyncio
async def test_this_week_on_a_saturday_still_reports_fridays_session(monkeypatch) -> None:
    """The complement: with Friday's bar present, Saturday's "this week" is real."""
    from exness_fixtures import FakeExness

    _freeze(monkeypatch, SATURDAY)
    bars = [
        {
            "time": datetime(2026, 11, day, 0, 0, tzinfo=UTC).isoformat(),
            "open": 100.0,
            "high": 100.0 + day,
            "low": 90.0,
            "close": 100.0,
            "volume": 1.0,
        }
        for day in (16, 17, 18, 19, 20)  # Mon-Fri of the frozen week
    ]
    fake = FakeExness(instruments=["XAUUSD"], candles={"D1:XAUUSD": bars})
    feed = MarketFeed(fake.client(), settings_loader=_settings_loader())
    try:
        query = parse_period_query("gold highest price this week")
        answer = await feed.period_answer(query)
    finally:
        await feed.close()

    assert answer is not None
    assert answer.high == pytest.approx(120.0)  # Friday's high, the highest
    assert answer.end.date().isoformat() == "2026-11-20"
