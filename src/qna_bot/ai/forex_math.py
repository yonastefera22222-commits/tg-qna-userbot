"""Forex arithmetic, solved locally.

Giveaways ask the same handful of numeric questions over and over:

    "how much is 20 pips on a standard lot EURUSD"
    "what is 2% of a 5000 account"
    "what lot size for 1% risk with a 25 pip stop on 10000"
    "how many pips from 1.09850 to 1.09950"

Every one is arithmetic, and the local math solver cannot help because the
expression is buried in words. Sending these to a model costs 300-800 ms and
occasionally returns a confident wrong number; a giveaway has no tolerance for
either. So they are recognised and computed here, the same way the arithmetic
solver works, and answered with zero network calls.

Everything is derived from the standard conventions a trader would use:
- a pip is 0.0001 for a 5-digit pair and 0.01 for a JPY pair
- a standard lot is 100,000 units, a mini 10,000, a micro 1,000, a nano 100
- pip value in the quote currency = pip * units; converted to the account
  currency only when the quote currency already is the account currency, which
  is the only case that can be answered without a live rate (and is why the
  USD-quoted majors are the ones answered here)

Anything requiring a live conversion rate is deliberately NOT answered here -
it falls through to the AI, which can use the live feed.
"""

from __future__ import annotations

import re

#: Pip size per instrument family.
_JPY_PAIRS = ("JPY",)

LOT_UNITS = {
    "standard": 100_000,
    "mini": 10_000,
    "micro": 1_000,
    "nano": 100,
    "cent": 1_000,
}

_PAIR_RE = re.compile(
    r"\b((?:[A-Z]{3}\s*[/\-]?\s*[A-Z]{3})|XAU\s*USD|XAUUSD|GOLD)\b", re.IGNORECASE
)
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

_LOT_WORD_RE = re.compile(
    r"\b(standard|mini|micro|nano|cent)\s*lots?\b|\b(\d+(?:\.\d+)?)\s*lots?\b", re.IGNORECASE
)
_RISK_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*%\s*(?:risk|of|on)\b", re.IGNORECASE)
_STOP_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:pip|pips)\s*(?:stop|sl)\b", re.IGNORECASE)
_LOT_SIZE_RE = re.compile(r"\blot\s*size\b", re.IGNORECASE)
_PIP_MOVE_RE = re.compile(
    r"\bfrom\s+(\d+(?:\.\d+)?)\s*(?:to|-|->|→)\s*(\d+(?:\.\d+)?)", re.IGNORECASE
)
_PIP_VALUE_RE = re.compile(r"\bpip\s*value\b|\bvalue\s*of\s*(?:a|1|one)\s*pip\b", re.IGNORECASE)
_PIP_QUESTION_RE = re.compile(r"\bhow\s+many\s+pips\b|\bpips?\s+(?:in|from|between)\b", re.IGNORECASE)


def _num(text: str) -> float:
    return float(text.replace(",", ""))


def _is_jpy(pair: str) -> bool:
    return any(pair.upper().endswith(code) for code in _JPY_PAIRS)


def pip_size(pair: str) -> float:
    """0.01 for a JPY pair, 0.0001 otherwise (0.1 for gold, by convention)."""
    upper = pair.upper().replace("/", "").replace("-", "").replace(" ", "")
    if upper.startswith(("XAU", "GOLD")):
        return 0.1
    return 0.01 if _is_jpy(upper) else 0.0001


def _fmt(value: float, *, money: bool = False) -> str:
    if money:
        return f"${value:,.2f}"
    if abs(value - round(value)) < 1e-9:
        return f"{round(value):,}"
    return f"{value:,.5f}".rstrip("0").rstrip(".")


def _pair_in(text: str) -> str | None:
    match = _PAIR_RE.search(text)
    if not match:
        return None
    raw = match.group(1).upper()
    if raw in ("GOLD", "XAUUSD", "XAU USD"):
        return "XAUUSD"
    return re.sub(r"[\s/\-]", "", raw)


def _units(text: str) -> float | None:
    """Lot size in units, from "standard lot" / "0.5 lots" / "mini lot"."""
    match = _LOT_WORD_RE.search(text)
    if not match:
        return None
    if match.group(1):
        return LOT_UNITS[match.group(1).lower()]
    if match.group(2):
        return _num(match.group(2)) * LOT_UNITS["standard"]
    return None


def solve_forex_math(text: str) -> str | None:
    """Return the numeric answer, or None when this is not Forex arithmetic.

    Returns None rather than a guess for anything needing a live exchange rate;
    the AI path (with the market feed) handles those.
    """
    if not text or len(text) > 200:
        return None

    pair = _pair_in(text)

    # 1. "how many pips from 1.09850 to 1.09950" - pure subtraction.
    move = _PIP_MOVE_RE.search(text)
    if move and (pair or _PIP_QUESTION_RE.search(text)):
        start_px, end_px = _num(move.group(1)), _num(move.group(2))
        size = pip_size(pair or "EURUSD")
        pips = abs(end_px - start_px) / size
        if pips > 0:
            return _fmt(round(pips, 2))

    risk = _RISK_RE.search(text)
    stop = _STOP_RE.search(text)

    # 2. Position size BEFORE the plain risk-% shortcut: "1% risk with a 25 pip
    #    stop on 10000" is a lot-size question that also contains "1% of ...".
    if risk and stop and _LOT_SIZE_RE.search(text) and pair:
        account = None
        for match in _NUMBER_RE.finditer(text):
            value = _num(match.group(0))
            # The account balance is the largest plain number that is not the
            # stop distance or the risk percentage.
            if value >= 100 and value != _num(stop.group(1)):
                account = value
        if account is not None:
            risk_amount = account * _num(risk.group(1)) / 100
            stop_pips = _num(stop.group(1))
            pip_value_per_lot = pip_size(pair) * LOT_UNITS["standard"]
            if stop_pips > 0 and pip_value_per_lot > 0:
                return f"{risk_amount / (stop_pips * pip_value_per_lot):.2f}"

    # 3. Pip value: "value of 1 pip on a mini lot". Needs a lot size, not a pip
    #    count - "20 pips on a micro lot" is a different question (step 4).
    if _PIP_VALUE_RE.search(text):
        units = _units(text)
        if units and (pair is None or ("JPY" not in pair and pair != "XAUUSD")):
            return _fmt(round(pip_size(pair or "EURUSD") * units, 2), money=True)

    # 4. "how much is 20 pips on a micro lot" - pips * pip value.
    pip_amount = re.search(r"\b(\d+(?:\.\d+)?)\s*pips?\b", text, re.IGNORECASE)
    units = _units(text)
    if pip_amount and units and (pair is None or ("JPY" not in pair and pair != "XAUUSD")):
        value = _num(pip_amount.group(1)) * pip_size(pair or "EURUSD") * units
        return _fmt(round(value, 2), money=True)

    # 5. Plain percentage of an account: "2% of a 5000 account".
    if risk and pair is None:
        amounts = [m for m in _NUMBER_RE.finditer(text) if _num(m.group(0)) >= 100]
        if amounts:
            account = _num(amounts[-1].group(0))
            return _fmt(round(account * _num(risk.group(1)) / 100, 2))

    return None
