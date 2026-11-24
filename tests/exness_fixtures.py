"""Shared stubs for the Exness feed.

The sandbox cannot make outbound TLS calls at all, and even on a real host a
test must never touch a trading account. So every market test drives a
``httpx.MockTransport`` that speaks the documented Exness shapes:

* ``GET /v1/trading/access-point?account_id=…`` on the Gateway
  -> ``{"access_point": "...", "account_id": "..."}``
* ``GET /v1/configuration/accounts/{id}/instruments``
  -> ``{"instruments": ["EURUSD", ...]}``
* ``GET /v1/market-data/accounts/{id}/candles?instrument=…&timeframe=…``
  -> ``{"candles": [{"time": ..., "open": ..., ...}]}``

Requests are recorded with their headers so a test can assert that every call
was signed and that the signed path is byte-identical to the transmitted one.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx

#: A deterministic Ed25519 key in the 128-hex form Key Management hands out
#: (32-byte seed followed by the 32-byte public half). Not a real credential.
TEST_PRIVATE_KEY = (
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
    "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
)

GATEWAY = "https://gateway.exness-test.example"
ACCESS_POINT = "https://ap-abc123.exness-test.example"
ACCOUNT_ID = "152706877"
API_KEY = "exnsk-test-key-0000000000"


@dataclass
class RecordedRequest:
    url: str
    headers: dict[str, str]

    @property
    def path_with_query(self) -> str:
        parts = urlsplit(self.url)
        return parts.path + (f"?{parts.query}" if parts.query else "")

    @property
    def host(self) -> str:
        return f"{urlsplit(self.url).scheme}://{urlsplit(self.url).netloc}"


@dataclass
class FakeExness:
    """A signed-request-aware stand-in for the Exness REST surface."""

    instruments: list[str] = field(
        default_factory=lambda: ["EURUSD", "XAUUSD", "BTCUSD", "GBPUSD"]
    )
    #: instrument -> candles, newest last. ``None`` means "generate a live one".
    candles: dict[str, list[dict]] = field(default_factory=dict)
    access_point: str = ACCESS_POINT
    gateway: str = GATEWAY
    account_id: str = ACCOUNT_ID
    discovery_status: int = 200
    candles_status: int = 200
    instruments_status: int = 200
    error_code: int = 3000
    calls: list[RecordedRequest] = field(default_factory=list)

    # -- plumbing -------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport())

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(RecordedRequest(str(request.url), dict(request.headers)))
        path = urlsplit(str(request.url)).path

        if path == "/v1/trading/access-point":
            if self.discovery_status != 200:
                return self._error(self.discovery_status, self.error_code)
            return httpx.Response(
                200,
                json={"access_point": self.access_point, "account_id": self.account_id},
            )

        if path.endswith("/instruments"):
            if self.instruments_status != 200:
                return self._error(self.instruments_status, self.error_code)
            return httpx.Response(200, json={"instruments": self.instruments})

        if path.endswith("/candles"):
            if self.candles_status != 200:
                return self._error(self.candles_status, self.error_code)
            query = parse_qs(urlsplit(str(request.url)).query)
            instrument = (query.get("instrument") or [""])[0]
            timeframe = (query.get("timeframe") or ["M1"])[0]
            if instrument not in self.instruments:
                return self._error(400, 4000)
            # A per-timeframe series wins (``D1:EURUSD``), then a per-instrument
            # one, then a generated live quote.
            bars = self.candles.get(f"{timeframe}:{instrument}")
            if bars is None:
                bars = self.candles.get(instrument)
            if bars is None:
                bars = _live_candles()
            return httpx.Response(200, json={"candles": bars})

        return httpx.Response(404, json={"code": 3000, "error_message": "REQUEST_INVALID"})

    @staticmethod
    def _error(status: int, code: int) -> httpx.Response:
        return httpx.Response(status, json={"code": code, "error_message": "TEST_ERROR"})

    # -- assertions -----------------------------------------------------
    def candle_requests(self) -> list[RecordedRequest]:
        return [c for c in self.calls if c.path_with_query.split("?")[0].endswith("/candles")]

    def hosts(self) -> set[str]:
        return {c.host for c in self.calls}


def _live_candles(minutes_ago: float = 1.0, price: float = 1.0984) -> list[dict]:
    """Three M1 candles ending a minute ago: the shape a quote read expects."""
    now = datetime.now(UTC)
    return [
        {
            "time": (now - timedelta(minutes=minutes_ago + (2 - idx))).isoformat(),
            "open": price - 0.0002,
            "high": price + 0.0001,
            "low": price - 0.0003,
            "close": price if idx == 2 else price - 0.0001,
            "volume": 120.0,
        }
        for idx in range(3)
    ]


def candle_payload(
    *,
    minutes_ago: float = 1.0,
    price: float = 1.0984,
    count: int = 3,
) -> list[dict]:
    now = datetime.now(UTC)
    return [
        {
            "time": (now - timedelta(minutes=minutes_ago + (count - 1 - idx))).isoformat(),
            "open": price - 0.0002,
            "high": price + 0.0001,
            "low": price - 0.0003,
            "close": price if idx == count - 1 else price - 0.0001,
            "volume": 100.0 + idx,
        }
        for idx in range(count)
    ]


def daily_payload(series: list[float], *, days_back: int = 0) -> list[dict]:
    """Daily candles ending ``days_back`` days ago, oldest first.

    ``series`` is a list of closes; highs/lows are derived so the arithmetic in
    ``period_answer`` is predictable.
    """
    now = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    base = now - timedelta(days=days_back)
    bars = []
    total = len(series)
    for idx, close in enumerate(series):
        day = base - timedelta(days=total - 1 - idx)
        bars.append(
            {
                "time": day.isoformat(),
                "open": series[idx - 1] if idx else close,
                "high": close + 10,
                "low": close - 10,
                "close": close,
                "volume": 1.0,
            }
        )
    return bars


def decode_payload(headers: dict[str, str]) -> dict:
    """Unpack ``EXN-DATA`` exactly as the server would."""
    raw = next(v for k, v in headers.items() if k.lower() == "exn-data")
    padded = raw + "=" * (-len(raw) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def decode_request_payload(request: RecordedRequest) -> dict:
    return decode_payload(request.headers)


# ---------------------------------------------------------------------------
# Whole-engine helpers: these files drive AIEngine, so the Exness calls and the
# model calls must share one transport (a MockTransport is bound to a client,
# not to an engine).
# ---------------------------------------------------------------------------

#: Settings a MarketFeed needs. Exported so a test can pass ``exness_settings()``
#: straight into ``MarketFeed(..., settings_loader=...)``.
def exness_settings() -> dict:
    return {
        "exness_api_key": API_KEY,
        "exness_private_key": TEST_PRIVATE_KEY,
        "exness_account_id": ACCOUNT_ID,
        "exness_gateway_url": GATEWAY,
    }


def settings_loader(settings: dict | None = None):
    """An awaitable returning the Exness settings, for ``MarketFeed``."""
    values = exness_settings() if settings is None else dict(settings)

    async def load() -> dict:
        return dict(values)

    return load


def is_exness_request(url: str) -> bool:
    parts = urlsplit(str(url))
    return parts.netloc.endswith("exness-test.example")


def exness_response(request: httpx.Request, daily: list[dict]) -> httpx.Response:
    """Answer the three Exness endpoints a period question touches."""
    from urllib.parse import parse_qs

    parts = urlsplit(str(request.url))
    if parts.path == "/v1/trading/access-point":
        return httpx.Response(200, json={"access_point": ACCESS_POINT, "account_id": ACCOUNT_ID})
    if parts.path.endswith("/instruments"):
        return httpx.Response(200, json={"instruments": ["BTCUSD", "EURUSD", "XAUUSD", "ETHUSD"]})
    if parts.path.endswith("/candles"):
        query = parse_qs(parts.query)
        timeframe = (query.get("timeframe") or ["M1"])[0]
        # Daily requests get the series under test; intraday gets a live quote.
        return httpx.Response(200, json={"candles": daily if timeframe == "D1" else _live_candles()})
    return httpx.Response(404, json={"code": 3000, "error_message": "REQUEST_INVALID"})


def daily_bars_from_yahoo_shaped(payload: dict) -> list[dict]:
    """Convert the old ``{"chart": {...}}`` fixtures into Exness daily candles.

    The photo tests were written against a recorded chart payload; the numbers
    are the same series, so re-shaping one into the other keeps the arithmetic
    being asserted while the source becomes Exness.
    """
    result = ((payload.get("chart") or {}).get("result") or [{}])[0]
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    opens = quote.get("open") or []
    closes = quote.get("close") or []
    bars = []
    for idx, stamp in enumerate(stamps):
        high = highs[idx] if idx < len(highs) else None
        low = lows[idx] if idx < len(lows) else None
        if high is None or low is None:
            continue
        bars.append(
            {
                "time": datetime.fromtimestamp(int(stamp), tz=UTC).isoformat(),
                "open": opens[idx] if idx < len(opens) and opens[idx] is not None else high,
                "high": high,
                "low": low,
                "close": closes[idx] if idx < len(closes) and closes[idx] is not None else low,
                "volume": 1.0,
            }
        )
    return bars
