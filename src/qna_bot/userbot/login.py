"""Robust interactive Telegram login (phone + code + 2FA) for the in-bot wizard.

Why this module exists
----------------------
The naive login flow (``send_code_request`` -> "Code sent!" -> wait forever) is
the single most common reason a userbot wizard appears to "silently do nothing"
after the owner types their phone number. Telegram has **not** failed to send
any code in many of those cases; one of these is happening instead:

1. ``signIn`` needs the ``phone_code_hash`` that belongs to the *exact* code
   request. If the temporary client reconnects (new auth key) or the flow falls
   back to sending a *new* code, the stored hash is stale and Telegram answers
   ``PHONE_CODE_EXPIRED`` -- which the UI used to show as a generic error.
2. Telegram rarely delivers the code by SMS. For accounts that already have a
   Telegram app installed it is pushed as a **service message inside Telegram**,
   it can be an in-app confirmation prompt, an **email** code, a **call**/
   flash-call, or it may be blocked until a **login-email** is set up. The old
   code assumed "SMS" and showed nothing about where the code actually went.
3. Resending used ``ResendCode`` with an unknown/stale hash, which Telegram
   rejects with ``PHONE_CODE_EXPIRED`` / ``FLOOD`` instead of delivering again.
4. The temporary login client was disconnected (or was silently collected)
   while the wizard waited, so a correct code could no longer be verified.

This module keeps the temporary client *alive and healthy*, records exactly
where Telegram delivered the code, validates the phone number before talking to
Telegram, transparently recovers from stale hashes, and produces actionable,
specific error messages instead of "could not send login code: <repr>".
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    ApiIdPublishedFloodError,
    AuthRestartError,
    CodeInvalidError,
    FloodError,
    FloodPremiumWaitError,
    FloodWaitError,
    PasswordEmptyError,
    PasswordHashInvalidError,
    PhoneCodeEmptyError,
    PhoneCodeExpiredError,
    PhoneCodeHashEmptyError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberFloodError,
    PhoneNumberInvalidError,
    PhoneNumberOccupiedError,
    PhoneNumberUnoccupiedError,
    PhonePasswordFloodError,
    RPCError,
    SendCodeUnavailableError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.auth import ResendCodeRequest
from telethon.tl.types import auth as auth_types

logger = logging.getLogger(__name__)

PersistCallback = Callable[[dict[str, object] | None], Awaitable[None]]
ClearCallback = Callable[[], Awaitable[None]]

# ---------------------------------------------------------------------------
# Phone number normalisation / validation
# ---------------------------------------------------------------------------

#: Country calling codes that exist today. "1" covers NANP, "7" Russia/Kazakhstan.
_VALID_CALLING_CODES: frozenset[str] = frozenset(
    {
        "1", "7", "20", "27", "30", "31", "32", "33", "34", "36", "39", "40", "41",
        "43", "44", "45", "46", "47", "48", "49", "51", "52", "53", "54", "55", "56",
        "57", "58", "60", "61", "62", "63", "64", "65", "66", "81", "82", "84", "86",
        "90", "91", "92", "93", "94", "95", "98", "211", "212", "213", "216", "218",
        "220", "221", "222", "223", "224", "225", "226", "227", "228", "229", "230",
        "231", "232", "233", "234", "235", "236", "237", "238", "239", "240", "241",
        "242", "243", "244", "245", "246", "248", "249", "250", "251", "252", "253",
        "254", "255", "256", "257", "258", "260", "261", "262", "263", "264", "265",
        "266", "267", "268", "269", "290", "291", "297", "298", "299", "350", "351",
        "352", "353", "354", "355", "356", "357", "358", "359", "370", "371", "372",
        "373", "374", "375", "376", "377", "378", "380", "381", "382", "383", "385",
        "386", "387", "389", "420", "421", "423", "500", "501", "502", "503", "504",
        "505", "506", "507", "508", "509", "590", "591", "592", "593", "594", "595",
        "596", "597", "598", "599", "670", "672", "673", "674", "675", "676", "677",
        "678", "679", "680", "681", "682", "683", "685", "686", "687", "688", "689",
        "690", "691", "692", "850", "852", "853", "855", "856", "880", "886", "960",
        "961", "962", "963", "964", "965", "966", "967", "968", "970", "971", "972",
        "973", "974", "975", "976", "977", "992", "993", "994", "995", "996", "998",
    }
)

#: Common local (trunk) prefixes users type instead of their country code.
_LOCAL_TRUNK_PREFIXES = ("0",)

_E164_RE = re.compile(r"^\+?[1-9]\d{6,14}$")


class PhoneIssue(StrEnum):
    """Why a phone number cannot be used for a Telegram login."""

    OK = "ok"
    EMPTY = "empty"
    NOT_A_NUMBER = "not_a_number"
    NO_COUNTRY_CODE = "no_country_code"
    BAD_COUNTRY_CODE = "bad_country_code"
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"


@dataclass(frozen=True)
class ParsedPhone:
    """Result of normalising user-supplied phone input."""

    e164: str
    issue: PhoneIssue = PhoneIssue.OK
    hint: str = ""

    @property
    def ok(self) -> bool:
        return self.issue is PhoneIssue.OK


def parse_phone(raw: str) -> ParsedPhone:
    """Normalise human phone input into E.164, explaining what is wrong if it is not usable.

    Accepts ``+251912345678``, ``251912345678``, ``00251912345678``, ``+251 91 234 5678``
    and rejects local formats such as ``0912345678`` with a message that says how to
    fix it -- the old flow forwarded whatever arrived to Telethon and surfaced a
    generic "could not send login code" message.
    """
    text = (raw or "").strip()
    if not text:
        return ParsedPhone("", PhoneIssue.EMPTY, "Send your phone number in international format.")

    # Allow users to paste "+251 (91) 234-5678" or "tel:+251..." style input.
    cleaned = re.sub(r"(?i)^(tel|phone)[:\s]+", "", text)
    cleaned = re.sub(r"[^\d+]", "", cleaned)
    had_plus = cleaned.startswith("+")
    cleaned = cleaned.replace("+", "")

    if not cleaned:
        return ParsedPhone(
            "",
            PhoneIssue.NOT_A_NUMBER,
            "That does not look like a phone number. Use digits, e.g. +251912345678.",
        )

    # 00 is the international access code in many countries -> drop it.
    if not had_plus and cleaned.startswith("00"):
        cleaned = cleaned[2:]

    digits = cleaned

    # E.164 allows at most 15 digits including the country code.
    if len(digits) > 15:
        return ParsedPhone(
            f"+{digits}",
            PhoneIssue.TOO_LONG,
            "That number is longer than 15 digits (the international maximum). "
            "Remove any extra digits or spaces.",
        )

    # A leading 0 is a *national trunk prefix*: the country code is unknown, and
    # guessing one would send the code to a completely different country (e.g.
    # "0912…" would look like an Indian +91 number). Ask for the full form.
    if not had_plus and digits.startswith(_LOCAL_TRUNK_PREFIXES):
        return ParsedPhone(
            "",
            PhoneIssue.NO_COUNTRY_CODE,
            "That is a local number (it starts with <code>0</code>), so Telegram "
            "cannot tell which country it belongs to.\n"
            "Send it in international format with your country code instead:\n"
            "• <code>+251912345678</code> (Ethiopia)\n"
            "• <code>+15551234567</code> (US/Canada)\n"
            "• <code>+254712345678</code> (Kenya)",
        )

    if not _has_known_call_prefix(digits):
        return ParsedPhone(
            "",
            PhoneIssue.NO_COUNTRY_CODE,
            "Missing or unknown country code. Include it, e.g. "
            "<code>+251912345678</code> (Ethiopia) or <code>+15551234567</code> (US).",
        )

    e164 = f"+{digits}"

    if len(digits) < 8:
        return ParsedPhone(
            e164,
            PhoneIssue.TOO_SHORT,
            "That number looks too short. Double-check the country code plus the full "
            "national number, e.g. <code>+251912345678</code>.",
        )

    if not _E164_RE.match(e164):  # pragma: no cover - defensive
        return ParsedPhone("", PhoneIssue.BAD_COUNTRY_CODE, "That number does not look right.")

    return ParsedPhone(e164, PhoneIssue.OK)


def _has_known_call_prefix(digits: str) -> bool:
    """True when ``digits`` starts with a real country calling code."""
    for length in (3, 2, 1):
        if len(digits) > length and digits[:length] in _VALID_CALLING_CODES:
            return True
    return False


# ---------------------------------------------------------------------------
# Where did Telegram deliver the code?
# ---------------------------------------------------------------------------


class DeliveryChannel(StrEnum):
    TELEGRAM_APP = "telegram_app"
    SMS = "sms"
    CALL = "call"
    FLASH_CALL = "flash_call"
    MISSED_CALL = "missed_call"
    EMAIL = "email"
    FRAGMENT = "fragment"
    IN_APP_PROMPT = "in_app_prompt"
    SETUP_REQUIRED = "setup_required"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CodeDelivery:
    """Human-readable information about where Telegram sent the login code."""

    channel: DeliveryChannel
    title: str
    detail: str
    length: int | None = None
    #: ``True`` when the user must first complete a step (e.g. set up a login email).
    action_required: bool = False


def describe_delivery(sent: object) -> CodeDelivery:
    """Turn a Telethon ``auth.SentCode`` into precise, actionable UI text.

    This is the part the old wizard was missing entirely: it printed
    "A login code was sent ... via Telegram" for every delivery type, so users
    who received an SMS, an email code, or a confirmation prompt on another
    device waited for a Telegram message that was never going to appear.
    """
    sent_type = getattr(sent, "type", None)
    length = getattr(sent_type, "length", None)

    if isinstance(sent_type, auth_types.SentCodeTypeApp):
        return CodeDelivery(
            DeliveryChannel.TELEGRAM_APP,
            "Code sent inside Telegram",
            "Telegram pushed the login code as a <b>service message</b> to the account "
            "that <b>already uses this phone number</b> — <b>not</b> via SMS.\n\n"
            "📲 Open <b>Telegram</b> on that device. The code arrives in the <b>Telegram "
            "service chat</b> (look for “Telegram” with a blue shield ✓ at the very top "
            "of the chat list). If the app shows a <b>“Yes, it's me” / “Confirm login”</b> "
            "button, just tap it — you will not need to type a code at all.",
            length,
        )
    if isinstance(sent_type, auth_types.SentCodeTypeSms):
        return CodeDelivery(
            DeliveryChannel.SMS,
            "Code sent by SMS",
            "Telegram sent the code to the phone number by <b>SMS</b> "
            f"({length or 5} digits). Check your messages app — delivery can take up to a minute.",
            length,
        )
    if isinstance(sent_type, auth_types.SentCodeTypeSmsWord):
        return CodeDelivery(
            DeliveryChannel.SMS,
            "Code sent by SMS (first word <i>{}</i>)".format(
                getattr(sent_type, "beginning", "") or "…"
            ),
            "Telegram sent the code by SMS; the SMS begins with the word shown above.",
            None,
        )
    if isinstance(sent_type, auth_types.SentCodeTypeSmsPhrase):
        return CodeDelivery(
            DeliveryChannel.SMS,
            "Code sent by SMS (phrase starts with <i>{}</i>)".format(
                getattr(sent_type, "beginning", "") or "…"
            ),
            "Telegram sent the code by SMS; the SMS contains a phrase starting with the word above.",
            None,
        )
    if isinstance(sent_type, auth_types.SentCodeTypeCall):
        return CodeDelivery(
            DeliveryChannel.CALL,
            "Code delivered by phone call",
            "Telegram will <b>call your number</b> and read the code aloud. "
            "Answer the call and enter the digits you hear.",
            length,
        )
    if isinstance(sent_type, auth_types.SentCodeTypeFlashCall):
        return CodeDelivery(
            DeliveryChannel.FLASH_CALL,
            "Code delivered by flash call",
            "Telegram will <b>flash-call</b> your number: reject/ignore the call and use the "
            "phone number that called you as the code (pattern: "
            f"<code>{getattr(sent_type, 'pattern', '')}</code>).",
            None,
        )
    if isinstance(sent_type, auth_types.SentCodeTypeMissedCall):
        return CodeDelivery(
            DeliveryChannel.MISSED_CALL,
            "Code delivered by missed call",
            "Telegram will <b>missed-call</b> your number. The last "
            f"<b>{getattr(sent_type, 'length', 5)}</b> digits of the calling number are the code "
            f"(it starts with <code>{getattr(sent_type, 'prefix', '')}</code>).",
            getattr(sent_type, "length", None),
        )
    if isinstance(sent_type, auth_types.SentCodeTypeEmailCode):
        pattern = getattr(sent_type, "email_pattern", "") or "your login email"
        return CodeDelivery(
            DeliveryChannel.EMAIL,
            "Code sent by email",
            "Telegram sent the login code to your <b>login email</b> matching "
            f"<code>{pattern}</code> (check spam too).",
            length,
        )
    if isinstance(sent_type, auth_types.SentCodeTypeFragmentSms):
        url = getattr(sent_type, "url", "")
        return CodeDelivery(
            DeliveryChannel.FRAGMENT,
            "Code must be bought via Fragment",
            "This number can only receive a login code through Fragment. "
            f"Open <a href='{url}'>{url}</a> to continue.",
            getattr(sent_type, "length", None),
            action_required=True,
        )
    if isinstance(sent_type, auth_types.SentCodeTypeSetUpEmailRequired):
        return CodeDelivery(
            DeliveryChannel.SETUP_REQUIRED,
            "Login email required first",
            "Telegram will not send a code until a <b>login email</b> is set up for this account. "
            "Open Telegram on a device where you are still logged in → "
            "<b>Settings → Privacy and Security → Login email</b>, add one, then tap "
            "🔄 Resend Code here.",
            None,
            action_required=True,
        )
    return CodeDelivery(
        DeliveryChannel.UNKNOWN,
        "Code requested",
        "Telegram accepted the request, but did not say how the code was delivered. "
        "Most often it is a <b>service message inside the Telegram app</b> for this phone "
        "number. Tap 🔄 Resend Code to try another channel (SMS/call).",
        length,
    )


# ---------------------------------------------------------------------------
# Result / state objects
# ---------------------------------------------------------------------------


class LoginStatus(StrEnum):
    OK = "ok"
    CODE_SENT = "code_sent"
    OTP_REQUIRED = "otp_required"
    TWO_FA_REQUIRED = "2fa_required"
    ERROR = "error"


@dataclass
class LoginResult:
    status: LoginStatus
    message: str
    delivery: CodeDelivery | None = None
    #: Seconds the user should wait before retrying (FloodWait etc.).
    retry_after: int | None = None


@dataclass
class PendingLogin:
    """A live temporary Telethon client waiting for the owner's OTP/2FA input."""

    client: TelegramClient
    api_id: int
    api_hash: str
    phone: str
    phone_code_hash: str
    delivery: CodeDelivery | None = None
    #: Telegram said 2FA is needed; ``sign_in(password=...)`` will complete the login.
    code_verified: bool = False
    resend_count: int = 0
    code_length: int | None = None
    created_at: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_activity = time.monotonic()


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


def _flood_result(exc: Exception, action: str) -> LoginResult:
    seconds = int(getattr(exc, "seconds", 0) or 0)
    if isinstance(exc, FloodPremiumWaitError):
        seconds = int(getattr(exc, "seconds", 0) or 0)
    wait_txt = f" Wait <b>{seconds}s</b> and try again." if seconds else " Please wait and try again."
    return LoginResult(
        LoginStatus.ERROR,
        f"⏳ Telegram rate-limited this {action} (FloodWait).{wait_txt}",
        retry_after=seconds or None,
    )


def translate_send_error(exc: Exception, phone: str) -> LoginResult:
    """Map Telethon exceptions from ``send_code_request`` to useful instructions."""
    if isinstance(exc, FloodWaitError):
        return _flood_result(exc, "login code request")
    if isinstance(exc, FloodError):
        return _flood_result(exc, "login code request")
    if isinstance(exc, PhoneNumberFloodError):
        return LoginResult(
            LoginStatus.ERROR,
            "⏳ You requested this login code too many times on this number. "
            "Wait a few minutes (up to ~24h in the worst case) and try again. "
            "Tip: use 🔄 Resend Code instead of sending the number again.",
        )
    if isinstance(exc, PhoneNumberBannedError):
        return LoginResult(
            LoginStatus.ERROR,
            "🚫 Telegram has banned this phone number from logging in. "
            "Contact Telegram support or use a different number.",
        )
    if isinstance(exc, PhoneNumberOccupiedError):
        return LoginResult(
            LoginStatus.ERROR,
            "That number is already linked to a Telegram account that is currently active. "
            "Log in there first, or use the StringSession login method.",
        )
    if isinstance(exc, PhoneNumberUnoccupiedError):
        return LoginResult(
            LoginStatus.ERROR,
            "No Telegram account exists for this number yet. Create the account in the "
            "Telegram app first, then connect it here.",
        )
    if isinstance(exc, PhoneNumberInvalidError):
        return LoginResult(
            LoginStatus.ERROR,
            f"❌ Telegram rejected <code>{phone}</code> as an invalid phone number. "
            "Use full international format including the country code, e.g. "
            "<code>+251912345678</code> (no leading 0, no spaces).",
        )
    if isinstance(exc, ApiIdInvalidError):
        return LoginResult(
            LoginStatus.ERROR,
            "❌ The <b>API ID / API Hash</b> pair is invalid. Log in at "
            "<a href='https://my.telegram.org'>my.telegram.org</a> → <i>API development tools</i>, "
            "then re-enter both values in Step 1.",
        )
    if isinstance(exc, ApiIdPublishedFloodError):
        return LoginResult(
            LoginStatus.ERROR,
            "🚫 This API ID is blocked (it was published publicly). Create a fresh "
            "app at <a href='https://my.telegram.org'>my.telegram.org</a> and re-enter it.",
        )
    if isinstance(exc, SendCodeUnavailableError):
        return LoginResult(
            LoginStatus.ERROR,
            "Telegram could not deliver the code by SMS or call right now. "
            "Install/open Telegram on this number and sign in there, or try again later.",
        )
    if isinstance(exc, AuthRestartError):
        return LoginResult(
            LoginStatus.ERROR,
            "Telegram asked to restart the login. Tap 🔄 Resend Code — that usually fixes it.",
        )
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return LoginResult(
            LoginStatus.ERROR,
            "⏱ Telegram did not answer in time (network problem). Check the server's "
            "outbound access to Telegram and try again.",
        )
    return LoginResult(LoginStatus.ERROR, f"Could not send the login code: {exc}")


def translate_signin_error(exc: Exception) -> LoginResult:
    """Map Telethon exceptions from ``sign_in`` to useful instructions."""
    if isinstance(exc, SessionPasswordNeededError):
        return LoginResult(
            LoginStatus.TWO_FA_REQUIRED,
            "Two-Step Verification (2FA) is enabled on this account. Send your 2FA password.",
        )
    if isinstance(exc, PhoneCodeExpiredError):
        return LoginResult(
            LoginStatus.ERROR,
            "⌛ That login code expired (Telegram codes are short-lived). "
            "Tap 🔄 Resend Code to request a fresh one.",
        )
    if isinstance(exc, (PhoneCodeInvalidError, CodeInvalidError)):
        return LoginResult(
            LoginStatus.ERROR,
            "❌ Wrong login code. Tap 🗑 Clear and re-enter the digits exactly as Telegram "
            "showed them (check for leading zeros).",
        )
    if isinstance(exc, (PhoneCodeEmptyError, PhoneCodeHashEmptyError)):
        return LoginResult(
            LoginStatus.ERROR,
            "The code entry was empty, or the login session was lost. "
            "Tap 🔄 Resend Code to start a fresh code.",
        )
    if isinstance(exc, PasswordHashInvalidError):
        return LoginResult(
            LoginStatus.ERROR,
            "❌ Wrong 2FA password. Try again — note it is your Telegram <i>cloud password</i>, "
            "not the SMS code.",
        )
    if isinstance(exc, PasswordEmptyError):
        return LoginResult(LoginStatus.ERROR, "Please send your 2FA password (it cannot be empty).")
    if isinstance(exc, PhonePasswordFloodError):
        return LoginResult(
            LoginStatus.ERROR,
            "⏳ Too many wrong 2FA attempts. Wait a while before trying again.",
        )
    if isinstance(exc, FloodWaitError):
        return _flood_result(exc, "sign-in attempt")
    if isinstance(exc, FloodError):
        return _flood_result(exc, "sign-in attempt")
    if isinstance(exc, PhoneNumberUnoccupiedError):
        return LoginResult(
            LoginStatus.ERROR,
            "No Telegram account exists for this number. Create it in the Telegram app first.",
        )
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return LoginResult(
            LoginStatus.ERROR,
            "⏱ Telegram did not answer in time. Tap ✅ Submit again — the login session is kept.",
        )
    if isinstance(exc, RPCError):
        return LoginResult(LoginStatus.ERROR, f"Telegram rejected the code: {exc}")
    return LoginResult(LoginStatus.ERROR, f"Sign-in failed: {exc}")


# ---------------------------------------------------------------------------
# Shared client factory
# ---------------------------------------------------------------------------


def make_login_client(api_id: int, api_hash: str, session: StringSession | None = None) -> TelegramClient:
    """Create a temporary Telethon client configured for interactive login.

    ``flood_sleep_threshold=0`` is important: Telethon's default silently
    *sleeps* through small FloodWaits (up to 60s) inside the client. During the
    wizard that looks like a frozen bot, so we surface the wait instead.
    ``request_retries`` is raised a little so flaky mobile-server connections do
    not abort a login the owner is halfway through.
    """
    return TelegramClient(
        session or StringSession(),
        int(api_id),
        api_hash,
        flood_sleep_threshold=0,
        request_retries=5,
        connection_retries=5,
        retry_delay=1,
    )


# ---------------------------------------------------------------------------
# The login session itself
# ---------------------------------------------------------------------------


class PhoneLoginSession:
    """Owns the temporary Telethon client for the whole phone → code → 2FA flow.

    Kept as a separate object from :class:`UserbotManager` so the delicate
    Telegram login state machine is unit-testable without a network.
    """

    #: How many times we transparently request a *fresh* code before giving up.
    MAX_AUTO_RESENDS = 2

    def __init__(self, *, keepalive_interval: float = 20.0) -> None:
        self._pending: PendingLogin | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._keepalive_interval = keepalive_interval
        # Optional persistence (SQLite) so a host restart during login does not
        # silently throw away a login attempt the owner already started.
        self._persist: PersistCallback | None = None
        self._clear_persisted: ClearCallback | None = None

    def set_persistence(
        self,
        persist: PersistCallback | None,
        clear: ClearCallback | None = None,
    ) -> None:
        self._persist = persist
        self._clear_persisted = clear

    def _snapshot(self) -> dict[str, object] | None:
        pending = self._pending
        if pending is None:
            return None
        # Deliberately excludes api_hash: it is already stored (encrypted) under
        # ``tg_api_hash``, and a restarted process can never resume this attempt
        # anyway because Telegram binds the hash to the old auth key.
        return {
            "phone": pending.phone,
            "phone_code_hash": pending.phone_code_hash,
            "code_verified": pending.code_verified,
            "code_length": pending.code_length,
            "resend_count": pending.resend_count,
            "delivery_channel": pending.delivery.channel.value if pending.delivery else None,
            "saved_at": time.time(),
        }

    async def _save(self) -> None:
        if self._persist is None or self._pending is None:
            return
        try:
            await self._persist(self._snapshot())
        except Exception as exc:  # pragma: no cover - storage is best effort
            logger.debug("Could not persist pending login: %s", exc)

    async def _discard(self) -> None:
        if self._clear_persisted is None:
            return
        try:
            await self._clear_persisted()
        except Exception as exc:  # pragma: no cover - storage is best effort
            logger.debug("Could not clear persisted pending login: %s", exc)

    # -- state ---------------------------------------------------------
    @property
    def pending(self) -> PendingLogin | None:
        return self._pending

    @property
    def has_pending(self) -> bool:
        return self._pending is not None

    def _set_pending(self, pending: PendingLogin | None) -> None:
        self._pending = pending

    # -- lifecycle -----------------------------------------------------
    async def close(self) -> None:
        """Disconnect the temporary client (no login completed)."""
        await self._stop_keepalive()
        pending, self._pending = self._pending, None
        await self._discard()
        if pending is not None:
            try:
                await pending.client.disconnect()
            except Exception:  # pragma: no cover - best effort cleanup
                pass

    async def _start_keepalive(self, client: TelegramClient) -> None:
        """Keep the temporary login connection alive while the owner types the code.

        Without this, the temporary client can be dropped (idle timeout, Render
        free-tier network hiccup) and the *correct* code then fails with a vague
        error — the exact symptom being fixed here.
        """
        await self._stop_keepalive()

        async def _loop() -> None:
            while True:
                await asyncio.sleep(self._keepalive_interval)
                try:
                    if not client.is_connected():
                        await client.connect()
                except Exception as exc:  # pragma: no cover - network dependent
                    logger.debug("Login client keepalive failed: %s", exc)

        self._keepalive_task = asyncio.create_task(_loop())

    async def _stop_keepalive(self) -> None:
        task, self._keepalive_task = self._keepalive_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _ensure_connected(self, client: TelegramClient) -> None:
        if not client.is_connected():
            await client.connect()

    # -- step 1: phone -> code -----------------------------------------
    async def request_code(self, api_id: int, api_hash: str, raw_phone: str) -> LoginResult:
        """Validate the number, request a code and record where it was delivered."""
        parsed = parse_phone(raw_phone)
        if not parsed.ok:
            return LoginResult(LoginStatus.ERROR, f"❌ {parsed.hint}")

        await self.close()
        client = make_login_client(api_id, api_hash)
        try:
            await client.connect()
            sent = await client.send_code_request(parsed.e164)
        except Exception as exc:
            try:
                await client.disconnect()
            except Exception:
                pass
            logger.warning("send_code_request failed for %s: %s", parsed.e164, exc)
            return translate_send_error(exc, parsed.e164)

        delivery = describe_delivery(sent)
        self._set_pending(
            PendingLogin(
                client=client,
                api_id=int(api_id),
                api_hash=api_hash.strip(),
                phone=parsed.e164,
                phone_code_hash=sent.phone_code_hash,
                delivery=delivery,
                code_length=delivery.length,
            )
        )
        await self._start_keepalive(client)
        await self._save()
        logger.info(
            "Login code requested for %s via %s (length=%s)",
            parsed.e164,
            delivery.channel,
            delivery.length,
        )
        return LoginResult(
            LoginStatus.CODE_SENT,
            f"Login code requested for {parsed.e164}.",
            delivery=delivery,
        )

    # -- resend ---------------------------------------------------------
    async def resend_code(self) -> LoginResult:
        """Ask Telegram for another code, recovering from stale hashes automatically.

        The naive implementation called ``send_code_request`` again, which (a)
        starts a brand new authorization attempt with a *new* hash, invalidating
        any code the owner already has, and (b) hits ``PHONE_NUMBER_FLOOD`` fast.
        We first try ``ResendCode`` with the live hash (Telegram then switches
        channel, e.g. app message → SMS → call), and only start over if that
        hash is dead.
        """
        pending = self._pending
        if pending is None:
            return LoginResult(LoginStatus.ERROR, "No login in progress. Send your phone number again.")

        pending.touch()
        await self._ensure_connected(pending.client)

        # 1) Preferred: Telegram's own "resend the same attempt" endpoint.
        try:
            sent = await pending.client(ResendCodeRequest(pending.phone, pending.phone_code_hash))
            pending.resend_count += 1
            delivery = describe_delivery(sent)
            pending.phone_code_hash = getattr(sent, "phone_code_hash", "") or pending.phone_code_hash
            pending.delivery = delivery
            pending.code_length = delivery.length or pending.code_length
            await self._save()
            return LoginResult(
                LoginStatus.CODE_SENT,
                f"New code requested — {delivery.title}.",
                delivery=delivery,
            )
        except (PhoneCodeExpiredError, PhoneCodeHashEmptyError, SendCodeUnavailableError) as exc:
            logger.info("ResendCode unusable (%s); starting a fresh code request", exc)
        except FloodWaitError as exc:
            return _flood_result(exc, "resend request")
        except FloodError as exc:
            return _flood_result(exc, "resend request")
        except Exception as exc:
            logger.debug("ResendCode failed unexpectedly: %s", exc)

        # 2) Fallback: brand new request from the same (still connected) client.
        try:
            sent = await pending.client.send_code_request(pending.phone)
        except Exception as exc:
            logger.warning("Fresh send_code_request failed for %s: %s", pending.phone, exc)
            return translate_send_error(exc, pending.phone)

        delivery = describe_delivery(sent)
        pending.phone_code_hash = sent.phone_code_hash
        pending.delivery = delivery
        pending.code_length = delivery.length or pending.code_length
        pending.resend_count += 1
        await self._save()
        return LoginResult(
            LoginStatus.CODE_SENT,
            f"Fresh code requested — {delivery.title}.",
            delivery=delivery,
        )

    # -- step 2: code ---------------------------------------------------
    async def submit_code(self, raw_code: str) -> LoginResult:
        """Verify the OTP. Recovers transparently from a stale ``phone_code_hash``."""
        pending = self._pending
        if pending is None:
            return LoginResult(
                LoginStatus.ERROR,
                "No login session is active. Send your phone number again to get a new code.",
            )

        digits = "".join(ch for ch in (raw_code or "") if ch.isdigit())
        expected = pending.code_length
        if not digits:
            return LoginResult(LoginStatus.ERROR, "Please enter the code digits first.")
        if expected and len(digits) != expected:
            return LoginResult(
                LoginStatus.ERROR,
                f"The code should be <b>{expected} digits</b>, but {len(digits)} were entered. "
                "Tap 🗑 Clear and try again.",
            )
        if not expected and len(digits) < 4:
            return LoginResult(LoginStatus.ERROR, "Code looks too short. Enter all digits sent by Telegram.")

        pending.touch()
        await self._ensure_connected(pending.client)

        try:
            await pending.client.sign_in(
                phone=pending.phone,
                code=digits,
                phone_code_hash=pending.phone_code_hash,
            )
        except SessionPasswordNeededError:
            # Telegram accepted the code; only the cloud password is missing.
            pending.code_verified = True
            return LoginResult(
                LoginStatus.TWO_FA_REQUIRED,
                "Two-Step Verification (2FA) is enabled. Send your Telegram cloud password.",
            )
        except (PhoneCodeExpiredError, PhoneCodeHashEmptyError) as exc:
            logger.info("Code hash stale during sign-in (%s); fetching a fresh code", exc)
            return await self._recover_stale_hash(digits)
        except (PhoneCodeInvalidError, CodeInvalidError) as exc:
            logger.info("Invalid code entered for %s: %s", pending.phone, exc)
            return translate_signin_error(exc)
        except Exception as exc:
            logger.warning("sign_in failed for %s: %s", pending.phone, exc)
            return translate_signin_error(exc)

        return await self._success()

    async def _recover_stale_hash(self, digits: str) -> LoginResult:
        """The hash no longer matches (common after a resend/reconnect).

        A code that was *just* delivered with a new hash is very often the same
        code the owner is holding, so we fetch a fresh hash and retry the same
        digits once before telling the user to wait for another code.
        """
        pending = self._pending
        assert pending is not None
        if pending.resend_count >= self.MAX_AUTO_RESENDS:
            return translate_signin_error(PhoneCodeExpiredError(request=None))

        try:
            sent = await pending.client.send_code_request(pending.phone)
        except Exception as exc:
            return translate_send_error(exc, pending.phone)

        pending.phone_code_hash = sent.phone_code_hash
        pending.delivery = describe_delivery(sent)
        pending.resend_count += 1

        try:
            await pending.client.sign_in(
                phone=pending.phone,
                code=digits,
                phone_code_hash=pending.phone_code_hash,
            )
        except SessionPasswordNeededError:
            pending.code_verified = True
            return LoginResult(
                LoginStatus.TWO_FA_REQUIRED,
                "Two-Step Verification (2FA) is enabled. Send your Telegram cloud password.",
            )
        except Exception as exc:
            return translate_signin_error(exc)

        return await self._success()

    # -- step 3: 2FA ----------------------------------------------------
    async def submit_password(self, password: str) -> LoginResult:
        pending = self._pending
        if pending is None:
            return LoginResult(LoginStatus.ERROR, "No login session is active. Restart the login.")

        if not (password or "").strip():
            return LoginResult(LoginStatus.ERROR, "Please send your 2FA password (it cannot be empty).")

        pending.touch()
        await self._ensure_connected(pending.client)
        try:
            await pending.client.sign_in(password=password)
        except SessionPasswordNeededError:
            return LoginResult(LoginStatus.TWO_FA_REQUIRED, "That password was not accepted. Try again.")
        except Exception as exc:
            logger.warning("2FA sign_in failed: %s", exc)
            return translate_signin_error(exc)

        return await self._success()

    # -- completion -----------------------------------------------------
    async def _success(self) -> LoginResult:
        pending = self._pending
        assert pending is not None
        await self._stop_keepalive()
        await self._discard()
        return LoginResult(LoginStatus.OK, "Login successful.")

    def take_authorized_client(self) -> PendingLogin | None:
        """Hand the now-authorized client to the caller and stop tracking it."""
        pending, self._pending = self._pending, None
        return pending
