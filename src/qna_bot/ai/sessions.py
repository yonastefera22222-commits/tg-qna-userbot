"""Is this market open right now, and if not, since when?

The bot decides "live price" versus "market closed" by the age of the newest
candle. Age is a bad proxy, in both directions:

* **A shut market looks live on Saturday morning.** The last tick was 20:00
  Friday; at 09:00 Saturday it is 37 hours old and correctly reads closed, but
  at 01:00 Saturday it is only 5 hours old - inside the staleness window - so
  Friday's close was posted as the current price of a market that had shut four
  hours earlier. In a forex giveaway asking for "the price now", that is a wrong
  answer stated confidently, on the one day of the week it is most likely to be
  asked, because the weekend is when the questions are asked about the week just
  gone.
* **A quiet open market looks shut.** A Sunday-evening reopen or a Monday
  holiday session can have no tick for hours, and reporting "market closed"
  while the market is technically open is a refusal the trader cannot explain.

So the calendar decides *whether* the market is open, and the candle's age
decides only whether the number inside it is still fresh enough to repeat.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from .market_calendar import CLOSED_WEEKENDS, MarketClass

#: The weekend window for a weekday market, in UTC.
#:
#: Friday 22:00 UTC is the Friday New York close in winter (17:00 EST); in
#: summer the close is 21:00 UTC. Sunday 22:00 UTC is the Sunday reopen in
#: summer (17:00 EDT) and 23:00 UTC in winter. The window is therefore two hours
#: wide at each end, and the deliberate choice is to *start* it late and *end* it
#: early relative to the widest reading: a market that shut at 21:00 UTC but is
#: still called open is caught by the age check almost immediately (minutes),
#: whereas a market wrongly called closed refuses a price a trader can see on
#: their own platform, which is the more damaging of the two mistakes.
WEEKEND_CLOSE = time(22, 0)
WEEKEND_REOPEN = time(22, 0)


def weekend_window(market: MarketClass, now: datetime) -> tuple[datetime, datetime]:
    """The current or next weekend closure as ``(closed_from, open_from)``.

    ``now`` may fall inside the window; callers check that with
    :func:`is_closed_for_weekend`.
    """
    moment = now.astimezone(UTC)
    day = moment.date()
    # 4 = Friday. Walking back to the Friday of *this* week first matters: from
    # Saturday or Sunday, "days to Friday" would otherwise jump forward a week
    # and describe a closure that has not happened yet.
    days_since_friday = (day.weekday() - 4) % 7
    friday = day - timedelta(days=days_since_friday)
    closed_from = datetime.combine(friday, WEEKEND_CLOSE, tzinfo=UTC)
    open_from = closed_from + timedelta(days=2)  # Sunday, same hour
    if moment >= open_from:
        # Past this weekend already (Monday to Thursday): describe the next one.
        closed_from += timedelta(days=7)
        open_from += timedelta(days=7)
    return closed_from, open_from


def is_closed_for_weekend(market: MarketClass, now: datetime) -> bool:
    """True inside the Friday-night to Sunday-night closure."""
    if market.weekend != CLOSED_WEEKENDS:
        return False
    closed_from, open_from = weekend_window(market, now)
    return closed_from <= now.astimezone(UTC) < open_from


def closed_since(market: MarketClass, now: datetime) -> datetime | None:
    """When the market shut, if it is shut now. Else ``None``."""
    if not is_closed_for_weekend(market, now):
        return None
    return weekend_window(market, now)[0]


def next_open(market: MarketClass, now: datetime) -> datetime | None:
    """When the market reopens, if it is shut now. Else ``None``."""
    if not is_closed_for_weekend(market, now):
        return None
    return weekend_window(market, now)[1]
