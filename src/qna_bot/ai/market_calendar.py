"""Which markets are actually shut at the weekend, and what that changes.

The bot used to hold one boolean - "is this crypto?" - and derive everything
else from it. That is wrong in both directions, and both wrong directions reach
a group:

* **Indices were treated like crypto when the crypto check was skipped.** A
  stock-index CFD follows its exchange, so a Saturday "this week" answer could
  be built from a weekend bar that no exchange ever printed.
* **Gold and silver were lumped in with forex.** Most brokers do shut spot
  XAU/USD at Friday's New York close, so for the default case the old behaviour
  was right - but the classification was an accident of grouping rather than a
  statement of fact, and nothing in the code could tell the two apart when a
  broker *did* offer a weekend metals feed.

So the weekend rule lives here, once, and every caller asks this module instead
of comparing against a set of crypto symbols.

A note on what "closed at the weekend" does *not* mean. It is not only a
question of which daily bars exist. Exness frequently keeps serving a Sunday
bar for a shut market carrying Friday's close forward. Counted, that bar makes a
five-session window silently span seven days and makes a weekend "high" equal
Friday's close. It is dropped by date, which is what :func:`is_session` does.
"""

from __future__ import annotations

from datetime import date

#: How a market behaves over a weekend. Two classes are all the behaviour needs:
#: a market either prints sessions on Saturday and Sunday or it does not.
TRADES_WEEKENDS = "always"
CLOSED_WEEKENDS = "closed"


class MarketClass:
    """One instrument family and the weekend facts about it."""

    __slots__ = ("key", "weekend", "note")

    def __init__(self, key: str, weekend: str, note: str) -> None:
        self.key = key
        self.weekend = weekend
        self.note = note

    @property
    def trades_weekends(self) -> bool:
        return self.weekend == TRADES_WEEKENDS

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"MarketClass({self.key!r}, {self.weekend!r})"


#: Crypto settles every calendar day, including weekends and holidays. A weekend
#: bar here is a real session - the only family where that is true.
CRYPTO = MarketClass(
    "crypto",
    TRADES_WEEKENDS,
    "24/7; Saturday and Sunday are real sessions",
)

#: Forex: the interbank market shuts at the Friday New York close and reopens
#: Sunday evening. No Saturday session exists at any broker.
FOREX = MarketClass(
    "forex",
    CLOSED_WEEKENDS,
    "closed Friday evening to Sunday evening",
)

#: Spot metals. Gold and silver price off the same wholesale structure as forex
#: and the LBMA auctions run on business days, so the default is shut at the
#: weekend with forex. (Some brokers sell a separate 24/7 product, but that is a
#: different instrument, not this one.)
METALS = MarketClass(
    "metals",
    CLOSED_WEEKENDS,
    "LBMA business-day pricing; shut with forex",
)

#: Indices and yields - DXY, US10Y. These follow their underlying exchange or
#: fixing, which is a Monday-to-Friday institution. Crucially *not* crypto: this
#: is the family the old single-boolean model got wrong by omission.
INDICES = MarketClass(
    "indices",
    CLOSED_WEEKENDS,
    "follows the underlying exchange or fixing; weekdays only",
)

_BY_KEY = {c.key: c for c in (CRYPTO, FOREX, METALS, INDICES)}


def market_class_for(instrument: str) -> MarketClass:
    """The weekend behaviour of ``instrument``, which must be a known symbol.

    Unknown instruments are treated as weekday markets. That is the safe side of
    the error: dropping a bar that was real costs one session in a window, while
    counting a bar that was not real fabricates a high. The bot only ever asks
    about :data:`FEEDABLE_INSTRUMENTS`, so this is a guard rather than a path.
    """
    symbol = (instrument or "").upper()
    # Imported here, not at module scope: market.py imports this module, and the
    # instrument sets live there to keep one list of what the feed can quote.
    from .market import _CRYPTO_SYMBOLS

    if symbol in _CRYPTO_SYMBOLS:
        return CRYPTO
    if symbol in ("XAUUSD", "XAGUSD"):
        return METALS
    if symbol in ("DXY", "US10Y"):
        return INDICES
    return FOREX


def is_session(day: date, market: MarketClass) -> bool:
    """True when ``day`` is a trading session for ``market``.

    Weekend *days* are the whole question: no family here closes on a weekday and
    all of them are open on weekdays. Public holidays are deliberately not
    modelled - the broker's own bar series is the authority on whether a holiday
    session exists, and inventing a holiday calendar would drop real sessions
    from any country the bot cannot see.
    """
    if market.trades_weekends:
        return True
    return day.weekday() < 5


def describe(market: MarketClass) -> str:
    """One line for the owner's diagnostics, never for a group answer."""
    return f"{market.key}: {market.note}"
