"""Exness Trader API client: signed requests, host discovery, quotes, candles.

Why this module exists
----------------------
Forex groups are answered from real numbers only - the model explains, the feed
supplies. That feed used to be a chain of keyless public sources (Yahoo,
gold-api, Frankfurter/ECB), which meant the bot quoted *somebody's* price, not
the broker's price. A giveaway is scored against the Exness terminal in front of
the host, so a tick that is 4 pips off is a wrong answer, and an ECB daily fix
presented as a live price is worse than no answer at all.

So every price now comes from Exness itself. This module is the only place that
knows how to talk to them.

What the Exness Public API actually is
--------------------------------------
Two things are worth stating plainly, because both shape the code:

1. **There is no anonymous quotes endpoint.** Quotes are account-scoped: the API
   key must be allowed to read a specific Exness trading account, and "Standard
   MetaTrader accounts cannot connect to this API". An Exness trading account is
   a hard prerequisite.
2. **The host is discovered, not fixed.** A signed ``GET /v1/trading/access-point``
   against the Gateway host from Key Management's DNS widget returns the
   ``access_point`` for that account. Every account-scoped request is then sent
   *there*, not to the Gateway. Discovery is rate limited, so the result is
   cached and reused.

Request signing (Ed25519, protocol version 1)
---------------------------------------------
Six headers, every request:

``EXN-API-KEY``        the key identifier (``exnsk-...``)
``EXN-TIMESTAMP``      Unix milliseconds, fresh, close to server time
``EXN-SIGN-VERSION``   ``1``
``EXN-IDEMPOTENCY-KEY``  required header; **empty** for signed GET
``EXN-DATA``           base64url(no padding) of the signed JSON payload
``EXN-SIGN``           base64url(no padding) of Ed25519(payload bytes)

The signed payload is decoded-then-signed, i.e. the signature covers the UTF-8
JSON bytes, not the base64url string. Its ``path`` field must be byte-identical
to what goes on the wire - same order, same encoding, no normalisation - so this
module builds the query string once, signs *that string*, and sends *that
string*. Duplicate query parameters are rejected by the server; a bodyless
request hashes the empty byte sequence, whose SHA-256 in base64url is
``47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU``.

Market data
-----------
* **Live price** - the last candle of the tightest timeframe on the Access
  Point, with a small overlap so the newest closed/forming candle comes back
  (``GET /v1/market-data/accounts/{id}/candles``). One signed GET per
  instrument, which keeps the p95 answer fast and needs no long-lived socket.
* **History** - the same endpoint at ``D1``, feeding the period answers
  ("highest price last week") that are computed locally instead of guessed.

Nothing here ever invents a price. If credentials are absent, discovery fails,
the account cannot see the instrument, or the market is shut, the caller gets
``None`` and the answer says "no live data" - which is the honest reply.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

logger = logging.getLogger(__name__)

#: SHA-256 of the empty byte sequence, base64url without padding. Constant per
#: the signing spec; computed here anyway so a reader can see where it comes
#: from (and so a test can pin it).
EMPTY_BODY_HASH = "47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU"

#: The only signature protocol version Exness accepts.
SIGN_VERSION = 1

#: Discovery is explicitly rate limited and the result is stable per account,
#: so it is cached for this long. An hour is far longer than a giveaway needs
#: and short enough that a rebalanced Access Point is picked up the same day.
ACCESS_POINT_TTL_SECONDS = 3600.0

#: Instruments live on the account, but the OpenAPI contract allows suffixes
#: (``EURUSDm``, ``XAUUSDc``) that differ between account types. When the plain
#: name is not offered, a suffixed variant is tried.
_INSTRUMENT_SUFFIXES = ("", "m", "c", "z", "#", ".raw")

#: The tightest timeframe whose history window still comfortably covers "now".
#: S1 history is only retained for a day; M1 for a year, which is plenty.
QUOTE_TIMEFRAME = "M1"
#: Fetch a few candles rather than one: on a quiet pair the newest candle can be
#: an empty placeholder, and the previous one is then the real last price.
QUOTE_CANDLE_COUNT = 3
#: Daily bars for period questions ("last week", "this month").
HISTORY_TIMEFRAME = "D1"

#: A quote newer than this is "live". FX trades ~24/5, so older means the
#: market is shut and the figure is the last available tick.
QUOTE_STALE_MINUTES = 20.0


class ExnessError(RuntimeError):
    """A request to Exness failed, with the business error code when known."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class ExnessCredentialsMissing(ExnessError):
    """No API key / private key / account id is configured.

    Distinct from a failure: nothing was attempted, so the caller can say
    "Exness is not connected" instead of "Exness is broken".
    """


@dataclass(frozen=True)
class ExnessCredentials:
    """The three values the owner supplies, plus the Gateway host."""

    api_key: str
    private_key: str
    account_id: str
    gateway_url: str

    @property
    def is_complete(self) -> bool:
        return all(
            part.strip()
            for part in (self.api_key, self.private_key, self.account_id, self.gateway_url)
        )

    def __repr__(self) -> str:  # never leak a key through a log line or traceback
        return (
            f"ExnessCredentials(api_key={_mask(self.api_key)}, "
            f"account_id={self.account_id!r}, gateway_url={self.gateway_url!r}, "
            f"private_key={'set' if self.private_key else 'missing'})"
        )


def _mask(value: str) -> str:
    """``exnsk-abcd…wxyz`` - enough for the owner to recognise, not to use."""
    value = (value or "").strip()
    if len(value) <= 10:
        return "set" if value else "missing"
    return f"{value[:7]}…{value[-4:]}"


def b64url(raw: bytes) -> str:
    """base64url without ``=`` padding, as every ``EXN-*`` header requires."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def load_private_key(text: str) -> Ed25519PrivateKey:
    """Accept both formats Exness hands out.

    * 128 hex characters = the 64-byte Ed25519 private key (32-byte seed
      followed by the 32-byte public half), as generated in Key Management.
    * An unencrypted PKCS#8 PEM, as generated when the owner registers their own
      public key (``openssl genpkey -algorithm ed25519``).
    """
    text = (text or "").strip()
    if not text:
        raise ExnessCredentialsMissing("No Exness private key configured")

    if text.startswith("-----BEGIN"):
        try:
            key = serialization.load_pem_private_key(text.encode(), password=None)
        except Exception as exc:  # noqa: BLE001 - malformed key is an owner error
            raise ExnessError(f"Exness private key is not a readable PEM: {exc}") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise ExnessError("Exness private key must be Ed25519")
        return key

    # Tolerate PEM/hex pasted with line breaks or spaces.
    compact = re.sub(r"\s+", "", text).lower()
    if not re.fullmatch(r"[0-9a-f]{128}", compact):
        raise ExnessError(
            "Exness private key must be 128 hex characters (or an Ed25519 PEM)"
        )
    seed = bytes.fromhex(compact)[:32]
    return Ed25519PrivateKey.from_private_bytes(seed)


def normalise_gateway_url(value: str) -> str:
    """The Gateway base URL, validated the way Exness documents it.

    They are specific: HTTPS, no credentials, no path, no query, no fragment -
    and explicitly *not* a hostname copied from an example. Validation happens
    here so a typo is caught when the owner saves it, not at answer time.
    """
    raw = (value or "").strip().rstrip("/")
    if not raw:
        raise ExnessCredentialsMissing("No Exness Gateway URL configured")
    parts = urlsplit(raw)
    if parts.scheme != "https":
        raise ExnessError("Exness Gateway URL must start with https://")
    if parts.username or parts.password:
        raise ExnessError("Exness Gateway URL must not contain credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ExnessError("Exness Gateway URL must be a bare host, with no path or query")
    if not parts.hostname:
        raise ExnessError("Exness Gateway URL has no host")
    return f"https://{parts.netloc}"


def signed_headers(
    credentials: ExnessCredentials,
    method: str,
    path_with_query: str,
    *,
    body: bytes = b"",
    idempotency_key: str = "",
    private_key: Ed25519PrivateKey | None = None,
    timestamp_ms: int | None = None,
) -> dict[str, str]:
    """Build the six ``EXN-*`` headers for one request.

    ``path_with_query`` must be *exactly* what is sent on the wire, leading
    slash included and query string in final order.
    """
    key = private_key or load_private_key(credentials.private_key)
    timestamp = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    body_hash = b64url(hashlib.sha256(body).digest()) if body else EMPTY_BODY_HASH

    payload = json.dumps(
        {
            "api_key": credentials.api_key,
            "idempotency_key": idempotency_key,
            "timestamp": timestamp,
            "sign_version": SIGN_VERSION,
            "method": method.upper(),
            "path": path_with_query,
            "body_hash": body_hash,
        },
        separators=(",", ":"),
    ).encode()

    return {
        "EXN-API-KEY": credentials.api_key,
        # Required header, empty value, for signed GETs. Omitting it fails auth.
        "EXN-IDEMPOTENCY-KEY": idempotency_key,
        "EXN-TIMESTAMP": str(timestamp),
        "EXN-SIGN-VERSION": str(SIGN_VERSION),
        "EXN-DATA": b64url(payload),
        # The signature covers the decoded payload bytes, not the b64 string.
        "EXN-SIGN": b64url(key.sign(payload)),
    }


def _raise_for_exness(resp: httpx.Response, what: str) -> None:
    """Turn an Exness error body into an ``ExnessError`` with its code."""
    if resp.status_code < 400:
        return
    code: int | None = None
    message = ""
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            raw_code = payload.get("code")
            code = int(raw_code) if raw_code is not None else None
            message = str(payload.get("error_message") or "")
    except Exception:  # noqa: BLE001 - an HTML 403 has no JSON body
        message = ""
    detail = f"{code} {message}".strip() or f"HTTP {resp.status_code}"
    raise ExnessError(f"Exness {what} failed: {detail}", code=code)


@dataclass
class Candle:
    """One OHLC bar, bid side, as returned by the candle endpoint."""

    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class ExnessClient:
    """Signed REST access to one Exness trading account.

    ``client`` is injectable so tests never touch the network (the sandbox has
    no outbound TLS at all, so tests *must* stub it).
    """

    credentials: ExnessCredentials
    client: httpx.AsyncClient | None = None
    _access_point: str | None = field(default=None, init=False, repr=False)
    _access_point_at: float = field(default=0.0, init=False, repr=False)
    _key: Ed25519PrivateKey | None = field(default=None, init=False, repr=False)
    _discovery_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )
    _instruments: set[str] | None = field(default=None, init=False, repr=False)

    # -- plumbing -------------------------------------------------------
    async def _get_client(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0),
                headers={"User-Agent": "tg-qna-userbot/exness"},
                follow_redirects=False,  # never forward signed headers
            )
        return self.client

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    @property
    def account_id(self) -> str:
        return self.credentials.account_id.strip()

    def _signing_key(self) -> Ed25519PrivateKey:
        if self._key is None:
            self._key = load_private_key(self.credentials.private_key)
        return self._key

    async def _signed_get(
        self, base_url: str, path_with_query: str, *, what: str
    ) -> dict:
        client = await self._get_client()
        headers = signed_headers(
            self.credentials, "GET", path_with_query, private_key=self._signing_key()
        )
        resp = await client.get(f"{base_url}{path_with_query}", headers=headers)
        _raise_for_exness(resp, what)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ExnessError(f"Exness {what} returned no JSON") from exc
        return payload if isinstance(payload, dict) else {}

    # -- host discovery -------------------------------------------------
    async def access_point(self, *, force: bool = False) -> str:
        """Resolve (and cache) the account Access Point for this account.

        The Gateway host is only used for this one call; everything else goes to
        the returned Access Point. Discovery is rate limited, hence the cache.
        """
        cached = self._access_point
        if (
            not force
            and cached is not None
            and (time.monotonic() - self._access_point_at) < ACCESS_POINT_TTL_SECONDS
        ):
            return cached

        async with self._discovery_lock:
            # Another coroutine may have resolved it while we waited.
            cached = self._access_point
            if (
                not force
                and cached is not None
                and (time.monotonic() - self._access_point_at) < ACCESS_POINT_TTL_SECONDS
            ):
                return cached

            gateway = normalise_gateway_url(self.credentials.gateway_url)
            path = f"/v1/trading/access-point?account_id={self.account_id}"
            payload = await self._signed_get(gateway, path, what="host discovery")
            point = payload.get("access_point")
            if not isinstance(point, str) or not point.strip():
                raise ExnessError("Exness host discovery returned no access_point")
            resolved = normalise_gateway_url(point)
            self._access_point = resolved
            self._access_point_at = time.monotonic()
            logger.info("Exness access point resolved: %s", resolved)
            return resolved

    # -- account-scoped reads -------------------------------------------
    async def _account_get(self, path: str, *, what: str) -> dict:
        """Signed GET against the account's Access Point (not the Gateway)."""
        base = await self.access_point()
        return await self._signed_get(base, path, what=what)

    async def available_instruments(self, *, force: bool = False) -> set[str]:
        """Instrument names this account can actually trade and quote."""
        if self._instruments is not None and not force:
            return self._instruments
        payload = await self._account_get(
            f"/v1/configuration/accounts/{self.account_id}/instruments",
            what="instrument list",
        )
        names = payload.get("instruments")
        instruments = {str(n) for n in names} if isinstance(names, list) else set()
        self._instruments = instruments
        return instruments

    def symbol_for(self, instrument: str, available: set[str] | None) -> str:
        """Public name for the account's symbol, falling back to the input.

        Used when labelling a quote: the diagnostic should show ``EURUSDm``
        (what was actually fetched) without exploding if the list went stale.
        """
        try:
            return self._resolve_symbol(instrument, available)
        except ExnessError:
            return instrument

    def _resolve_symbol(self, instrument: str, available: set[str] | None) -> str:
        """Map our instrument name onto the account's symbol, suffix included.

        Exness suffixes depend on the account type (``EURUSDm`` on mini,
        ``XAUUSDc`` on cent). Guessing one would work for some accounts and
        silently break for others, so the account's own list decides.
        """
        if not available:
            return instrument
        if instrument in available:
            return instrument
        upper = instrument.upper()
        for suffix in _INSTRUMENT_SUFFIXES:
            candidate = f"{upper}{suffix}"
            if candidate in available:
                return candidate
        # Case-insensitive last resort (the list is normally uppercase already).
        for name in available:
            if name.upper() == upper:
                return name
        raise ExnessError(f"Exness account does not offer {instrument}", code=4000)

    async def candles(
        self,
        instrument: str,
        *,
        timeframe: str = QUOTE_TIMEFRAME,
        count: int = QUOTE_CANDLE_COUNT,
        price_type: str = "bid",
        instruments: set[str] | None = None,
    ) -> list[Candle]:
        """Newest ``count`` candles, oldest first, as Exness returns them.

        ``from`` is required and is paired with ``count`` (never with ``to``).
        Negative counts are valid and mean "the most recent N", which is exactly
        what a "price now" read wants - no clock synchronisation, no risk of a
        future ``to`` being rejected.
        """
        symbol = self._resolve_symbol(instrument, instruments)
        # Build the query once. This exact string is signed and transmitted;
        # reordering or re-encoding it afterwards is an auth failure.
        path = (
            f"/v1/market-data/accounts/{self.account_id}/candles"
            f"?instrument={symbol}"
            f"&timeframe={timeframe}"
            f"&price_type={price_type}"
            f"&from={_now_iso()}"
            f"&count={-abs(int(count))}"
        )
        payload = await self._account_get(path, what="candles")

        raw = payload.get("candles")
        if not isinstance(raw, list):
            return []
        bars: list[Candle] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            # ``price_type=both`` nests bid/ask; bid alone is flat.
            side = item.get("bid") if isinstance(item.get("bid"), dict) else item
            parsed = _parse_candle(side)
            if parsed is not None:
                bars.append(parsed)
        bars.sort(key=lambda c: c.time)
        return bars

def _now_iso() -> str:
    """RFC 3339 in UTC, second resolution.

    The server requires ``from``; sending "now" and a negative count asks for
    the most recent candles, which is the honest reading of "the price".
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_candle(item: object) -> Candle | None:
    if not isinstance(item, dict):
        return None
    try:
        open_ = float(item["open"])
        high = float(item["high"])
        low = float(item["low"])
        close = float(item["close"])
    except (KeyError, TypeError, ValueError):
        return None
    stamp = _parse_time(item.get("time"))
    if stamp is None:
        return None
    try:
        volume = float(item.get("volume") or 0.0)
    except (TypeError, ValueError):
        volume = 0.0
    return Candle(
        time=stamp, open=open_, high=high, low=low, close=close, volume=volume
    )


def _parse_time(value: object) -> datetime | None:
    """Exness date-times are ISO 8601 with an explicit offset (UTC)."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def credentials_from(settings: dict) -> ExnessCredentials:
    """Build the credentials object from the owner's stored settings.

    Raises ``ExnessCredentialsMissing`` when anything is blank, so callers can
    distinguish "not set up yet" (permanent, the owner must act) from "Exness is
    down" (transient, retry later).
    """
    creds = ExnessCredentials(
        api_key=str(settings.get("exness_api_key") or "").strip(),
        private_key=str(settings.get("exness_private_key") or "").strip(),
        account_id=str(settings.get("exness_account_id") or "").strip(),
        gateway_url=str(settings.get("exness_gateway_url") or "").strip(),
    )
    if not creds.is_complete:
        missing = [
            name
            for name, value in (
                ("API key", creds.api_key),
                ("private signing key", creds.private_key),
                ("account ID", creds.account_id),
                ("Gateway host", creds.gateway_url),
            )
            if not value
        ]
        raise ExnessCredentialsMissing(
            "Exness is not connected (missing " + ", ".join(missing) + ")"
        )
    return creds
