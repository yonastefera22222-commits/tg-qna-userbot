"""Exness integration: signing, discovery, quotes, candles, and the no-fallback rule.

The bind is the one this feature exists for: every Forex number the bot posts is
an Exness number, and when Exness cannot be reached the answer admits it rather
than borrowing a price from somewhere else.

Nothing here touches the network - the sandbox has no outbound TLS, and a test
must never prod a live trading account - so all of it runs on
``httpx.MockTransport`` against the documented response shapes.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime

import pytest
from exness_fixtures import (
    ACCOUNT_ID,
    API_KEY,
    GATEWAY,
    TEST_PRIVATE_KEY,
    FakeExness,
    candle_payload,
    daily_payload,
    decode_payload,
    decode_request_payload,
)

from qna_bot.ai.exness import (
    EMPTY_BODY_HASH,
    ExnessCredentials,
    ExnessCredentialsMissing,
    ExnessError,
    b64url,
    credentials_from,
    load_private_key,
    normalise_gateway_url,
    signed_headers,
)
from qna_bot.ai.market import (
    STALE_AFTER_MINUTES,
    MarketFeed,
    parse_period_query,
)


@pytest.fixture
def creds() -> ExnessCredentials:
    return ExnessCredentials(
        api_key=API_KEY,
        private_key=TEST_PRIVATE_KEY,
        account_id=ACCOUNT_ID,
        gateway_url=GATEWAY,
    )


def _feed(fake: FakeExness, creds: ExnessCredentials) -> MarketFeed:
    """MarketFeed wired to a stubbed client and a fixed credential set."""
    settings = {
        "exness_api_key": creds.api_key,
        "exness_private_key": creds.private_key,
        "exness_account_id": creds.account_id,
        "exness_gateway_url": creds.gateway_url,
    }
    feed = MarketFeed(fake.client(), settings_loader=_loader(settings))
    return feed


def _loader(settings: dict):
    async def load() -> dict:
        return dict(settings)

    return load


# ---------------------------------------------------------------------------
# Signing - the part that is easiest to get subtly wrong
# ---------------------------------------------------------------------------


def test_empty_body_hash_matches_the_documented_constant() -> None:
    assert b64url(hashlib.sha256(b"").digest()) == EMPTY_BODY_HASH
    assert EMPTY_BODY_HASH == "47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU"


def test_signed_get_sends_all_six_headers_with_an_empty_idempotency_key(creds) -> None:
    headers = signed_headers(creds, "GET", "/v1/configuration/accounts/1/account")
    assert set(headers) == {
        "EXN-API-KEY",
        "EXN-IDEMPOTENCY-KEY",
        "EXN-TIMESTAMP",
        "EXN-SIGN-VERSION",
        "EXN-DATA",
        "EXN-SIGN",
    }
    # Required header, empty value - omitting it fails auth per the spec.
    assert headers["EXN-IDEMPOTENCY-KEY"] == ""
    assert headers["EXN-SIGN-VERSION"] == "1"
    assert int(headers["EXN-TIMESTAMP"]) > 1_700_000_000_000


def test_headers_are_base64url_without_padding(creds) -> None:
    headers = signed_headers(creds, "GET", "/v1/x")
    for name in ("EXN-DATA", "EXN-SIGN"):
        assert "=" not in headers[name]
        assert "+" not in headers[name] and "/" not in headers[name]


def test_signed_payload_carries_the_exact_path_and_empty_body_hash(creds) -> None:
    path = "/v1/trading/access-point?account_id=152706877"
    headers = signed_headers(creds, "GET", path)
    payload = decode_payload(headers)
    assert payload["api_key"] == API_KEY
    assert payload["idempotency_key"] == ""
    assert payload["sign_version"] == 1
    assert payload["method"] == "GET"
    # Byte-identical to the wire path, query order included.
    assert payload["path"] == path
    assert payload["body_hash"] == EMPTY_BODY_HASH
    assert payload["timestamp"] == int(headers["EXN-TIMESTAMP"])


def test_signature_verifies_against_the_decoded_payload_bytes(creds) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    headers = signed_headers(creds, "GET", "/v1/x")
    payload_bytes = base64.urlsafe_b64decode(
        headers["EXN-DATA"] + "=" * (-len(headers["EXN-DATA"]) % 4)
    )
    signature = base64.urlsafe_b64decode(
        headers["EXN-SIGN"] + "=" * (-len(headers["EXN-SIGN"]) % 4)
    )
    public = load_private_key(TEST_PRIVATE_KEY).public_key()
    assert isinstance(public, Ed25519PublicKey)
    # Raises on a bad signature; signing the base64 string instead would fail here.
    public.verify(signature, payload_bytes)


def test_get_and_post_sign_different_methods(creds) -> None:
    get = decode_payload(signed_headers(creds, "GET", "/p"))
    post = decode_payload(signed_headers(creds, "POST", "/p", body=b"{}"))
    assert get["method"] == "GET"
    assert post["method"] == "POST"
    # A body changes the hash; an empty body uses the constant.
    assert post["body_hash"] != EMPTY_BODY_HASH


# ---------------------------------------------------------------------------
# Credential handling
# ---------------------------------------------------------------------------


def test_128_hex_private_key_loads() -> None:
    key = load_private_key(TEST_PRIVATE_KEY)
    assert key is not None
    # Whitespace from a wrapped paste is tolerated.
    assert load_private_key(" ".join([TEST_PRIVATE_KEY[:64], TEST_PRIVATE_KEY[64:]])) is not None


def test_private_key_must_be_hex_or_pem() -> None:
    with pytest.raises(ExnessError, match="128 hex"):
        load_private_key("not-a-key")


def test_credentials_are_missing_until_all_four_are_set() -> None:
    with pytest.raises(ExnessCredentialsMissing) as excinfo:
        credentials_from({"exness_api_key": API_KEY})
    assert "private signing key" in str(excinfo.value)
    assert "account ID" in str(excinfo.value)

    complete = credentials_from(
        {
            "exness_api_key": API_KEY,
            "exness_private_key": TEST_PRIVATE_KEY,
            "exness_account_id": ACCOUNT_ID,
            "exness_gateway_url": GATEWAY,
        }
    )
    assert complete.is_complete


def test_credential_repr_never_leaks_the_secret(creds) -> None:
    shown = repr(creds)
    assert TEST_PRIVATE_KEY not in shown
    assert API_KEY not in shown  # masked to a prefix/suffix
    assert creds.api_key[:7] in shown


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://gateway.example", "https://gateway.example"),
        ("https://gateway.example/", "https://gateway.example"),
        ("  https://gateway.example  ", "https://gateway.example"),
    ],
)
def test_gateway_url_is_normalised(raw: str, expected: str) -> None:
    assert normalise_gateway_url(raw) == expected


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("http://gateway.example", "https"),
        ("https://user:pass@gateway.example", "credentials"),
        ("https://gateway.example/v1", "bare host"),
        ("https://gateway.example?x=1", "bare host"),
    ],
)
def test_bad_gateway_urls_are_rejected_when_saved(raw: str, fragment: str) -> None:
    with pytest.raises(ExnessError, match=fragment):
        normalise_gateway_url(raw)


# ---------------------------------------------------------------------------
# Host discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_uses_the_gateway_then_the_access_point(creds) -> None:
    fake = FakeExness()
    feed = _feed(fake, creds)
    try:
        snapshot = await feed.snapshot("EURUSD?")
    finally:
        await feed.close()

    assert snapshot.quotes
    # Discovery went to the Gateway; every account-scoped read went to the
    # Access Point it returned. Sending both to one host is the classic bug.
    discovery = [c for c in fake.calls if c.path_with_query.startswith("/v1/trading/access-point")]
    assert [c.host for c in discovery] == ["https://gateway.exness-test.example"]
    for call in fake.candle_requests():
        assert call.host == "https://ap-abc123.exness-test.example"


@pytest.mark.asyncio
async def test_access_point_is_resolved_once_and_cached(creds) -> None:
    fake = FakeExness()
    feed = _feed(fake, creds)
    try:
        await feed.snapshot("EURUSD?")
        first = len([c for c in fake.calls if "access-point" in c.url])
        # A different instrument bypasses the snapshot cache entirely.
        await feed.snapshot("gold price")
        second = len([c for c in fake.calls if "access-point" in c.url])
    finally:
        await feed.close()
    # Discovery is rate limited by Exness; calling it per request would trip it.
    assert first == 1
    assert second == 1


@pytest.mark.asyncio
async def test_signed_path_equals_transmitted_path(creds) -> None:
    """The signature covers ``path`` *as sent*; drift here is an auth failure."""
    fake = FakeExness()
    feed = _feed(fake, creds)
    try:
        await feed.snapshot("EURUSD?")
    finally:
        await feed.close()

    assert fake.candle_requests()
    for call in fake.calls:
        payload = decode_request_payload(call)
        assert payload["path"] == call.path_with_query


@pytest.mark.asyncio
async def test_candle_query_uses_a_negative_count_and_one_from(creds) -> None:
    """``from`` + ``count``, never ``from`` + ``to``: the API rejects the mix."""
    fake = FakeExness()
    feed = _feed(fake, creds)
    try:
        await feed.snapshot("EURUSD?")
    finally:
        await feed.close()

    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(fake.candle_requests()[0].url).query)
    assert "from" in query
    assert "count" in query and query["count"][0].startswith("-")
    assert "to" not in query
    assert query["timeframe"] == ["M1"]
    assert query["price_type"] == ["bid"]


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("open_market")
async def test_fresh_candle_becomes_a_live_exness_quote(creds) -> None:
    fake = FakeExness(candles={"EURUSD": candle_payload(price=1.12345)})
    feed = _feed(fake, creds)
    try:
        snapshot = await feed.snapshot("EURUSD?")
    finally:
        await feed.close()

    assert len(snapshot.quotes) == 1
    quote = snapshot.quotes[0]
    assert quote.price == pytest.approx(1.12345)
    assert quote.kind == "live"
    assert quote.source == "exness"
    assert quote.decimals == 5


@pytest.mark.asyncio
async def test_old_candle_is_market_closed_not_live(creds) -> None:
    """Friday's close is a real Exness price and a false claim of "now"."""
    fake = FakeExness(
        candles={"EURUSD": candle_payload(minutes_ago=STALE_AFTER_MINUTES + 600)}
    )
    feed = _feed(fake, creds)
    try:
        snapshot = await feed.snapshot("EURUSD?")
    finally:
        await feed.close()

    assert snapshot.quotes[0].kind == "market_closed"
    assert "market closed" in snapshot.quotes[0].label()


@pytest.mark.asyncio
async def test_account_suffix_is_used_when_the_plain_symbol_is_absent(creds) -> None:
    """Exness suffixes depend on account type; guessing one breaks silently."""
    fake = FakeExness(
        instruments=["EURUSDm", "XAUUSDm"], candles={"EURUSDm": candle_payload()}
    )
    feed = _feed(fake, creds)
    try:
        snapshot = await feed.snapshot("EURUSD?")
    finally:
        await feed.close()

    assert snapshot.quotes[0].price == pytest.approx(1.0984)
    assert snapshot.quotes[0].symbol == "EURUSDm"
    assert "instrument=EURUSDm" in fake.candle_requests()[0].url


@pytest.mark.asyncio
async def test_instrument_the_account_does_not_offer_fails_honestly(creds) -> None:
    fake = FakeExness(instruments=["XAUUSD"])
    feed = _feed(fake, creds)
    try:
        snapshot = await feed.snapshot("EURUSD?")
    finally:
        await feed.close()

    assert snapshot.is_empty()
    assert snapshot.failed == ["EURUSD"]
    assert "does not offer" in (feed.last_error or "")


@pytest.mark.asyncio
async def test_empty_placeholder_candles_are_not_quoted_as_zero(creds) -> None:
    zero = [
        {
            "time": datetime.now(UTC).isoformat(),
            "open": 0.0,
            "high": 0.0,
            "low": 0.0,
            "close": 0.0,
            "volume": 0.0,
        }
    ]
    fake = FakeExness(candles={"EURUSD": zero})
    feed = _feed(fake, creds)
    try:
        snapshot = await feed.snapshot("EURUSD?")
    finally:
        await feed.close()

    # A quote of 0.00000 is worse than an admitted gap.
    assert snapshot.is_empty()


# ---------------------------------------------------------------------------
# The rule this feature exists for
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_credentials_means_no_quote_and_no_other_vendor(creds) -> None:
    """The feed must not borrow a price when Exness is not connected."""
    fake = FakeExness()
    feed = MarketFeed(fake.client(), settings_loader=_loader({}))
    try:
        snapshot = await feed.snapshot("EURUSD and gold")
    finally:
        await feed.close()

    assert snapshot.is_empty()
    assert feed.last_error_kind == "not_configured"
    # Nothing was fetched at all - least of all from a fallback host.
    assert fake.calls == []


@pytest.mark.asyncio
async def test_feed_makes_no_request_to_any_non_exness_host(creds) -> None:
    fake = FakeExness()
    feed = _feed(fake, creds)
    try:
        await feed.snapshot("EURUSD gold BTCUSD?")

        first = fake.candle_requests()[0]
        payload = decode_request_payload(first)
        assert payload["path"] == first.path_with_query
    finally:
        await feed.close()

    allowed = {"https://gateway.exness-test.example", "https://ap-abc123.exness-test.example"}
    assert fake.hosts() <= allowed
    # Yahoo / gold-api / Frankfurter must be gone, not merely deprioritised.
    assert not any(
        host in url
        for call in fake.calls
        for url in [call.url]
        for host in ("yahoo", "gold-api", "frankfurter", "ecb")
    )


@pytest.mark.asyncio
async def test_discovery_failure_leaves_the_snapshot_empty(creds) -> None:
    fake = FakeExness(discovery_status=503, error_code=2)
    feed = _feed(fake, creds)
    try:
        snapshot = await feed.snapshot("EURUSD?")
    finally:
        await feed.close()

    assert snapshot.is_empty()
    assert "2" in (feed.last_error or "") or "503" in (feed.last_error or "")


@pytest.mark.asyncio
async def test_candle_error_does_not_raise_out_of_an_answer(creds) -> None:
    fake = FakeExness(candles_status=500, error_code=3)
    feed = _feed(fake, creds)
    try:
        snapshot = await feed.snapshot("EURUSD?")
    finally:
        await feed.close()

    assert snapshot.is_empty()
    assert feed.last_error


@pytest.mark.asyncio
async def test_rotating_the_key_rebuilds_the_client_and_host_cache(creds) -> None:
    """A key saved in the dashboard must apply without a restart."""
    fake = FakeExness()
    settings = {
        "exness_api_key": API_KEY,
        "exness_private_key": TEST_PRIVATE_KEY,
        "exness_account_id": ACCOUNT_ID,
        "exness_gateway_url": GATEWAY,
    }
    feed = MarketFeed(fake.client(), settings_loader=_loader(settings))
    try:
        first = await feed.exness()
        rotated = dict(settings, exness_api_key="exnsk-rotated-key-99999")
        feed._settings_loader = _loader(rotated)  # noqa: SLF001 - simulating a save
        second = await feed.exness()
    finally:
        await feed.close()
    assert first is not second
    assert second.credentials.api_key == "exnsk-rotated-key-99999"


# ---------------------------------------------------------------------------
# History / period answers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_period_high_comes_from_exness_daily_candles(creds) -> None:
    """The high of the previous Monday-Friday, from the account's own bars.

    Fixture: three weeks of weekday bars where this week trades highest and the
    previous week is second. "Last week" must return the previous week's high -
    not this week's, and not a trailing seven days' worth of both.
    """
    from datetime import timedelta

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    this_monday = today - timedelta(days=today.weekday())
    bars = []
    # Three calendar weeks of sessions, oldest first, ending with this week's
    # sessions up to today.
    base = this_monday - timedelta(days=21)
    day = base
    step = 0
    while day <= today:
        if day.weekday() < 5:
            # Weeks get progressively higher highs: older is lower.
            week_index = (day - base).days // 7
            level = 100.0 + week_index * 100.0
            bars.append(
                {
                    "time": day.isoformat(),
                    "open": level,
                    "high": level + 10.0,
                    "low": level - 10.0,
                    "close": level,
                    "volume": 1.0,
                }
            )
        day += timedelta(days=1)
        step += 1

    fake = FakeExness(
        instruments=["XAUUSD", "EURUSD"],
        candles={"D1:XAUUSD": bars},
    )
    feed = _feed(fake, creds)
    try:
        query = parse_period_query("what was the highest gold price last week")
        assert query is not None and query.instrument == "XAUUSD"
        answer = await feed.period_answer(query)
    finally:
        await feed.close()

    assert answer is not None
    assert answer.aggregation == "high"
    # Three weeks back is index 0, so the previous week is index 2: its
    # Monday-Friday bars all carry a 310 high, and this week's 410s must not
    # appear even though this week has already printed by mid-week.
    assert answer.high == pytest.approx(310.0)
    # And the window really is Mon-Fri of that week, not a trailing seven days.
    assert answer.start.date().weekday() == 0
    assert answer.end.date().weekday() <= 4
    assert answer.end.date() < this_monday.date()


@pytest.mark.asyncio
async def test_period_answer_is_none_without_exness(creds) -> None:
    fake = FakeExness()
    feed = MarketFeed(fake.client(), settings_loader=_loader({}))
    try:
        query = parse_period_query("highest BTC price last week")
        assert query is not None
        assert await feed.period_answer(query) is None
    finally:
        await feed.close()


@pytest.mark.asyncio
async def test_daily_bars_are_cached_between_questions(creds) -> None:
    fake = FakeExness(
        instruments=["BTCUSD"],
        candles={"D1:BTCUSD": daily_payload([100.0 + i for i in range(20)], days_back=1)},
    )
    feed = _feed(fake, creds)
    try:
        first = await feed.ohlc_bars("BTCUSD", days=7)
        calls = len(fake.candle_requests())
        second = await feed.ohlc_bars("BTCUSD", days=7)
    finally:
        await feed.close()

    assert first and first == second
    # A giveaway fires several questions in a row; refetching is slow and
    # burns the rate limit for identical data.
    assert len(fake.candle_requests()) == calls


def test_instrument_recognition_is_not_a_promise_the_account_has_it() -> None:
    from qna_bot.ai.market import FEEDABLE_INSTRUMENTS, normalise_instrument

    assert "EURUSD" in FEEDABLE_INSTRUMENTS
    assert normalise_instrument("eur/usd") == "EURUSD"
    # A recognised name still has to survive the account's own instrument list,
    # which is what _resolve_symbol does at fetch time.
    assert normalise_instrument("NOTAPAIR") is None


# ---------------------------------------------------------------------------
# Dashboard credential entry
# ---------------------------------------------------------------------------


def test_dashboard_validation_accepts_real_values() -> None:
    from qna_bot.dashboard.handlers.exness import _validate

    assert _validate("api_key", "  exnsk-abcdefghijkl  ")[0] == "exnsk-abcdefghijkl"
    assert _validate("account_id", " 152706877 ")[0] == "152706877"
    assert _validate("gateway_url", "https://gateway.example/")[0] == "https://gateway.example"
    clean, error = _validate("private_key", TEST_PRIVATE_KEY)
    assert error is None and clean == TEST_PRIVATE_KEY


def test_dashboard_validation_rejects_before_saving() -> None:
    """A bad value saved silently surfaces later as 'no live data'."""
    from qna_bot.dashboard.handlers.exness import _validate

    assert _validate("api_key", "short")[1]
    assert _validate("account_id", "152 706 877")[1]
    assert _validate("private_key", "zzzz")[1]
    assert _validate("gateway_url", "http://gateway.example")[1]
    assert _validate("api_key", "   ")[1]


def test_dashboard_validation_keeps_pem_keys_intact() -> None:
    """A multi-line PEM must not be trimmed into a broken key."""
    from qna_bot.dashboard.handlers.exness import _validate

    pem = "-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEI...\n-----END PRIVATE KEY-----"
    clean, error = _validate("private_key", pem)
    assert error is None
    assert clean == pem.strip()


def test_every_exness_setting_is_encrypted_at_rest() -> None:
    """The signing key is a credential; it must never sit in SQLite as text."""
    from qna_bot.storage.database import SECRET_SETTING_KEYS

    assert "exness_api_key" in SECRET_SETTING_KEYS
    assert "exness_private_key" in SECRET_SETTING_KEYS


def test_secret_settings_stay_out_of_shared_snapshots() -> None:
    from qna_bot.storage.database import IDENTITY_SETTING_KEYS

    assert "exness_private_key" in IDENTITY_SETTING_KEYS
    assert "exness_api_key" in IDENTITY_SETTING_KEYS


@pytest.mark.asyncio
async def test_engine_wiring_reaches_the_feed(creds) -> None:
    """``attach_market_settings`` is what makes a dashboard save take effect."""
    from qna_bot.ai.engine import AIEngine

    fake = FakeExness()
    feed = MarketFeed(fake.client())
    engine = AIEngine(market_feed=feed)
    engine.attach_market_settings(_loader({
        "exness_api_key": API_KEY,
        "exness_private_key": TEST_PRIVATE_KEY,
        "exness_account_id": ACCOUNT_ID,
        "exness_gateway_url": GATEWAY,
    }))

    try:
        snapshot = await feed.snapshot("EURUSD?")
    finally:
        await feed.close()
    assert snapshot.quotes, "engine wiring should make the feed usable"


# ---------------------------------------------------------------------------
# Credential entry, end to end through the handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_saving_a_key_from_the_dashboard_encrypts_it_and_moves_on(tmp_path) -> None:
    """The whole point: what the owner types is what the feed later signs with."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from qna_bot.dashboard.handlers.exness import msg_field
    from qna_bot.dashboard.states import ExnessStates
    from qna_bot.storage.crypto import SecretCipher
    from qna_bot.storage.database import Database

    db = Database(tmp_path / "exness.db", SecretCipher.from_env_or_file(None, tmp_path / ".k"))
    await db.connect()
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=42, user_id=42)
    )
    await state.set_state(ExnessStates.waiting_api_key)

    message = SimpleNamespace(
        text="  exnsk-pasted-key-1234  ", answer=AsyncMock(), delete=AsyncMock()
    )
    ctx = SimpleNamespace(db=db)
    try:
        await msg_field(message, ctx, state)

        # Stored trimmed, and encrypted rather than sitting as plaintext.
        assert await db.get_setting("exness_api_key") == "exnsk-pasted-key-1234"
        async with db.conn.execute(
            "SELECT value FROM settings WHERE key = 'exness_api_key'"
        ) as cursor:
            row = await cursor.fetchone()
        assert "exnsk-pasted-key-1234" not in row["value"]

        # The state is cleared so the next field can be asked for.
        assert await state.get_state() is None
        # The message held a secret; it is removed from the chat.
        message.delete.assert_awaited()
        # And the owner is told what is still missing, not left guessing.
        sent = message.answer.await_args.args[0]
        assert "Still needed" in sent
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_bad_value_keeps_the_owner_in_the_same_step(tmp_path) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from qna_bot.dashboard.handlers.exness import msg_field
    from qna_bot.dashboard.states import ExnessStates
    from qna_bot.storage.crypto import SecretCipher
    from qna_bot.storage.database import Database

    db = Database(tmp_path / "exness2.db", SecretCipher.from_env_or_file(None, tmp_path / ".k"))
    await db.connect()
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=42, user_id=42)
    )
    await state.set_state(ExnessStates.waiting_account_id)

    message = SimpleNamespace(text="152 706 877", answer=AsyncMock(), delete=AsyncMock())
    ctx = SimpleNamespace(db=db)
    try:
        await msg_field(message, ctx, state)
        # Nothing saved, and the owner can simply retype.
        assert await db.get_setting("exness_account_id") is None
        assert await state.get_state() == ExnessStates.waiting_account_id.state
        assert "digits only" in message.answer.await_args.args[0]
    finally:
        await db.close()
