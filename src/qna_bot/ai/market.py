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
from datetime import UTC, datetime, timedelta

import httpx

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

FEEDABLE_INSTRUMENTS: frozenset[str] = frozenset(
    {
        *_CRYPTO_SYMBOLS,
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
    (re.compile(r"\b(last|previous|past)\s+(?:7|seven)\s+days?\b", re.I), "last_week"),
    (re.compile(r"\b(last|previous|past)\s+(?:30|thirty)\s+days?\b", re.I), "last_month"),
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
        if base in _PAIR_CURRENCIES and quote in _PAIR_CURRENCIES:
            return f"{base}{quote}"
    if cleaned in FEEDABLE_INSTRUMENTS:
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

        cached = self._bar_cache.get(instrument)
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
        self._bar_cache[instrument] = (time.monotonic(), bars)
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

        age_minutes = (datetime.now(UTC) - last.time).total_seconds() / 60
        return Quote(
            instrument=instrument,
            price=last.close,
            # Past the stale window the market is shut and this is Friday's
            # close, not a tick. Same number, different claim.
            kind="live" if age_minutes <= STALE_AFTER_MINUTES else "market_closed",
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
