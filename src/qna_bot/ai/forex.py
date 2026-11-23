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
    "hedging", "correlation", "volatility", "liquidity", "order block",
    "supply and demand", "session", "london session", "new york session",
    "tokyo session", "forex", "fx", "cfd", "broker", "brokerage", "swap rate",
    "carry trade", "interest rate", "central bank", "fed", "ecb", "boj",
    "fomc", "non-farm", "nfp", "cpi", "gdp", "pmi", "rate hike", "rate cut",
    "hawkish", "dovish", "quantitative easing", "inflation", "deflation",
    "recession", "safe haven", "gold", "xauusd", "silver", "xagusd",
    "oil", "wti", "brent", "dxy", "dollar index", "cable", "fiber", "loonie",
    "aussie", "kiwi", "swissy", "yen", "euro", "pound", "greenback",
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


def group_guidance() -> str:
    """Guidance for every answer in a Forex-mode group, question or not.

    Wider than :func:`prompt_guidance` on purpose: the honesty rule has to hold
    for phrasing the detector has never seen.
    """
    return FOREX_GUIDANCE


#: Appended for Forex groups. Written as rules, not vibes.
FOREX_GUIDANCE = (
    "This is a Forex/trading group. Answer with professional FX conventions:\n"
    "- Write pairs the way traders do (EURUSD, GBPJPY, XAUUSD) and say 'pips' "
    "for price movement, never 'points'.\n"
    "- A pip is 0.0001 for most pairs and 0.01 for JPY pairs; a standard lot is "
    "100,000 units. Use these exact conventions when asked.\n"
    "- For 'what moves X' questions, distinguish interest-rate differentials, "
    "central-bank policy, inflation prints, risk sentiment and commodity terms "
    "of trade. Be specific about which is relevant, not generic.\n"
    "- Keep giveaway answers to the bare term or number, as the style requires.\n"
    "- " + NO_LIVE_DATA_GUIDANCE
)
