"""Forex / trading domain support.

Two distinct jobs, and it matters that they stay distinct:

1. **Recognition.** Forex groups ask short, bare questions - "EURUSD?", "GU
   bias", "XAU support" - with no question word and no `?`. Without specific
   vocabulary those look like casual chat and get filtered out.

2. **Specialist prompting.** Answers must use trading conventions (FX pips, not
   "points"; pairs written the way traders write them) and must not invent
   prices, levels, or today's news. A model has no live feed. An answer that
   confidently recites a fake EURUSD level is worse than no answer, because a
   giveaway host marks it wrong and the number is baked into the bot's reply
   history.

So the guidance here is deliberately split: definitions, mechanics and
economics are answerable and worth answering; live prices, today's news and
future direction are not, and the bot must say so instead of guessing.
"""

from __future__ import annotations

import re

#: Pairs in trader shorthand, plus the majors and crosses by name.
_CURRENCIES = (
    "usd", "eur", "gbp", "jpy", "chf", "aud", "nzd", "cad",
    "cny", "sek", "nok", "dkk", "mxn", "zar", "try", "inr", "sgd", "hkd",
)

#: Concepts a Forex giveaway actually asks about.
_CONCEPTS = (
    "pip", "pips", "leverage", "lot", "lots", "micro lot", "mini lot",
    "spread", "swap", "slippage", "margin", "equity", "drawdown", "lot size",
    "stop loss", "take profit", "breakeven", "risk reward", "risk-to-reward",
    "rr", "bullish", "bearish", "support", "resistance", "trend", "range",
    "breakout", "reversal", "pullback", "retracement", "fibonacci", "fib",
    "candlestick", "doji", "hammer", "engulfing", "head and shoulders",
    "moving average", "ema", "sma", "rsi", "macd", "bollinger", "atr",
    "ichimoku", "scalping", "swing trading", "day trading", "position trading",
    "cross pair", "base currency", "quote currency", "exotic pair", "major pair",
    "minor pair", "bull flag", "bear flag", "pennant", "wedge", "triangle",
    "golden cross", "death cross", "double top", "double bottom",
    "triple top", "triple bottom", "cup and handle", "channel", "trendline",
    "pivot", "pivot point", "round number", "psychological level",
    "divergence", "overbought", "oversold", "confluence", "fakeout", "whipsaw",
    "retest", "rejection", "wick", "body", "engulfing candle", "inside bar",
    "pin bar", "spinning top", "marubozu", "momentum", "impulse",
    "stop hunt", "trailing stop", "risk management", "position size", "leverage ratio",
    "margin call", "free margin", "used margin", "equity curve", "win rate",
    "profit factor", "expectancy", "spread cost", "commission", "raw spread",
    "standard account", "cent account", "swap long", "swap short", "rollover",
    "triple swap", "weekend gap", "economic calendar", "red news",
    "high impact news", "central bank meeting", "interest rate decision",
    "press conference", "fomc minutes", "dot plot", "tapering", "rate pause",
    "basis point", "bond yield", "real yield", "inflation expectation",
    "purchasing power", "current account", "trade balance", "balance of payments",
    "gross domestic product", "unemployment rate", "retail sales",
    "manufacturing", "services", "consumer confidence", "jobless claims",
    "average earnings", "wage growth", "risk-on", "risk-off", "flight to safety",
    "liquidity sweep", "order flow", "market maker", "retail trader",
    "institutional", "speculator", "hedger", "long position", "short position",
    "net long", "net short", "open interest", "spot price", "bid", "ask",
    "bid-ask", "mid price", "quote price", "tick", "tick size", "point value",
    "contract size", "notional", "pipette", "fractional pip",
    "most traded", "most liquid", "traded pair", "currency pair",
    "hedging", "correlation", "volatility", "liquidity", "order block",
    "supply and demand", "session", "london session", "new york session",
    "tokyo session", "forex", "fx", "cfd", "broker", "brokerage", "swap rate",
    "carry trade", "interest rate", "central bank", "fed", "ecb", "boj",
    "fomc", "non-farm", "nfp", "cpi", "gdp", "pmi", "rate hike", "rate cut",
    "hawkish", "dovish", "quantitative easing", "inflation", "deflation",
    "recession", "safe haven", "gold", "xauusd", "silver", "xagusd",
    "oil", "wti", "brent", "dxy", "dollar index", "cable", "fiber", "loonie",
    "btc", "bitcoin", "eth", "ethereum", "sol", "solana", "xrp", "ripple",
    "doge", "dogecoin", "bnb", "ltc", "litecoin", "crypto", "btcusd", "btcusdt",
    "ethusdt", "coin", "altcoin", "chart", "candle", "ohlc",
    "aussie", "kiwi", "swissy", "yen", "euro", "pound", "greenback",
)

#: Forex terms written in Ethiopic script. Amharic-speaking traders spell the
#: English term phonetically - ጎልድ is "gold", ሌቨሬጅ is "leverage" - and these
#: questions are otherwise invisible to an English keyword list.
_AMHARIC_FOREX_TERMS = (
    "ጎልድ", "ሶስት", "ብር", "ዶላር", "ዩሮ", "ፓውንድ", "የን",
    "ፒፕ", "ፒፕስ", "ሎት", "ሌቨሬጅ", "ስፕሬድ", "ማርጅን", "ስቶፕ ሎስ",
    "ቴክ ፕሮፊት", "ስዋፕ", "ሴሽን", "ትሬንድ", "ሱፖርት", "ሪሲስታንስ",
    "ቻርት", "ካንድል", "ብረት", "ሴንትራል ባንክ", "ኢንፍሌሽን",
    "ኤፍኤክስ", "ፎሬክስ", "ሚኒ ሎት", "ስታንዳርድ ሎት", "ሚክሮ ሎት",
)

#: Explicit pair mentions: EURUSD, EUR/USD, EUR-USD.
_PAIR_RE = re.compile(
    r"\b(?:" + "|".join(_CURRENCIES) + r")\s*[/\-]?\s*"
    r"(?:" + "|".join(_CURRENCIES) + r")\b",
    re.IGNORECASE,
)

#: Metals and indices traders quote as if they were pairs.
_METAL_RE = re.compile(
    r"\b(?:xau\s*/?\s*usd|xag\s*/?\s*usd|xauusd|xagusd|spot\s+gold|spot\s+silver|"
    r"us30|nas100|spx500|ger40|uk100)\b",
    re.IGNORECASE,
)

#: Shorthand nicknames that carry no question word.
_NICKNAMES = (
    "cable", "fiber", "loonie", "aussie", "kiwi", "swissy", "gu", "eu", "uj",
    "ej", "gj", "chfjpy", "eurgbp", "gbpjpy", "eurchf", "audjpy",
)

_CONCEPT_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(c) for c in sorted(_CONCEPTS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

_NICKNAME_RE = re.compile(r"\b(?:" + "|".join(_NICKNAMES) + r")\b", re.IGNORECASE)

#: Ethiopic terms are matched without \b: the word-boundary classes are tuned
#: for Latin script and would not fire reliably around Ethiopic letters.
_AMHARIC_TERM_RE = re.compile("|".join(_AMHARIC_FOREX_TERMS))

#: A question that asks for live/current/forward-looking information. These are
#: unanswerable without a data feed and must not be guessed.
LIVE_DATA_RE = re.compile(
    r"\b(?:today|tonight|now|current(?:ly)?|right\s+now|live|this\s+(?:week|month)|"
    r"tomorrow|next\s+(?:week|month)|forecast|prediction|predict|will\s+\w+\s+"
    r"(?:go|rise|fall)|where\s+is|what(?:'s|\s+is)\s+the\s+(?:price|rate|level)|"
    r"price\s+of|how\s+much\s+is\s+\w+\s+(?:today|now)|market\s+(?:update|outlook|"
    r"open|close)|daily|news)\b",
    re.IGNORECASE,
)

#: The honest refusal, used when a question needs a live feed.
NO_LIVE_DATA_ANSWER = "No live data"
NO_LIVE_DATA_GUIDANCE = (
    "You do NOT have a live market feed. If the question asks for a current "
    "price, today's level, this week's outlook, a forecast, or market news, do "
    "NOT invent numbers or recall an old price as if it were current. Reply "
    "with exactly: " + NO_LIVE_DATA_ANSWER + "\n"
    "Definitional, mechanical and economic questions (what a pip is, how "
    "leverage works, what drives a currency, how a pattern is interpreted) you "
    "must answer normally and precisely."
)
#: Swapped in when the feed did return figures. Without this the two blocks
#: contradict each other - one hands over the price, the other forbids quoting
#: one - and the model resolves the clash by refusing anyway.
LIVE_DATA_PRESENT_GUIDANCE = (
    "The prices in the LIVE MARKET DATA block are current and are the ONLY "
    "prices you may state. Use them to answer the price question directly: give "
    "the figure on its own. Do NOT volunteer a date, a time, an 'as of', a source "
    "name, or any note about where the number came from - a bare price is what a "
    "trader would say, and anything more announces that a machine fetched it. "
    "For direction/trend questions, reason from the change and levels actually "
    "present in that block - never from memory.\n"
    "Do NOT use the phrase '" + NO_LIVE_DATA_ANSWER + "' when the block covers "
    "the instrument asked about. Only refuse if the instrument is absent from "
    "the block, and then say exactly: " + NO_LIVE_DATA_ANSWER + "\n"
    "Never invent a level, forecast, target, event result or news item that is "
    "not in that block."
)


def is_forex_question(text: str) -> bool:
    """True when this looks like a Forex/trading question.

    Covers the bare forms real groups use: ``EURUSD?``, ``GU bias``,
    ``XAU support``, ``what is a pip``.
    """
    if not text:
        return False
    return bool(
        _PAIR_RE.search(text)
        or _METAL_RE.search(text)
        or _CONCEPT_RE.search(text)
        or _NICKNAME_RE.search(text)
        or _AMHARIC_TERM_RE.search(text)
    )


def asks_for_live_data(text: str) -> bool:
    """True when the question needs a price feed the bot does not have."""
    if not text:
        return False
    return bool(LIVE_DATA_RE.search(text))


def prompt_guidance(text: str) -> str:
    """Extra system-prompt guidance for a Forex question ('' when not Forex)."""
    if not is_forex_question(text):
        return ""
    return FOREX_GUIDANCE


#: A photograph of a chart carries the numbers the question is about, so the
#: "no feed" rule must not fire on one. Without this a chart photo was answered
#: "No live data" while the price sat in plain sight on the image.
CHART_IMAGE_GUIDANCE = (
    "The message includes an image: a trading chart, a position screenshot or a "
    "broker panel. Read the values out of the image itself - price scale, last "
    "price, high/low markers, candle highs and lows, indicator values, symbol, "
    "timeframe - and answer from those. Those numbers ARE the data for this "
    "question: never reply '" + NO_LIVE_DATA_ANSWER + "' when the answer is "
    "visible in the picture.\n"
    "Read the current price from the price axis against the latest candle, not "
    "from an unrelated label elsewhere on the screen. Name the instrument and "
    "timeframe when the image shows them (symbol, axis labels, candle spacing). "
    "If a value is genuinely illegible, say which one, rather than guessing it."
)


def group_guidance(*, has_live_data: bool = False, has_image: bool = False) -> str:
    """Guidance for every answer in a Forex-mode group, question or not.

    Wider than :func:`prompt_guidance` on purpose: the honesty rule has to hold
    for phrasing the detector has never seen.
    """
    if has_image:
        # The picture is the feed; the refusal rule must not fire on a chart.
        return FOREX_GUIDANCE.replace(NO_LIVE_DATA_GUIDANCE, CHART_IMAGE_GUIDANCE)
    if has_live_data:
        return FOREX_GUIDANCE.replace(NO_LIVE_DATA_GUIDANCE, LIVE_DATA_PRESENT_GUIDANCE)
    return FOREX_GUIDANCE


#: Appended for Forex groups. Written as rules, not vibes.
FOREX_GUIDANCE = (
    "This is a Forex/trading group. Answer as a professional trader would:\n"
    "CONVENTIONS\n"
    "- Write pairs the way traders do (EURUSD, GBPJPY, XAUUSD) and say 'pips' "
    "for price movement, never 'points'.\n"
    "- A pip is 0.0001 on 5-digit pairs and 0.01 on JPY pairs; a standard lot is "
    "100,000 units, mini 10,000, micro 1,000. Gold moves in 0.1 'dollars' per pip.\n"
    "- Quote the base currency first: in EURUSD, EUR is base and USD is quote.\n"
    "MACRO REASONING - be specific about the mechanism, never generic\n"
    "- Pair direction is a RELATIVE story: EURUSD rises on hawkish ECB vs dovish "
    "Fed, not on 'good European news' alone. Name both sides of the pair.\n"
    "- Rate differentials and real yields drive currency flows; inflation prints "
    "matter through what they imply for central-bank policy, not in themselves.\n"
    "- Risk sentiment: JPY, CHF and USD strengthen as safe havens in risk-off; "
    "AUD and NZD are risk-on proxies; CAD tracks oil; NOK tracks oil and rates.\n"
    "- Gold rises with falling real yields, a weaker dollar and safe-haven "
    "demand. The dollar index (DXY) and gold usually move inversely.\n"
    "TECHNICALS\n"
    "- Use the correct vocabulary: support/resistance, trend, range, breakout, "
    "retest, pullback, higher-high/higher-low, RSI overbought above 70 and "
    "oversold below 30, moving-average crosses.\n"
    "- A pattern name is not a prediction; describe what it signals and where it "
    "invalidates.\n"
    "ANSWER DISCIPLINE\n"
    "- Keep giveaway answers to the bare term or number, as the style requires.\n"
    "- If a question is ambiguous (which pair, which timeframe), answer the most "
    "standard reading and give the answer, not a question back.\n"
    "- Never invent a price, level, indicator value or event result that is not "
    "in the data you were given.\n"
    "- " + NO_LIVE_DATA_GUIDANCE
)
