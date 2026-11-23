"""Live market data for Forex groups.

A language model has no market feed. Until now a Forex-group answer could only
refuse live questions ("No live data"), which is honest but useless when the
whole point of the group is "EURUSD now?".

This module supplies the *numbers*, and the model supplies the *explanation*.
The split matters: model parameters carry a price only up to their training
cutoff, so any figure the model produces from memory is stale by definition.
Every number here is fetched at answer time and carries the timestamp of the
quote, and the prompt is told to use these figures only.

Sources (all keyless, all free):

``Yahoo Finance chart API``
    Primary. Covers FX pairs, gold/silver futures, the dollar index and the US
    10-year yield, with intraday freshness.
``gold-api.com``
    Keyless gold spot, used when Yahoo does not cover the instrument.
``Frankfurter (ECB)``
    Official ECB reference rates, one fix per working day. Used when the live
    sources fail; it is a *daily* fix, never quote it as an intraday price.

Freshness is the whole product here. A trader who is told "EURUSD 1.0984" when
the last tick was three days old is worse off than one who is told nothing, so
every digest records when each figure was taken and what it is (live quote vs.
official daily fix), and says so in the prompt.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

logger = logging.getLogger(__name__)

#: A quote older than this is not "now". FX trades ~24/5, so a weekend gap is
#: normal and must be labelled as such rather than hidden.
STALE_AFTER_MINUTES = 20
#: Daily ECB fixes are only ever as fresh as the last working day.
DAILY_FIX_MAX_AGE_HOURS = 96
#: Whole-digest cache. A giveaway fires several questions within seconds of
#: each other; re-fetching for each one would add latency and burn rate limits.
CACHE_TTL_SECONDS = 30.0
#: Never let a slow feed delay an answer more than this.
FEED_TIMEOUT_SECONDS = 4.0


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
_CRYPTO_PAIR_RE = re.compile(
    r"\b(" + "|".join(_CRYPTO_NAMES) + r")\s*(?:/|-)?\s*(?:usdt|usd|busd)\b",
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
_INDEX_INSTRUMENTS = {
    "DXY": ("DX-Y.NYB", "DXY", "US Dollar Index"),
    "US10Y": ("^TNX", "US10Y", "US 10-year yield (percent)"),
}
#: Everything the feed can quote. Gold futures are the liquidity reference.
_YAHOO_SYMBOLS: dict[str, tuple[str, str, str]] = {
    "XAUUSD": ("GC=F", "XAUUSD", "gold futures (front month)"),
    "XAGUSD": ("SI=F", "XAGUSD", "silver futures (front month)"),
    **_INDEX_INSTRUMENTS,
}

#: Crypto instruments and their Yahoo tickers. These are also the instruments
#: eligible for period questions ("what was the high last week").
_CRYPTO_SYMBOLS: dict[str, str] = {
    "BTCUSD": "BTC-USD",
    "ETHUSD": "ETH-USD",
    "SOLUSD": "SOL-USD",
    "XRPUSD": "XRP-USD",
    "DOGEUSD": "DOGE-USD",
    "BNBUSD": "BNB-USD",
    "LTCUSD": "LTC-USD",
}

#: Instruments whose OHLC history is fetched from the same free chart endpoint.
#: Metals and indices are included because "last week's high" is just as fair a
#: question about gold as about Bitcoin.
HISTORY_INSTRUMENTS: dict[str, str] = {
    **_CRYPTO_SYMBOLS,
    "XAUUSD": "GC=F",
    "XAGUSD": "SI=F",
    "DXY": "DX-Y.NYB",
}

_PAIR_RE = re.compile(r"^([A-Z]{3})/?([A-Z]{3})$")
#: Bare metals and the yield/index tokens a trader types on their own.
_BARE_RE = re.compile(
    r"\b(XAU|XAG|GOLD|SILVER|DXY|US10Y|BTC|ETH|SOL|XRP|DOGE|BNB|LTC|"
    r"BITCOIN|ETHEREUM|SOLANA|RIPPLE|DOGECOIN|LITECOIN)\b"
)
_NICKNAME_RE = re.compile(
    r"\b(" + "|".join(sorted(_SHORTHAND, key=len, reverse=True)) + r")\b"
)
_PAIR_TOKEN_RE = re.compile(r"\b([A-Z]{3})\s*[/\-]?\s*([A-Z]{3})\b")

#: Currencies Frankfurter publishes (ECB reference set).
_ECB_CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "CNY", "SEK",
    "NOK", "DKK", "HKD", "INR", "MXN", "ZAR", "TRY", "SGD", "PLN", "CZK",
    "HUF", "RON", "BRL", "ILS", "KRW", "PHP", "THB", "MYR", "IDR", "ISK",
}


@dataclass
class Quote:
    """One fetched figure, with everything needed to describe it honestly."""

    instrument: str
    price: float
    #: "live" (intraday quote), "daily_fix" (official once-a-day reference) or
    #: "futures" (a dated contract, not spot).
    kind: str
    source: str
    as_of: datetime
    change_percent: float | None = None
    #: Raw digits a trader would quote. JPY pairs need 4 dp, metals 2, FX 5.
    decimals: int = 5
    note: str = ""

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
            if self.kind == "daily_fix":
                return f"{self.instrument} {self.formatted()} (official ECB daily fix, {self.as_of:%Y-%m-%d})"
            if self.age_minutes > STALE_AFTER_MINUTES:
                return f"{self.instrument} {self.formatted()} (last tick {stamp}, market closed)"
            return f"{self.instrument} {self.formatted()} ({stamp})"
        # A closed market still needs saying: without it the model would present
        # Friday's close as the live price. A bare state tag does that without
        # exposing a clock.
        if self.kind == "daily_fix":
            return f"{self.instrument} {self.formatted()} (daily fix)"
        if self.age_minutes > STALE_AFTER_MINUTES:
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
        if any(q.kind == "daily_fix" for q in self.quotes):
            caveats.append(
                "Figures marked 'daily fix' are a once-a-day reference rate, not an "
                "intraday price; never call them the current price."
            )
        if any(
            q.kind != "daily_fix" and q.age_minutes > STALE_AFTER_MINUTES for q in self.quotes
        ):
            caveats.append(
                "Figures marked 'market closed' are the last available tick. Say the "
                "market is closed; never present them as live."
            )
        if any(q.kind == "futures" for q in self.quotes):
            caveats.append(
                "XAUUSD here is the front-month COMEX future, which trades a little "
                "above spot gold; call it gold futures, not spot."
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
    (re.compile(r"\b(last|previous|past)\s+(?:7|seven)\s+days?\b", re.I), "last_week"),
    (re.compile(r"\b(last|previous|past)\s+(?:30|thirty)\s+days?\b", re.I), "last_month"),
)

#: What to read off the series.
_AGGREGATIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(highest|high|top|peak|maximum|max)\b", re.I), "high"),
    (re.compile(r"\b(lowest|low|bottom|minimum|min)\b", re.I), "low"),
    (re.compile(r"\b(open(?:ing)?|opened)\b", re.I), "open"),
    (re.compile(r"\b(close|closed|closing|end(?:ed)?)\b", re.I), "close"),
    (re.compile(r"\b(change|move|performance|return|gain|loss|up|down|%)\b", re.I), "change"),
)

#: How many days back each period starts. Crypto trades daily, so calendar days
#: are the right unit; an FX "last week" skips the weekend automatically because
#: Yahoo simply has no bars for it.
_PERIOD_DAYS = {
    "today": 1,
    "yesterday": 2,
    "this_week": 7,
    "last_week": 14,
    "this_month": 31,
    "last_month": 62,
}


@dataclass
class PeriodQuery:
    """A question about a past window, answerable from an OHLC series."""

    period: str
    aggregation: str
    instrument: str | None = None

    @property
    def days(self) -> int:
        return _PERIOD_DAYS.get(self.period, 7)

    def window_days(self) -> int:
        """How many of the most recent bars belong to the window itself.

        "last week" means the previous 7 days excluding today; "this week"
        includes today, so the arithmetic lives here rather than in the caller.
        """
        if self.period in ("today", "yesterday"):
            return 1
        return 7 if "week" in self.period else 30

    @property
    def is_calendar_week(self) -> bool:
        """True for "this week", which starts on Monday - not 7 days ago.

        A giveaway host asking "the highest price this week" on a Tuesday means
        Monday and Tuesday, not the last seven days. Trailing-seven was close
        enough to look right and wrong often enough to matter, which is the
        worst combination for a competition answer.
        """
        return self.period == "this_week"


def parse_period_query(text: str) -> PeriodQuery | None:
    """Detect a "high/low/close over a past window" question.

    Returns None when the message is not a period question, which keeps the
    live-quote path untouched for everything else.
    """
    if not text:
        return None
    period = None
    for pattern, name in _PERIOD_PATTERNS:
        if pattern.search(text):
            period = name
            break
    if period is None:
        return None

    aggregation = None
    for pattern, name in _AGGREGATIONS:
        if pattern.search(text):
            aggregation = name
            break
    if aggregation is None:
        return None

    instruments = instruments_in(text)
    return PeriodQuery(period=period, aggregation=aggregation,
                       instrument=instruments[0] if instruments else None)


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
        if base in _ECB_CURRENCIES and quote in _ECB_CURRENCIES:
            return f"{base}{quote}"
    if cleaned in _YAHOO_SYMBOLS:
        return cleaned
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

    return found[:limit]


class MarketFeed:
    """Fetches and caches quotes. Safe to share across groups."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._own_client = client is None
        # Keyed by instrument list: a cache hit must never hand back a digest
        # for different instruments than the question asked about.
        self._cache: dict[str, tuple[float, MarketSnapshot]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=2.0),
                headers={"User-Agent": "Mozilla/5.0 (compatible; tg-qna-userbot)"},
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None

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

    async def _fetch_all(self, instruments: list[str]) -> MarketSnapshot:
        snapshot = MarketSnapshot()

        async def one(instrument: str) -> Quote | None:
            try:
                return await asyncio.wait_for(
                    self._quote(instrument), timeout=FEED_TIMEOUT_SECONDS
                )
            except TimeoutError:
                logger.info("Market feed timeout for %s", instrument)
            except Exception as exc:  # noqa: BLE001 - a feed must never break an answer
                logger.info("Market feed failed for %s: %s", instrument, exc)
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


        # Drop today's still-forming bar from the *completed* pool: an
        # incomplete candle makes "highest price last week" wrong the moment it
        # is posted. Today's own bar is still what "today"/"this week" ask for.
        # Identify today's bar by date, and fall back to the newest bar when the
        # forming candle carried no values at all (a Sunday, say). Position alone
        # is unreliable the moment a bar is dropped for a null field.
        now = datetime.now(UTC)
        today_bar = next((b for b in reversed(bars) if b[0].date() == now.date()), None)
        if today_bar is None and bars:
            today_bar = bars[-1]
        completed = [b for b in bars if b is not today_bar]
        pool = completed or bars

        window = query.window_days()
        if query.period == "today":
            selected = [today_bar] if today_bar else pool[-1:]
        elif query.period == "yesterday":
            selected = pool[-1:]
        elif query.is_calendar_week:
            # "This week" is Monday-to-now, not a trailing seven days. On a
            # Tuesday the difference is the whole weekend before it.
            monday = (now - timedelta(days=now.weekday())).date()
            selected = [b for b in [*pool, *( [today_bar] if today_bar else [])]
                        if b[0].date() >= monday]
            if not selected:
                # A brand-new week with no bars yet (or a market that has not
                # opened): fall back to the most recent session rather than
                # returning nothing and losing the question.
                selected = pool[-1:]
        elif query.period == "this_month":
            # Trailing window ending now, today's partial bar included.
            selected = pool[-window:]
            if today_bar is not None:
                selected = [*selected, today_bar]
        else:
            # "last week" / "last month": the completed window before today.
            selected = pool[-window:]
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
        """Daily (timestamp, high, low, open, close) bars straight from Yahoo.

        Shared by the period-answer path and by the photo path: a chart photo
        showing BTC against *this week's* series needs the same numbers, and
        computing "the high this week" from a real series beats asking a model
        to read an axis.
        """
        ticker = HISTORY_INSTRUMENTS.get(instrument)
        if ticker is None:
            return []
        client = await self._get_client()
        try:
            resp = await client.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                params={"interval": "1d", "range": f"{max(days, 5)}d"},
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("OHLC fetch failed for %s: %s", instrument, exc)
            return []

        result = ((resp.json().get("chart") or {}).get("result") or [None])[0]
        if not result:
            return []

        timestamps = result.get("timestamp") or []
        quotes = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        highs = quotes.get("high") or []
        lows = quotes.get("low") or []
        opens = quotes.get("open") or []
        closes = quotes.get("close") or []

        def at(series: list, idx: int) -> float | None:
            return series[idx] if idx < len(series) else None

        # Pair each day with its bar, dropping the null bars Yahoo emits when a
        # market was shut (weekends, holidays). A bar is usable when the fields
        # the question needs are present: Yahoo routinely leaves one field null
        # on a still-forming candle, and dropping the whole bar for that would
        # silently shorten the window - which is how "the high last week" comes
        # back as the week before.
        bars: list[tuple[datetime, float, float, float, float]] = []
        for idx, stamp in enumerate(timestamps):
            h = at(highs, idx)
            low = at(lows, idx)
            o = at(opens, idx)
            c = at(closes, idx)
            if h is None or low is None:
                continue
            bars.append(
                (
                    datetime.fromtimestamp(int(stamp), tz=UTC),
                    float(h),
                    float(low),
                    float(o) if o is not None else float(c if c is not None else h),
                    float(c) if c is not None else float(o if o is not None else low),
                )
            )
        return bars

    async def _quote(self, instrument: str) -> Quote | None:
        """Try sources in order; one failing must not prevent the next."""
        for source in self._sources(instrument):
            try:
                quote = await source()
            except Exception as exc:  # noqa: BLE001 - fall through to the next source
                logger.info("Market source failed for %s: %s", instrument, exc)
                continue
            if quote is not None:
                return quote
        return None

    def _sources(self, instrument: str):
        """Yield candidate fetchers, best freshness first."""
        if instrument in _YAHOO_SYMBOLS:
            yield lambda: self._yahoo(*_YAHOO_SYMBOLS[instrument], instrument)
        if instrument in ("XAUUSD", "XAGUSD"):
            yield lambda: self._gold_api(instrument)
        if instrument == "DXY":
            yield lambda: self._yahoo(*_INDEX_INSTRUMENTS["DXY"], "DXY")
        if _PAIR_RE.match(instrument):
            symbol = f"{instrument[:3]}{instrument[3:]}=X"
            yield lambda symbol=symbol: self._yahoo(symbol, instrument, "live quote", instrument)
            yield lambda: self._ecb_fix(instrument, instrument[:3], instrument[3:])  # noqa: B023

    # -- sources ---------------------------------------------------------
    async def _yahoo(self, symbol: str, name: str, note: str, instrument: str) -> Quote | None:
        client = await self._get_client()
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        resp = await client.get(url, params={"interval": "1d", "range": "1d"})
        resp.raise_for_status()
        payload = resp.json()
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            return None
        meta = result[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None:
            return None
        stamp = meta.get("regularMarketTime")
        as_of = (
            datetime.fromtimestamp(int(stamp), tz=UTC)
            if stamp
            else datetime.now(UTC)
        )
        kind = "futures" if note.startswith(("gold", "silver")) else "live"
        return Quote(
            instrument=name,
            price=float(price),
            kind=kind,
            source="yahoo",
            as_of=as_of,
            change_percent=meta.get("regularMarketChangePercent"),
            decimals=_decimals_for(instrument),
            note=note,
        )

    async def _gold_api(self, instrument: str) -> Quote | None:
        client = await self._get_client()
        symbol = "XAU" if instrument == "XAUUSD" else "XAG"
        resp = await client.get(f"https://api.gold-api.com/price/{symbol}")
        resp.raise_for_status()
        payload = resp.json()
        price = payload.get("price")
        if price is None:
            return None
        as_of = datetime.now(UTC)
        raw_stamp = payload.get("updatedAt")
        if raw_stamp:
            try:
                as_of = datetime.fromisoformat(str(raw_stamp).replace("Z", "+00:00"))
            except ValueError:
                pass
        return Quote(
            instrument=instrument,
            price=float(price),
            kind="live",
            source="gold-api",
            as_of=as_of,
            decimals=2,
            note="spot",
        )

    async def _ecb_fix(self, instrument: str, base: str, quote_ccy: str) -> Quote | None:
        if base not in _ECB_CURRENCIES or quote_ccy not in _ECB_CURRENCIES:
            return None
        client = await self._get_client()
        resp = await client.get(
            "https://api.frankfurter.dev/v1/latest",
            params={"base": base, "symbols": quote_ccy},
        )
        resp.raise_for_status()
        payload = resp.json()
        rate = (payload.get("rates") or {}).get(quote_ccy)
        if rate is None:
            return None
        date_str = payload.get("date")
        try:
            as_of = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
        except (TypeError, ValueError):
            as_of = datetime.now(UTC)
        return Quote(
            instrument=instrument,
            price=float(rate),
            kind="daily_fix",
            source="frankfurter/ecb",
            as_of=as_of,
            decimals=_decimals_for(instrument),
            note="official ECB daily reference rate",
        )
