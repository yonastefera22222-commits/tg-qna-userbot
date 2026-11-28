"""Live market data for Forex groups, sourced from Exness.

A language model has no market feed. Until now a Forex-group answer could only
refuse live questions ("No live data"), which is honest but useless when the
whole point of the group is "EURUSD now?".

This module supplies the *numbers*, and the model supplies the *explanation*.
The split matters: model parameters carry a price only up to their training
cutoff, so any figure the model produces from memory is stale by definition.
Every number here is fetched at answer time and carries the timestamp of the
quote, and the prompt is told to use these figures only.

The single source is the **Exness Trader API** (see :mod:`.exness`), read over
the owner's own trading account. That is not a stylistic choice:

* A giveaway is scored against the Exness terminal in front of the host. A tick
  from a different liquidity pool can be pips off, and pips are the answer.
* Exness publishes no anonymous quote feed - quotes are account-scoped - so
  "Exness data" and "credentials configured" are the same condition.

There is deliberately **no fallback to another vendor**. If Exness is not
connected, is unreachable, or the market is shut, the digest is empty (or says
"market closed") and the answer says no live data. Substituting a different
broker's or a central bank's number under a Forex group's nose is exactly the
fabrication this module exists to prevent: a wrong price posted confidently is
worse than an admitted gap.

Freshness is the rest of the product. A trader told "EURUSD 1.0984" when the
last tick was three days old is worse off than one who is told nothing, so every
digest records when the figure was taken and labels a shut market as shut.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import httpx

from . import market_calendar as calendar
from . import sessions as trading_sessions
from .exness import (
    HISTORY_TIMEFRAME,
    QUOTE_CANDLE_COUNT,
    QUOTE_STALE_MINUTES,
    QUOTE_TIMEFRAME,
    ExnessClient,
    ExnessCredentialsMissing,
    ExnessError,
    credentials_from,
)

logger = logging.getLogger(__name__)

#: A quote older than this is not "now". FX trades ~24/5, so a weekend gap is
#: normal and must be labelled as such rather than hidden.
STALE_AFTER_MINUTES = int(QUOTE_STALE_MINUTES)
#: Whole-digest cache. A giveaway fires several questions within seconds of
#: each other; re-fetching for each one would add latency and burn rate limits.
CACHE_TTL_SECONDS = 30.0
#: Never let a slow feed delay an answer more than this.
FEED_TIMEOUT_SECONDS = 6.0
#: How many daily bars to keep warm. Two months covers the longest period
#: question ("last month") without paying for history nobody asked about.
HISTORY_BARS = 64


#: Amharic names for the instruments a giveaway asks about, mapped to the
#: ticker. These are transliterations, not translations: "ቢቲሲ" is how BTC is
#: written in Ethiopic script, and a poster printed in Addis will use it.
_AMHARIC_INSTRUMENTS: dict[str, str] = {
    "ቢቲሲ": "BTCUSD",
    "ቢትኮይን": "BTCUSD",
    "ኢቲኤች": "ETHUSD",
    "ኢቴርየም": "ETHUSD",
    "ሶል": "SOLUSD",
    "ሶላና": "SOLUSD",
    "ዶጅ": "DOGEUSD",
    "ወርቅ": "XAUUSD",
    "ብር": "XAGUSD",
    "ዶላር": "DXY",
}

#: Traders' shorthand -> the instrument Yahoo quotes.
_SHORTHAND: dict[str, str] = {
    "EU": "EURUSD",
    "GU": "GBPUSD",
    "UJ": "USDJPY",
    "EJ": "EURJPY",
    "GJ": "GBPJPY",
    "CABLE": "GBPUSD",
    "FIBER": "EURUSD",
    "LOONIE": "USDCAD",
    "AUSSIE": "AUDUSD",
    "KIWI": "NZDUSD",
    "SWISSY": "USDCHF",
    "DXY": "DXY",
    "DXYX": "DXY",
    "GOLD": "XAUUSD",
    "SILVER": "XAGUSD",
}

_METALS = {"XAU": "XAUUSD", "XAG": "XAGUSD"}

#: Crypto is quoted around the clock, including weekends, so a period question
#: about it is always answerable without a "market closed" caveat.
_CRYPTO_NAMES = {
    "BTC": "BTCUSD", "BITCOIN": "BTCUSD",
    "ETH": "ETHUSD", "ETHEREUM": "ETHUSD",
    "SOL": "SOLUSD", "SOLANA": "SOLUSD",
    "XRP": "XRPUSD", "RIPPLE": "XRPUSD",
    "DOGE": "DOGEUSD", "DOGECOIN": "DOGEUSD",
    "BNB": "BNBUSD", "LTC": "LTCUSD", "LITECOIN": "LTCUSD",
}
#: Bare tokens that map straight onto a feedable instrument.
#: "BTCUSD", "BTCUSDT", "BTC/USDT", "btcusd" - how brokers (Exness included)
#: and charts actually write crypto pairs.
#: Start/end of an instrument token. ``\b`` is wrong here: it treats every
#: Ethiopic letter as a word character, so ``በBTC ገበታ`` (Amharic for "on the BTC
#: chart") has no boundary before the B and the instrument is never found. The
#: lookaround versions only exclude the characters that could genuinely be part
#: of a ticker, which keeps ``BTCUSD`` and ``BTC/usd`` together while letting the
#: Amharic prefix through.
_TICKER_START = r"(?<![A-Za-z0-9])"
_TICKER_END = r"(?![A-Za-z0-9])"

_CRYPTO_PAIR_RE = re.compile(
    _TICKER_START
    + r"(" + "|".join(_CRYPTO_NAMES) + r")\s*(?:/|-)?\s*(?:usdt|usd|busd)"
    + _TICKER_END,
    re.IGNORECASE,
)

_BARE_MAP = {
    **_METALS,
    **_CRYPTO_NAMES,
    "GOLD": "XAUUSD",
    "SILVER": "XAGUSD",
    "DXY": "DXY",
    "US10Y": "US10Y",
}

#: Everything the feed will quote. Membership here is only *recognition* - an
#: instrument the bot can name is not necessarily one the owner's Exness account
#: can quote, and the account's own instrument list decides that at fetch time.
#: Crypto instruments the feed will ask Exness for. Around the clock, so a
#: period question about them never carries a "market closed" caveat.
_CRYPTO_SYMBOLS: dict[str, str] = {
    "BTCUSD": "BTCUSD",
    "ETHUSD": "ETHUSD",
    "SOLUSD": "SOLUSD",
    "XRPUSD": "XRPUSD",
    "DOGEUSD": "DOGEUSD",
    "BNBUSD": "BNBUSD",
    "LTCUSD": "LTCUSD",
}

#: Oil and the indices. Named here so a question about them is *recognised*; the
#: owner's Exness account decides which names it actually quotes (see
#: ``exness._INSTRUMENT_ALIASES``). Without these entries "USOIL" and
#: "US Tech 100" matched nothing, the feed returned an empty snapshot, and the
#: answer degraded to "no live data" - indistinguishable from a dead feed.
_INDEX_AND_COMMODITY_SYMBOLS: dict[str, str] = {
    "USOIL": "USOIL",
    "UKOIL": "UKOIL",
    "NATGAS": "NATGAS",
    "USTEC": "USTEC",
    "US500": "US500",
    "US30": "US30",
    "US2000": "US2000",
    "GER40": "GER40",
    "UK100": "UK100",
    "FRA40": "FRA40",
    "EU50": "EU50",
    "JP225": "JP225",
    "HK50": "HK50",
    "AUS200": "AUS200",
}

#: The equity indices and index CFDs, excluding energy. The calendar needs the
#: split because oil and the Nasdaq do not observe the same session.
_INDEX_SYMBOLS: frozenset[str] = frozenset(
    {
        "USTEC", "US500", "US30", "US2000",
        "GER40", "UK100", "FRA40", "EU50", "JP225", "HK50", "AUS200",
        "DXY", "US10Y",
    }
)

FEEDABLE_INSTRUMENTS: frozenset[str] = frozenset(
    {
        *_CRYPTO_SYMBOLS,
        *_INDEX_AND_COMMODITY_SYMBOLS,
        "XAUUSD",
        "XAGUSD",
        "DXY",
        "US10Y",
        "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
        "EURGBP", "EURJPY", "GBPJPY", "EURAUD", "EURCHF", "EURCAD", "EURNZD",
        "GBPCHF", "GBPAUD", "GBPCAD", "GBPNZD", "AUDJPY", "CADJPY", "CHFJPY",
        "NZDJPY", "AUDCAD", "AUDCHF", "AUDNZD", "NZDCAD", "NZDCHF", "CADCHF",
        "USDSGD", "USDZAR", "USDTRY", "USDMXN", "USDNOK", "USDSEK",
        "USDPLN", "USDHUF", "USDCZK", "USDCNH", "USDINR",
    }
)

#: Instruments eligible for period questions ("highest price last week").
#: These are answered from the account's own daily candles, never a model.
HISTORY_INSTRUMENTS: frozenset[str] = FEEDABLE_INSTRUMENTS


_PAIR_RE = re.compile(r"^([A-Z]{3})/?([A-Z]{3})$")
#: Bare metals and the yield/index tokens a trader types on their own.
_BARE_RE = re.compile(
    _TICKER_START
    + r"(XAU|XAG|GOLD|SILVER|DXY|US10Y|BTC|ETH|SOL|XRP|DOGE|BNB|LTC|"
    r"BITCOIN|ETHEREUM|SOLANA|RIPPLE|DOGECOIN|LITECOIN)"
    + _TICKER_END
)
_NICKNAME_RE = re.compile(
    _TICKER_START
    + r"(" + "|".join(sorted(_SHORTHAND, key=len, reverse=True)) + r")"
    + _TICKER_END
)
_PAIR_TOKEN_RE = re.compile(_TICKER_START + r"([A-Z]{3})\s*[/\-]?\s*([A-Z]{3})" + _TICKER_END)

#: How a trader writes oil and the indices in a sentence. "US Tech 100" is the
#: name on the platform; "USTEC", "NAS100" and "the Nasdaq" are the same index.
#: Matching is phrase-based and case-insensitive because these names contain
#: spaces and punctuation, which the token regexes above deliberately reject.
#: Longest phrase first, so "us tech 100" wins over "us tech" and "crude oil"
#: over "oil".
_INDEX_PHRASES: dict[str, str] = {
    "us tech 100": "USTEC", "us tech": "USTEC", "us tech 100 index": "USTEC",
    "nasdaq 100": "USTEC", "nasdaq": "USTEC", "ustech": "USTEC", "ustec": "USTEC",
    "nas100": "USTEC", "us100": "USTEC", "ndx": "USTEC",
    "s&p 500": "US500", "s&p500": "US500", "spx500": "US500", "sp500": "US500",
    "s&p": "US500", "spx": "US500", "us500": "US500",
    "dow jones": "US30", "dow 30": "US30", "dj30": "US30", "ws30": "US30",
    "us30": "US30", "djia": "US30",
    "russell 2000": "US2000", "russell": "US2000", "us2000": "US2000",
    "dax 40": "GER40", "dax40": "GER40", "dax": "GER40", "ger40": "GER40",
    "de40": "GER40", "ger30": "GER40", "de30": "GER40",
    "ftse 100": "UK100", "ftse100": "UK100", "ftse": "UK100", "uk100": "UK100",
    "cac 40": "FRA40", "cac40": "FRA40", "cac": "FRA40", "fra40": "FRA40",
    "stoxx 50": "EU50", "stoxx": "EU50", "eu50": "EU50", "esx50": "EU50",
    "nikkei 225": "JP225", "nikkei": "JP225", "jpn225": "JP225", "jp225": "JP225",
    "hang seng": "HK50", "hangseng": "HK50", "hk50": "HK50", "hsi": "HK50",
    "asx 200": "AUS200", "aus200": "AUS200", "asx200": "AUS200",
    "wti crude": "USOIL", "wti": "USOIL", "us oil": "USOIL", "usoil": "USOIL",
    "crude oil": "USOIL", "crude": "USOIL", "oil": "USOIL", "xtiusd": "USOIL",
    "wticousd": "USOIL", "uscrude": "USOIL",
    "brent crude": "UKOIL", "brent": "UKOIL", "uk oil": "UKOIL", "ukoil": "UKOIL",
    "xbrusd": "UKOIL",
    "natural gas": "NATGAS", "nat gas": "NATGAS", "natgas": "NATGAS",
    "xngusd": "NATGAS",
}

#: One regex over every phrase, longest first so the most specific name wins.
#: The optional trailing letter is the account-type suffix a trader copies off
#: their own platform (``USOILm``, ``USTECm``); it is dropped before the lookup.
_INDEX_PHRASE_RE = re.compile(
    _TICKER_START
    + r"("
    + "|".join(re.escape(k) for k in sorted(_INDEX_PHRASES, key=len, reverse=True))
    + r")"
    + r"(?:m|c|z)?"
    + _TICKER_END,
    re.IGNORECASE,
)

#: Currencies that can form a spot pair. Recognition only - the owner's Exness
#: account decides which of these it actually quotes.
_PAIR_CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "CNY", "SEK",
    "NOK", "DKK", "HKD", "INR", "MXN", "ZAR", "TRY", "SGD", "PLN", "CZK",
    "HUF", "RON", "BRL", "ILS", "KRW", "PHP", "THB", "MYR", "IDR", "ISK",
}


@dataclass
class Quote:
    """One fetched figure, with everything needed to describe it honestly."""

    instrument: str
    price: float
    #: "live" (an Exness tick from a trading session that is still open) or
    #: "market_closed" (the last tick before the market shut - still a real
    #: Exness price, but never presentable as the current one).
    kind: str
    source: str
    as_of: datetime
    change_percent: float | None = None
    #: Raw digits a trader would quote. JPY pairs need 4 dp, metals 2, FX 5.
    decimals: int = 5
    note: str = ""
    #: Which Exness symbol answered, e.g. ``EURUSDm`` on a mini account. The
    #: owner's diagnostics show it; group replies never do.
    symbol: str = ""

    @property
    def age_minutes(self) -> float:
        return (datetime.now(UTC) - self.as_of).total_seconds() / 60

    def formatted(self) -> str:
        return f"{self.price:.{self.decimals}f}"

    def label(self, *, detailed: bool = False) -> str:
        """The instrument and its price, without anything the model could echo.

        Timestamps are deliberately absent by default. A group reply that reads
        ``EURUSD 1.09840 (2026-11-23 05:01 UTC)`` announces both that a machine
        produced it and that a feed was consulted, which is not what a person
        typing a price into a giveaway does. ``detailed=True`` is for the
        owner's own diagnostics, never for a group answer.
        """
        stamp = self.as_of.strftime("%Y-%m-%d %H:%M UTC")
        if detailed:
            # Owner diagnostics: the clock, the venue and the exact symbol all
            # help when a price looks wrong. This never reaches a group.
            symbol = f" {self.symbol}" if self.symbol and self.symbol != self.instrument else ""
            if self.kind == "market_closed":
                return (
                    f"{self.instrument} {self.formatted()} "
                    f"(Exness{symbol}, last tick {stamp}, market closed)"
                )
            return f"{self.instrument} {self.formatted()} (Exness{symbol}, {stamp})"
        # A closed market still needs saying: without it the model would present
        # Friday's close as the live price. A bare state tag does that without
        # exposing a clock.
        if self.kind == "market_closed":
            return f"{self.instrument} {self.formatted()} (market closed)"
        return f"{self.instrument} {self.formatted()}"


@dataclass
class PeriodAnswer:
    """A computed fact about a past window: not a quote, an answer."""

    instrument: str
    period: str
    aggregation: str
    value: float
    high: float
    low: float
    open: float
    close: float
    start: datetime
    end: datetime
    decimals: int = 2

    def formatted(self) -> str:
        # "change" is a percentage, everything else is a price.
        if self.aggregation == "change":
            return f"{self.value:+.2f}%"
        return f"{self.value:.{self.decimals}f}"


@dataclass
class MarketSnapshot:
    """A digest of the figures relevant to one question."""

    quotes: list[Quote] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    failed: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.quotes

    def as_prompt_block(self) -> str:
        """Render for the system prompt. Empty string when nothing was fetched."""
        if not self.quotes:
            return ""
        numbers = "\n".join(f"- {q.label()}" for q in self.quotes)
        caveats: list[str] = []
        if any(
            q.kind == "market_closed" or q.age_minutes > STALE_AFTER_MINUTES
            for q in self.quotes
        ):
            caveats.append(
                "Figures marked 'market closed' are the last available tick. Say the "
                "market is closed; never present them as live."
            )
        extra = ("\n" + "\n".join(caveats)) if caveats else ""
        return (
            "LIVE MARKET DATA (the only prices you may state):\n"
            f"{numbers}{extra}\n"
            "Quote the number exactly as written. Never add a date, a time, "
            "'as of', 'fetched', or a source name, and never mention data feeds, "
            "APIs or being updated - reply the way a trader glancing at a chart "
            "would: the price, and nothing around it."
        )


#: Period questions. "last week", "yesterday", "this month" - a window whose
#: high/low/close/change can be read straight off the OHLC series.
_PERIOD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(yesterday|last\s+session)\b", re.I), "yesterday"),
    (re.compile(r"\b(last|previous|past)\s+week\b", re.I), "last_week"),
    (re.compile(r"\bthis\s+week\b|\bweek\s+to\s+date\b|\bwtd\b", re.I), "this_week"),
    (re.compile(r"\b(last|previous|past)\s+month\b", re.I), "last_month"),
    (re.compile(r"\bthis\s+month\b|\bmonth\s+to\s+date\b|\bmtd\b", re.I), "this_month"),
    (re.compile(r"\b(today|so\s+far\s+today)\b", re.I), "today"),
    # Amharic. A transcribed poster or a typed question from an Ethiopian
    # giveaway group says "ይህ ሳምንት" where English says "this week", and none of the
    # Latin patterns above can match Ethiopic script. Ordered longest-first so
    # "ያለፈው ሳምንት" (last week) is not eaten by the bare "ሳምንት".
    (re.compile(r"ያለፈው\s+ሳምንት|ባለፈው\s+ሳምንት"), "last_week"),
    (re.compile(r"ይህ\s+ሳምንት|በዚህ\s+ሳምንት|የዚህ\s+ሳምንት"), "this_week"),
    (re.compile(r"ያለፈው\s+ወር|ባለፈው\s+ወር"), "last_month"),
    (re.compile(r"ይህ\s+ወር|በዚህ\s+ወር|የዚህ\s+ወር"), "this_month"),
    (re.compile(r"ትናንት"), "yesterday"),
    (re.compile(r"ዛሬ"), "today"),
)

#: What to read off the series.
_AGGREGATIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(highest|high|top|peak|maximum|max)\b", re.I), "high"),
    (re.compile(r"\b(lowest|low|bottom|minimum|min)\b", re.I), "low"),
    (re.compile(r"\b(open(?:ing)?|opened)\b", re.I), "open"),
    (re.compile(r"\b(close|closed|closing|end(?:ed)?)\b", re.I), "close"),
    (re.compile(r"\b(change|move|performance|return|gain|loss|up|down|%)\b", re.I), "change"),
    # Amharic. Checked after the Latin list, so a mixed-language question keeps
    # its English aggregation when it has one.
    # Amharic verbs inflect by prefix *and* suffix ("ተከፈተ" opened, "የተከፈተው"
    # the one that opened), so matching a full word form would need a conjugation
    # table. Matching the stem is what actually holds.
    (re.compile(r"ከፍተኛ"), "high"),
    (re.compile(r"ዝቅተኛ"), "low"),
    (re.compile(r"ከፈተ|ክፈት|መክፈቻ"), "open"),
    (re.compile(r"ዘጋ|ዘግተ|መዝጊያ"), "close"),
    (re.compile(r"ለውጥ|እድገት|መቶኛ|መቶኛው"), "change"),
)

#: How many calendar days of history to ask the broker for, per period. This is
#: a *fetch* budget, not the window: the window is decided by real dates below.
#: It is padded because the selection skips weekends and the still-forming day,
#: which would otherwise leave the buffer short by two days.
_PERIOD_FETCH_DAYS = {
    "today": 8,
    "yesterday": 8,
    "this_week": 14,
    "last_week": 21,
    "this_month": 45,
    "last_month": 75,
     # "last 5 trading days" needs five sessions plus a weekend plus today's
    # forming bar, so a fortnight is the smallest safe request.
    "last_5_days": 16,
}

#: Spelled-out numbers a poster may use instead of digits.
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "fourteen": 14, "twenty": 20,
    "thirty": 30, "sixty": 60, "ninety": 90,
}

#: "last 5 days", "past ten trading sessions". Checked BEFORE the general period
#: patterns, because an explicit day count is a more precise statement of intent
#: than the word "week" implied by it - and because these count *sessions*, so
#: the weekend is excluded rather than silently included.
_EXPLICIT_DAYS_RE = re.compile(
    r"\b(?:last|previous|past|over\s+the\s+(?:last|past))\s+"
    r"(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")\s+"
    r"(?:trading\s+|business\s+|market\s+)?(?:days?|sessions?)\b",
    re.I,
)


def _is_session(bar_time: datetime, *, market: calendar.MarketClass) -> bool:
    """True when this daily bar is a real trading session for ``market``.

    The weekend rule is not a property of the bar, it is a property of the
    instrument: crypto settles every calendar day, while forex, metals and
    indices are shut. Exness frequently still serves a Saturday/Sunday bar for a
    shut market carrying Friday's close forward, and that bar must not be
    counted - it would silently widen a five-session window to seven days and
    make a weekend "high" equal Friday's close.
    """
    return calendar.is_session(bar_time.date(), market)


def _date_range(period: str, now: datetime) -> tuple[date, date] | None:
    """The calendar window a period names, inclusive, as traders mean it.

    The whole point: "last week" is the previous **Monday to Friday**, not the
    seven days before today. A window ending "7 days ago" lands on an arbitrary
    weekday, which is how a Thursday question returned the wrong week's high.

    Weeks start Monday (ISO), the convention in every trading calendar.
    """
    today = now.date()
    monday = today - timedelta(days=today.weekday())
    if period == "this_week":
        return monday, today
    if period == "last_week":
        # Previous Monday to the Friday that closed it. Ending on Sunday would
        # put two non-sessions in the window; for instruments that carry Friday
        # forward, that silently re-counts Friday's range.
        return monday - timedelta(days=7), monday - timedelta(days=3)
    if period == "this_month":
        return today.replace(day=1), today
    if period == "last_month":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return last_prev.replace(day=1), last_prev
    if period == "today":
        return today, today
    if period == "yesterday":
        # The previous calendar day. The selector walks back to the last session
        # when that day had none, so a Monday question returns Friday.
        prev = today - timedelta(days=1)
        return prev, prev
    return None


@dataclass
class PeriodQuery:
    """A question about a past window, answerable from an OHLC series."""

    period: str
    aggregation: str
    instrument: str | None = None
    #: Set for an explicit "last N trading days", which counts *sessions* rather
    #: than calendar days so the weekend is not silently included.
    session_count: int | None = None

    @property
    def days(self) -> int:
        """Calendar days of history to request (fetch budget, not the window)."""
        if self.session_count:
            # Five sessions needs a week; forty needs two months. Two days of
            # slack per weekend plus the forming day is what the selector eats.
            return max(self.session_count + 4 + (self.session_count // 5) * 2, 10)
        return _PERIOD_FETCH_DAYS.get(self.period, 21)

    @property
    def is_calendar_week(self) -> bool:
        """True for either week period, whose boundaries are calendar dates.

        Kept as a property because the distinction it names is the one that
        matters: a week is Monday-to-Friday, not "the last seven bars". Both
        "this week" and "last week" are date ranges now - the older code only
        got the trailing edge right for the former.
        """
        return self.period in ("this_week", "last_week")

    @property
    def is_session_window(self) -> bool:
        """True when the window is a count of sessions, not a calendar range."""
        return self.session_count is not None


def _find_aggregation(text: str) -> str | None:
    """Which statistic the question asks for (high/low/open/close/change)."""
    for pattern, name in _AGGREGATIONS:
        if pattern.search(text):
            return name
    return None


def parse_period_query(text: str) -> PeriodQuery | None:
    """Detect a "high/low/close over a past window" question.

    Returns None when the message is not a period question, which keeps the
    live-quote path untouched for everything else.
    """
    if not text:
        return None
    # An explicit "last N trading days" wins over any week/month word nearby: it
    # states the window exactly, and it must be counted in sessions.
    explicit = _EXPLICIT_DAYS_RE.search(text)
    if explicit is not None:
        token = explicit.group(1).lower()
        count = int(token) if token.isdigit() else _NUMBER_WORDS.get(token)
        if count and 1 <= count <= 90:
            aggregation = _find_aggregation(text)
            if aggregation is not None:
                instruments = instruments_in(text)
                return PeriodQuery(
                    period=f"last_{count}_days",
                    aggregation=aggregation,
                    instrument=instruments[0] if instruments else None,
                    session_count=count,
                )

    period = None
    for pattern, name in _PERIOD_PATTERNS:
        if pattern.search(text):
            period = name
            break
    if period is None:
        return None

    aggregation = _find_aggregation(text)
    if aggregation is None:
        return None

    instruments = instruments_in(text)
    return PeriodQuery(period=period, aggregation=aggregation,
                       instrument=instruments[0] if instruments else None)


#: "What is the price of X", "X now?", "current X rate", "how much is X".
#: Deliberately about the *price itself*, never about analysis: "EURUSD
#: prediction" and "why did oil fall" must still reach a model, because no feed
#: can answer them and a canned number would be wrong about the question.
_QUOTE_INTENT_RES = (
    re.compile(
        r"\b(?:what(?:'s|\s+is)?|whats)\s+(?:the\s+)?"
        r"(?:current\s+|live\s+|today'?s?\s+|right\s+now\s+)?"
        r"(?:price|rate|quote|value|level|market\s+price)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:current|live|today'?s?|spot)\s+(?:price|rate|quote|level)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bhow\s+much\s+is\b", re.IGNORECASE),
    re.compile(
        r"\b(?:price|rate|quote|trading\s+at|at\s+what\s+price)\s+"
        r"(?:of|for|on)\b",
        re.IGNORECASE,
    ),
    # The bare form groups here actually use: "USOIL price?", "eurusd rate",
    # "US Tech 100 now?". Anchored to the end so "price action analysis" and
    # "price prediction" do not qualify - those need reasoning, not a quote.
    re.compile(
        r"\b(?:price|rate|quote|now|today)\s*[?!.]*\s*$",
        re.IGNORECASE,
    ),
)


def asks_for_quote(text: str) -> bool:
    """True when the question is *only* "what is the price right now".

    Used to answer from a single Exness read with no model call, which is what
    makes a price question as fast as the local arithmetic. The test is kept
    strict on purpose: a question that also asks *why* or *where next* needs
    reasoning, so it falls through to the normal path.
    """
    if not text or len(text) > 160:
        return False
    return any(pattern.search(text) for pattern in _QUOTE_INTENT_RES)


def format_quote_answer(instrument: str, quote: Quote) -> str:
    """The one-line answer a price question gets.

    Just the number: this is posted into a giveaway group, and "USOIL 71.42"
    is what a trader would type. A shut market says so, because Friday's close
    presented as the live price is a wrong answer, not a terse one.
    """
    price = quote.formatted()
    if quote.kind == "market_closed":
        return f"{instrument} {price} (market closed)"
    return f"{instrument} {price}"


#: Trading mechanics and definitions. These share words with a price question -
#: "what is the value of a pip in USOIL" reads as a price request but is a
#: definition, and answering "USOIL 71.42" to it would be confidently wrong.
#: They are excluded from the local quote path so they keep reaching a model,
#: which is the only thing that can explain them.
_MECHANICS_RE = re.compile(
    r"\b(?:pips?|pipettes?|points?\s+value|lots?|lot\s+size|mini\s+lot|"
    r"micro\s+lot|standard\s+lot|leverage|margin|spread|swap|commission|"
    r"contract\s+size|tick\s+size|tick\s+value|notional|risk|drawdown|"
    r"stop\s*loss|take\s*profit|position\s+size|what\s+does\s+\w+\s+mean)\b",
    re.IGNORECASE,
)


def asks_for_mechanics(text: str) -> bool:
    """True when the question is about *how trading works*, not a price.

    Used to keep such questions out of the local quote path: they mention an
    instrument and often the word "price" or "value", but the answer is an
    explanation no feed can supply.
    """
    return bool(text) and bool(_MECHANICS_RE.search(text))


def _decimals_for(instrument: str) -> int:
    if "JPY" in instrument:
        return 3
    # Crypto is quoted in dollars; five FX decimals on 66055.10 is nonsense.
    if instrument in _CRYPTO_SYMBOLS:
        return 2
    if instrument in ("XAUUSD", "XAGUSD"):
        return 2
    if instrument in ("DXY", "US10Y"):
        return 3
    # Oil is quoted in dollars to 2 dp on the platform, and so are the index
    # points - 5 dp would read as a tick chart rather than a price.
    if instrument in _INDEX_AND_COMMODITY_SYMBOLS:
        return 3 if instrument == "NATGAS" else 2
    return 5


def normalise_instrument(token: str) -> str | None:
    """Map a trader token (``EU``, ``GU``, ``eur/usd``, ``gold``) to an instrument."""
    cleaned = token.strip().upper().replace(" ", "")
    if cleaned in _BARE_MAP:
        return _BARE_MAP[cleaned]
    crypto = _CRYPTO_PAIR_RE.fullmatch(cleaned)
    if crypto:
        return _CRYPTO_NAMES[crypto.group(1).upper()]
    if cleaned in _SHORTHAND:
        return _SHORTHAND[cleaned]
    pair = _PAIR_RE.match(cleaned)
    if pair:
        base, quote = pair.groups()
        if base in _METALS:
            return f"{base}USD" if quote == "USD" else None
        if base in _PAIR_CURRENCIES and quote in _PAIR_CURRENCIES:
            return f"{base}{quote}"
    if cleaned in FEEDABLE_INSTRUMENTS:
        return cleaned
    # A broker synonym ("NAS100", "WTI", "SPX500") names the same instrument.
    from .exness import _ALIAS_TO_INSTRUMENT

    alias = _ALIAS_TO_INSTRUMENT.get(cleaned)
    if alias is not None:
        return alias
    return None


def instruments_in(text: str, *, limit: int = 4) -> list[str]:
    """Instruments a question is about, most specific first.

    Order matters: "is EURUSD correlated with DXY?" should fetch both, but a
    plain "GU bias" should fetch one. Capped so a paragraph cannot trigger ten
    HTTP requests before an answer.
    """
    if not text:
        return []
    found: list[str] = []
    upper = text.upper()

    for match in _INDEX_PHRASE_RE.finditer(text):
        phrase = match.group(1).lower().strip()
        instrument = _INDEX_PHRASES.get(phrase)
        if instrument is None:
            # The suffix letter was consumed by the regex, not by the phrase.
            instrument = _INDEX_PHRASES.get(phrase.rstrip("mcz"))
        if instrument is not None and instrument not in found:
            found.append(instrument)

    for match in _CRYPTO_PAIR_RE.finditer(text):
        instrument = _CRYPTO_NAMES.get(match.group(1).upper())
        if instrument and instrument not in found:
            found.append(instrument)

    for match in _PAIR_TOKEN_RE.finditer(upper):
        instrument = normalise_instrument(f"{match.group(1)}{match.group(2)}")
        if instrument and instrument not in found:
            found.append(instrument)

    for match in _NICKNAME_RE.finditer(upper):
        instrument = _SHORTHAND.get(match.group(1))
        if instrument and instrument not in found:
            found.append(instrument)

    for match in _BARE_RE.finditer(upper):
        instrument = _BARE_MAP.get(match.group(1))
        if instrument and instrument not in found:
            found.append(instrument)

    for name, instrument in _AMHARIC_INSTRUMENTS.items():
        if name in text and instrument not in found:
            found.append(instrument)

    return found[:limit]


class MarketFeed:
    """Fetches and caches Exness quotes. Safe to share across groups.

    The feed holds no credentials of its own. The owner's Exness settings live
    in the database, and ``settings_loader`` is a zero-argument awaitable that
    returns them - so a key saved from the dashboard takes effect on the next
    answer without a restart, and the keys never sit in a long-lived object.

    ``client`` is injectable for tests; the sandbox has no outbound TLS, so
    every market test drives a stubbed transport.
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        settings_loader: Callable[[], Awaitable[dict]] | None = None,
    ) -> None:
        self._client = client
        self._own_client = client is None
        self._settings_loader = settings_loader
        # Keyed by instrument list: a cache hit must never hand back a digest
        # for different instruments than the question asked about.
        self._cache: dict[str, tuple[float, MarketSnapshot]] = {}
        self._bar_cache: dict[str, tuple[float, list[tuple[datetime, float, float, float, float]]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # One client per credential set. Rebuilt when the owner changes keys;
        # the cached Access Point lives with it.
        self._exness: ExnessClient | None = None
        self._exness_fingerprint: tuple[str, str, str, str] | None = None
        #: Why the last fetch failed, for ``/diag``. Never shown in a group.
        self.last_error: str | None = None
        self.last_error_kind: str | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                # Exness discovery plus one candle read from a cold start is
                # two round trips; the per-instrument timeout below is the
                # real guard on an answer.
                timeout=httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0),
                headers={"User-Agent": "tg-qna-userbot/exness"},
                follow_redirects=False,
            )
        return self._client

    async def close(self) -> None:
        if self._exness is not None:
            # The Exness client shares ours when one was injected; only close
            # what this object owns.
            if self._own_client:
                await self._exness.aclose()
            self._exness = None
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- credentials ----------------------------------------------------
    async def _credentials(self):
        """Current owner settings, or raise ``ExnessCredentialsMissing``."""
        if self._settings_loader is None:
            raise ExnessCredentialsMissing("Exness settings are not wired up")
        return credentials_from(await self._settings_loader())

    async def exness(self) -> ExnessClient:
        """The signed client for the owner's *current* credentials."""
        credentials = await self._credentials()
        fingerprint = (
            credentials.api_key,
            credentials.private_key,
            credentials.account_id,
            credentials.gateway_url,
        )
        if self._exness is not None and self._exness_fingerprint != fingerprint:
            # Keys were rotated from the dashboard: drop the old host cache too.
            self._exness = None
        if self._exness is None:
            self._exness = ExnessClient(
                credentials=credentials, client=await self._get_client()
            )
            self._exness_fingerprint = fingerprint
        return self._exness

    async def configured(self) -> bool:
        """True when every Exness setting the API needs is present."""
        try:
            await self._credentials()
        except ExnessCredentialsMissing:
            return False
        return True

    async def snapshot(self, text: str) -> MarketSnapshot:
        """Best-effort snapshot for the instruments a question mentions."""
        instruments = instruments_in(text)
        if not instruments:
            return MarketSnapshot()

        key = ",".join(instruments)
        cached = self._cache.get(key)
        if cached is not None and (time.monotonic() - cached[0]) < CACHE_TTL_SECONDS:
            return cached[1]

        snapshot = await self._fetch_all(instruments)
        self._cache[key] = (time.monotonic(), snapshot)
        return snapshot

    async def live_quote_answer(self, text: str) -> str | None:
        """A bare price for a bare price question, with no model in the path.

        The local math solver answers arithmetic in under a millisecond and the
        group gets an exact number. A price question deserves the same: one
        Exness read, formatted here, posted as the answer. Asking a model to
        repeat a figure the bot already holds adds a round trip, a token budget
        and a chance to mistype the digits.

        Returns ``None`` - never a partial or invented answer - whenever the
        question is not a plain price question, names no instrument, or the
        feed could not supply a number. The caller then falls through to the
        normal path, which is exactly where such a question belonged anyway.
        """
        instruments = instruments_in(text)
        if len(instruments) != 1:
            # Two instruments in one sentence is a comparison ("EURUSD vs
            # DXY"), which is a sentence, not a number.
            return None
        instrument = instruments[0]

        snapshot = await self.snapshot(text)
        quote = snapshot.quotes[0] if snapshot.quotes else None
        if quote is None:
            return None

        return format_quote_answer(instrument, quote)

    async def _fetch_all(self, instruments: list[str]) -> MarketSnapshot:
        snapshot = MarketSnapshot()
        self.last_error = None
        self.last_error_kind = None

        async def one(instrument: str) -> Quote | None:
            try:
                return await asyncio.wait_for(
                    self._quote(instrument), timeout=FEED_TIMEOUT_SECONDS
                )
            except TimeoutError:
                logger.info("Market feed timeout for %s", instrument)
                await self._record_failure(RuntimeError("Exness timed out"))
            except Exception as exc:  # noqa: BLE001 - a feed must never break an answer
                logger.info("Market feed failed for %s: %s", instrument, exc)
                await self._record_failure(exc)
            return None

        results = await asyncio.gather(*(one(i) for i in instruments))
        for instrument, quote in zip(instruments, results, strict=True):
            if quote is None:
                snapshot.failed.append(instrument)
            else:
                snapshot.quotes.append(quote)
        return snapshot

    async def period_answer(self, query: PeriodQuery) -> PeriodAnswer | None:
        """Answer "high/low/close over a window" from a real OHLC series.

        The arithmetic is done here, not by the model: the highest price in a
        window is a fact about the series, and a model asked to eyeball a list
        of bars will eventually pick the wrong one. This is the same principle
        as the local math solver, applied to market history.
        """
        instrument = query.instrument
        if not instrument or instrument not in HISTORY_INSTRUMENTS:
            return None

        bars = await self.ohlc_bars(instrument, days=query.days)
        if not bars:
            return None

        now = datetime.now(UTC)
        today = now.date()
        # Which market this is decides whether weekend bars are sessions. This is
        # a per-family fact (crypto settles 24/7; forex, metals and indices are
        # shut), not a per-instrument guess, so it is asked once here.
        market = calendar.market_class_for(instrument)
        trades_weekends = market.trades_weekends

        # Identify today's still-forming bar by *date*, not position: a bar can
        # be dropped for a null field, and the newest bar is not always today
        # (a Saturday question's newest bar is Friday's).
        today_bar = next((b for b in reversed(bars) if b[0].date() == today), None)
        completed = [b for b in bars if b is not today_bar]

        def in_range(day: date, bounds: tuple[date, date]) -> bool:
            return bounds[0] <= day <= bounds[1]

        if query.session_count is not None:
            # "The last 5 trading days" counts *sessions*: for FX the weekend is
            # skipped, for crypto it is not, because both are what a trader
            # means by the phrase for that instrument.
            pool = [
                b for b in completed
                if _is_session(b[0], market=market)
            ]
            if trades_weekends and today_bar is not None:
                pool.append(today_bar)
            selected = pool[-query.session_count :]
        elif (bounds := _date_range(query.period, now)) is not None:
            selected = [b for b in bars if in_range(b[0].date(), bounds)]
            # Weekend bars inside a Monday-Friday window are not sessions for FX;
            # they carry Friday's close and would silently widen the window.
            if not trades_weekends:
                selected = [
                    b for b in selected
                    if b is today_bar or _is_session(b[0], market=market)
                ]
            # A still-forming bar may bound "today" and "this week so far", but
            # it must never set "the high last week": it is not finished, and the
            # number it shows now will not be the one the host sees at close.
            if query.period not in ("today", "this_week"):
                selected = [b for b in selected if b is not today_bar]
            if not selected:
                # A Monday "yesterday", or a new week before any session printed.
                # Answer with the most recent completed session rather than
                # dropping the question - the only case outside the named window.
                fallback = [
                    b for b in completed
                    if _is_session(b[0], market=market)
                ]
                selected = fallback[-1:]
                if not selected:
                    return None
                # Flagged rather than returned silently: the answer is truthful
                # but it is outside the window the question named.
                fallback_used = True
            else:
                fallback_used = False
        else:  # pragma: no cover - every period has a range or a count
            return None

        # A weekend question about "this week" would otherwise be answered from
        # whichever single bar the range happened to contain. Trading never
        # happened in that window, so the honest answer is no data, not a
        # one-candle high dressed up as the week's.
        if query.period == "this_week" and not fallback_used and not trades_weekends:
            sessions = [b for b in selected if _is_session(b[0], market=market)]
            if not sessions:
                return None

        if not selected:
            return None

        highest = max(b[1] for b in selected)
        lowest = min(b[2] for b in selected)
        first_open = selected[0][3]
        last_close = selected[-1][4]
        value = {
            "high": highest,
            "low": lowest,
            "open": first_open,
            "close": last_close,
            "change": (last_close - first_open) / first_open * 100 if first_open else 0.0,
        }[query.aggregation]

        return PeriodAnswer(
            instrument=instrument,
            period=query.period,
            aggregation=query.aggregation,
            value=value,
            high=highest,
            low=lowest,
            open=first_open,
            close=last_close,
            start=selected[0][0],
            end=selected[-1][0],
            decimals=_decimals_for(instrument),
        )

    async def ohlc_bars(
        self, instrument: str, *, days: int = 7
    ) -> list[tuple[datetime, float, float, float, float]]:
        """Daily (timestamp, high, low, open, close) bars straight from Exness.

        Shared by the period-answer path and by the photo path: a chart photo
        showing BTC against *this week's* series needs the same numbers, and
        computing "the high this week" from a real series beats asking a model
        to read an axis.

        Bars are cached briefly per instrument: a giveaway can fire three
        period questions in a row, and each one asking Exness for the same two
        months of daily candles would be both slow and rate-limit hungry.
        """
        if instrument not in HISTORY_INSTRUMENTS:
            return []

        # Keyed by the requested depth as well as the instrument: a 90-session
        # question must never be served the 16-day series fetched for a
        # five-session one.
        cache_key = f"{instrument}:{days}"
        cached = self._bar_cache.get(cache_key)
        if cached is not None and (time.monotonic() - cached[0]) < CACHE_TTL_SECONDS:
            return cached[1]

        try:
            client = await self.exness()
            try:
                available = await client.available_instruments()
            except ExnessError:
                available = set()
            candles = await client.candles(
                instrument,
                timeframe=HISTORY_TIMEFRAME,
                count=max(days, 10) + 2,
                instruments=available or None,
                # Date each daily bar by its trading day, not the UTC clock: the
                # session opens at local midnight, which is the previous evening
                # in UTC, and would otherwise be filed a day early and dropped
                # as a weekend.
                align_daily_to_utc_midnight=True,
            )
        except Exception as exc:  # noqa: BLE001 - a feed must never break an answer
            logger.warning("OHLC fetch failed for %s: %s", instrument, exc)
            await self._record_failure(exc)
            return []

        bars = [
            (bar.time, bar.high, bar.low, bar.open, bar.close)
            for bar in candles
            # A bar Exness has not filled yet comes back all-zero; keeping it
            # would make "the high last week" report 0 as the low.
            if bar.high > 0 and bar.low > 0
        ]
        self._bar_cache[cache_key] = (time.monotonic(), bars)
        return bars

    async def _quote(self, instrument: str) -> Quote | None:
        """Fetch one instrument from Exness, or ``None``.

        There is no failover chain any more. The old code fell through Yahoo ->
        gold-api -> ECB, which meant a Forex group could be quoted a central
        bank's daily fix while the prompt implied a live tick. One broker, one
        number; a failure stays a failure so the answer can say so.
        """
        client = await self.exness()
        # The account's own instrument list decides the symbol (and its suffix),
        # so a mini account quoting EURUSDm still works.
        available: set[str] = set()
        try:
            available = await client.available_instruments()
        except ExnessError as exc:
            logger.info("Exness instrument list unavailable (%s); trying bare symbols", exc)

        bars = await client.candles(
            instrument,
            timeframe=QUOTE_TIMEFRAME,
            count=QUOTE_CANDLE_COUNT,
            instruments=available or None,
        )
        if not bars:
            return None

        # The newest bar is the price. A bar whose OHLC are all zero is the
        # placeholder Exness returns for an untouched period; walk back to the
        # last real one rather than quoting 0.00000.
        last = next((bar for bar in reversed(bars) if bar.close > 0), None)
        if last is None:
            return None

        now = datetime.now(UTC)
        age_minutes = (now - last.time).total_seconds() / 60
        # Whether the market is *open* is a calendar question, not an age
        # question. Asking only about age marked Friday's close as live for the
        # first few hours of Saturday, because the last tick was still recent
        # enough - a shut market quoted as current, on the weekend, which is when
        # these questions get asked. Age then decides only whether the number is
        # still fresh enough to repeat while the market *is* open.
        market = calendar.market_class_for(instrument)
        shut_for_weekend = trading_sessions.is_closed_for_weekend(market, now)
        fresh = age_minutes <= STALE_AFTER_MINUTES
        return Quote(
            instrument=instrument,
            price=last.close,
            # Same number, different claim: a shut market's close is never the
            # current price.
            kind="live" if (fresh and not shut_for_weekend) else "market_closed",
            source="exness",
            as_of=last.time,
            decimals=_decimals_for(instrument),
            note="bid",
            symbol=client.symbol_for(instrument, available or None),
        )

    async def _record_failure(self, exc: Exception) -> None:
        """Remember why the last fetch failed, for ``/diag`` only.

        Never surfaced in a group answer: the group is told there is no live
        data, not that a signature was rejected.
        """
        self.last_error = str(exc) or type(exc).__name__
        self.last_error_kind = (
            "not_configured" if isinstance(exc, ExnessCredentialsMissing) else "error"
        )
