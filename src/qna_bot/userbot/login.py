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
from dataclasses import dataclass, field, replace
from enum import StrEnum

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    ApiIdPublishedFloodError,
    AuthRestartError,
    CodeInvalidError,
    EmailInvalidError,
    EmailUnconfirmedError,
    EmailVerifyExpiredError,
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
from telethon.tl import types
from telethon.tl.functions.account import (
    SendVerifyEmailCodeRequest,
    VerifyEmailRequest,
)
from telethon.tl.functions.auth import ResendCodeRequest
from telethon.tl.types import CodeSettings
from telethon.tl.types import auth as auth_types

logger = logging.getLogger(__name__)

PersistCallback = Callable[[dict[str, object] | None], Awaitable[None]]
ClearCallback = Callable[[], Awaitable[None]]
NotifyCallback = Callable[[str], Awaitable[None]]

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
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def _looks_like_email(value: str) -> bool:
    return bool(_EMAIL_RE.match((value or "").strip()))


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

    # A trunk prefix after the country code. "+251 091 234 5678" is how a great many
    # people write their own number: the international form with the national 0 kept.
    # Sent as-is, Telegram receives "+2510912345678", which is a *different* number
    # (and almost certainly nobody's), so it accepts the request and delivers the
    # code nowhere. Only stripped for the country codes where a leading 0 is known to
    # be a trunk prefix -- genuinely international numbers such as +1 0123... or
    # +44 0123... are left untouched because a real number really can start that way.
    digits = _drop_trunk_after_country_code(digits, had_plus=had_plus)

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


#: Country codes whose national numbers are written with a leading 0 that must be
#: removed before the country code. Covers the countries this bot is actually used
#: from; deliberately not a blanket rule, because for some numbering plans a leading
#: 0 is part of the number.
_TRUNK_ZERO_COUNTRIES = frozenset(
    {
        "20",   # Egypt
        "27",   # South Africa
        "30",   # Greece
        "31",   # Netherlands
        "32",   # Belgium
        "33",   # France
        "34",   # Spain
        "39",   # Italy (mobile numbers start with 3, landlines keep the 0 -> stripped)
        "40",   # Romania
        "41",   # Switzerland
        "43",   # Austria
        "44",   # United Kingdom
        "45",   # Denmark
        "46",   # Sweden
        "47",   # Norway
        "48",   # Poland
        "49",   # Germany
        "52",   # Mexico
        "55",   # Brazil
        "60",   # Malaysia
        "61",   # Australia
        "62",   # Indonesia
        "63",   # Philippines
        "64",   # New Zealand
        "66",   # Thailand
        "81",   # Japan
        "82",   # South Korea
        "84",   # Vietnam
        "90",   # Türkiye
        "91",   # India
        "92",   # Pakistan
        "94",   # Sri Lanka
        "98",   # Iran
        "212",  # Morocco
        "213",  # Algeria
        "216",  # Tunisia
        "218",  # Libya
        "220",  # Gambia
        "221",  # Senegal
        "233",  # Ghana
        "234",  # Nigeria
        "249",  # Sudan
        "250",  # Rwanda
        "251",  # Ethiopia
        "252",  # Somalia
        "254",  # Kenya
        "255",  # Tanzania
        "256",  # Uganda
        "257",  # Burundi
        "258",  # Mozambique
        "260",  # Zambia
        "263",  # Zimbabwe
        "264",  # Namibia
        "265",  # Malawi
        "267",  # Botswana
        "268",  # Eswatini
        "351",  # Portugal
        "352",  # Luxembourg
        "353",  # Ireland
        "358",  # Finland
        "370",  # Lithuania
        "371",  # Latvia
        "372",  # Estonia
        "380",  # Ukraine
        "381",  # Serbia
        "385",  # Croatia
        "386",  # Slovenia
        "420",  # Czechia
        "421",  # Slovakia
        "966",  # Saudi Arabia
        "971",  # UAE
        "972",  # Israel
        "974",  # Qatar
        "977",  # Nepal
        "994",  # Azerbaijan
    }
)

#: Minimum national-number length we accept after stripping the trunk zero, so a
#: number that merely starts with 0 is not shortened into nonsense.
_MIN_NATIONAL_DIGITS = 8


def _drop_trunk_after_country_code(digits: str, *, had_plus: bool) -> str:
    """Remove a national trunk ``0`` that was kept after the country code.

    ``+2510912345678`` -> ``+251912345678``. Returns ``digits`` unchanged whenever
    the country is unknown, no ``0`` follows the country code, or the remainder
    would be too short to be a real number.
    """
    if not isinstance(digits, str) or not digits:
        return digits
    for length in (3, 2, 1):
        country = digits[:length]
        if country not in _TRUNK_ZERO_COUNTRIES:
            continue
        rest = digits[length:]
        if not rest.startswith("0"):
            return digits
        stripped = rest.lstrip("0")
        # Guard: only strip when what is left is still a plausible national number.
        if len(stripped) < _MIN_NATIONAL_DIGITS:
            return digits
        return country + stripped
    return digits


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
    #: Telegram refuses every other channel and demands a login email instead.
    EMAIL_SETUP = "email_setup"
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
    #: The channel Telegram will use if we ask it to send the code again
    #: (``auth.sentCode.next_type``). ``None`` means Telegram named no fallback,
    #: in which case a "resend" button would be a lie.
    next_channel: DeliveryChannel | None = None
    #: Seconds to wait before Telegram will accept a resend (``auth.sentCode.timeout``).
    next_after: int | None = None


#: Map Telegram's ``auth.CodeType`` (the *next* delivery channel) to our enum.
_NEXT_TYPE_CHANNELS: tuple[tuple[str, DeliveryChannel], ...] = (
    ("CodeTypeCall", DeliveryChannel.CALL),
    ("CodeTypeSms", DeliveryChannel.SMS),
    ("CodeTypeFlashCall", DeliveryChannel.FLASH_CALL),
    ("CodeTypeMissedCall", DeliveryChannel.MISSED_CALL),
    ("CodeTypeFragmentSms", DeliveryChannel.FRAGMENT),
)


def describe_next_channel(next_type: object) -> DeliveryChannel | None:
    """Turn ``auth.sentCode.next_type`` into the channel a resend would use.

    Worth the code: the whole "no code ever arrives" complaint is often really
    "no code arrives *on the channel asked for*, and the app never says another
    channel exists". Telegram always names the fallback here; the old UI threw
    it away, so Resend looked like it did nothing when in fact it was switching
    to SMS or a call.
    """
    if next_type is None:
        return None
    name = type(next_type).__name__
    for candidate, channel in _NEXT_TYPE_CHANNELS:
        if name == candidate:
            return channel
    return None


def describe_delivery(sent: object) -> CodeDelivery:
    """Turn a Telethon ``auth.SentCode`` into precise, actionable UI text.

    Every branch here describes *this* attempt; the fallback channel Telegram
    nominated for the *next* attempt is attached afterwards in one place, so no
    single branch can forget it.
    """
    delivery = _describe_this_attempt(sent)
    return replace(
        delivery,
        next_channel=describe_next_channel(getattr(sent, "next_type", None)),
        next_after=getattr(sent, "timeout", None),
    )


def _describe_this_attempt(sent: object) -> CodeDelivery:
    """Describe where Telegram just put the code (one branch per delivery type).

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
            "button, just tap it — you will not need to type a code at all.\n\n"
            "⚠️ <b>If that chat stays empty</b>, Telegram is suppressing the code, not "
            "losing it — retrying only adds a flood wait. Tap <b>📧 Get the code by "
            "email</b>: Telegram delivers that channel to third-party clients reliably.",
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
    if isinstance(sent_type, auth_types.SentCodeTypeFirebaseSms):
        # Android/Google-service numbers: Telegram hands the code to Google Play
        # services, which shows it as a one-tap prompt *inside the Telegram app* on
        # that device - it never becomes an SMS. Reporting this as "unknown" (as the
        # bot used to) makes people wait for an SMS that Telegram was never going to
        # send, which is a big part of the "no code ever arrives" complaint.
        return CodeDelivery(
            DeliveryChannel.TELEGRAM_APP,
            "Code delivered by Google Play services",
            "This number gets its login code through <b>Google Play services</b>, not "
            "SMS: Telegram hands it to your phone and it appears as a "
            "<b>“Confirm” prompt inside the Telegram app</b> on the device that has "
            "this number, or in the <b>“Telegram” service chat</b>.\n\n"
            "📲 Open Telegram on that phone and look for the prompt or the code. "
            "Nothing will arrive by SMS for this attempt.\n\n"
            "⚠️ If nothing appears there, use <b>📷 QR Login</b> or "
            "<b>📧 Get the code by email</b> — those do not depend on this channel.",
            length,
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
    #: Telegram refused to send any code until a "login email" is set up
    #: (``SentCodeTypeSetUpEmailRequired``). Until this is cleared, *no* code will
    #: ever arrive no matter how many times the owner taps Resend.
    email_setup_required: bool = False
    email_setup_sent_to: str | None = None
    email_setup_length: int | None = None
    #: The owner explicitly asked for the code by email, and Telegram agreed.
    #: From then on the delivery channel is the inbox, not the app.
    email_login_requested: bool = False
    #: We already moved this attempt off the in-app channel once (one shot only).
    channel_escalated: bool = False
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


def code_settings() -> CodeSettings | None:
    """The delivery channels to permit when asking Telegram for a login code.

    Telethon calls ``auth.sendCode`` with a bare ``CodeSettings()``, whose every
    flag is ``None`` - and an unset TL flag means "not allowed". So the bot was
    literally telling Telegram not to use the SMS/FlashCall/Firebase channels,
    then wondering why no code arrived.

    ``allow_app_hash`` is the one that matters most here. Telethon computes the
    ``api_id``/``api_hash`` application hash and sends it with ``auth.sendCode``
    only when this flag is set; Telegram's anti-spam uses that hash to recognise a
    known third-party application rather than treating every login as an
    unrecognised client and silently dropping the code. Leaving it unset is a real,
    reproducible difference between this bot and a well-behaved client.

    Everything else is sent as an explicit ``False`` rather than ``None`` so the
    request states a decision instead of omitting one.
    """
    try:
        return CodeSettings(
            allow_flashcall=False,
            current_number=False,
            allow_app_hash=True,
            allow_missed_call=False,
            allow_firebase=True,
            unknown_number=False,
        )
    except Exception as exc:  # pragma: no cover - older Telethon builds
        logger.warning("Could not build CodeSettings (%s); using the default", exc)
        return None


#: The identity this userbot signs in with. Every one of these values is visible in
#: the owner's own Telegram "Other Devices" list as a working session
#: ("Pixel 6", "userbot 10.2.0"), so they are load-bearing, not decoration.
DEVICE_MODEL = "Pixel 6"
SYSTEM_VERSION = "SDK 33"
APP_VERSION = "10.2.0 (4300)"
LANG_CODE = "en"
SYSTEM_LANG_CODE = "en-US"


def make_login_client(api_id: int, api_hash: str, session: StringSession | None = None) -> TelegramClient:
    """Create a temporary Telethon client configured for interactive login.

    ``flood_sleep_threshold=0`` is important: Telethon's default silently
    *sleeps* through small FloodWaits (up to 60s) inside the client. During the
    wizard that looks like a frozen bot, so we surface the wait instead.
    ``request_retries`` is raised a little so flaky mobile-server connections do
    not abort a login the owner is halfway through.

    The device parameters are not cosmetic and must not be removed. They are the
    identity this userbot has always signed in with, and the owner's own Telegram
    "Other Devices" list shows the session it produced: Pixel 6 / userbot 10.2.0,
    from Ashburn - Render's datacenter, i.e. this exact deployment. A session only
    appears in that list after a *successful* login, so this identity, from that IP,
    is demonstrably one Telegram accepts for this account.

    Removing them makes the client announce itself as Telethon/Python from a
    datacenter IP instead, which is a different - and apparently less welcome -
    client for this account.
    """
    return TelegramClient(
        session or StringSession(),
        int(api_id),
        api_hash,
        flood_sleep_threshold=0,
        request_retries=5,
        connection_retries=5,
        retry_delay=1,
        device_model=DEVICE_MODEL,
        system_version=SYSTEM_VERSION,
        app_version=APP_VERSION,
        lang_code=LANG_CODE,
        system_lang_code=SYSTEM_LANG_CODE,
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
        #: Optional one-shot "act on this yourself" message for the owner.
        self._notify: NotifyCallback | None = None
        # Optional persistence (SQLite) so a host restart during login does not
        # silently throw away a login attempt the owner already started.
        self._persist: PersistCallback | None = None
        self._load_session: Callable[[], Awaitable[str | None]] | None = None
        #: Last persisted snapshot, kept in memory so ``restore()`` can rebuild the
        #: attempt without a second database round-trip.
        self._last_snapshot: dict[str, object] | None = None
        self._save_session: Callable[[str | None], Awaitable[None]] | None = None
        self._session_tasks: set[asyncio.Task] = set()
        self._clear_persisted: ClearCallback | None = None

    def set_notify(self, notify: NotifyCallback | None) -> None:
        """Register a callback used to message the owner *outside* the chat flow."""
        self._notify = notify

    async def _tell_owner(self, text: str) -> None:
        notify, self._notify = self._notify, None
        if notify is None:
            return
        try:
            await notify(text)
        except Exception as exc:  # pragma: no cover - notification is best effort
            logger.debug("Could not notify the owner: %s", exc)

    def set_session_store(
        self,
        load: Callable[[], Awaitable[str | None]] | None,
        save: Callable[[str | None], Awaitable[None]] | None,
    ) -> None:
        """Persist the *temporary login client's* session string.

        This is what makes a login survive the host restarting between "send your
        number" and "type the code" - the normal case on Render Free, which sleeps
        after 15 minutes idle and redeploys on every push. Telegram binds
        ``phone_code_hash`` to the auth key of the connection that requested the
        code, so keeping that connection's session is the only way the code the
        owner is holding can still be accepted afterwards.
        """
        self._load_session = load
        self._save_session = save

    def _store_session(self, client: TelegramClient | None) -> None:
        save, self._save_session = self._save_session, self._save_session
        if save is None:
            return
        value: str | None = None
        if client is not None:
            try:
                value = client.session.save()
            except Exception:  # pragma: no cover - defensive
                value = None
        task = asyncio.create_task(save(value))
        # Keep a reference so the task is not garbage collected mid-flight.
        self._session_tasks.add(task)
        task.add_done_callback(self._session_tasks.discard)

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
        if self._pending is not None:
            self._last_snapshot = self._snapshot()
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
        self._store_session(None)
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
    def seed_snapshot(self, snapshot: dict[str, object] | None) -> None:
        """Hand ``restore()`` the persisted attempt (including credentials)."""
        self._last_snapshot = snapshot

    async def restore(self) -> bool:
        """Reconnect a login attempt that was interrupted by a process restart.

        The owner sent a number, Telegram accepted it, and the host restarted before
        the code was typed - on Render Free that happens on every deploy and after
        15 minutes idle. Telegram ties ``phone_code_hash`` to the auth key of the
        connection that asked for the code, so the attempt can only continue if that
        connection's session was kept. It is; this rebuilds the client from it, so
        the code the owner already has is still accepted.
        """
        if self._pending is not None:
            return True
        if self._load_session is None:
            return False
        try:
            stored = await self._load_session()
        except Exception as exc:  # pragma: no cover - storage is best effort
            logger.debug("Could not load a saved login session: %s", exc)
            return False
        if not stored:
            return False
        snapshot = self._last_snapshot
        if not snapshot or not snapshot.get("phone") or not snapshot.get("phone_code_hash"):
            return False

        api_id = snapshot.get("api_id")
        api_hash = snapshot.get("api_hash")
        if not api_id or not api_hash:
            # api_hash is never snapshotted on purpose; the caller supplies creds.
            return False

        try:
            client = make_login_client(
                int(api_id), str(api_hash), session=StringSession(str(stored))
            )
            await client.connect()
        except Exception as exc:
            logger.info("Could not rebuild the interrupted login connection: %s", exc)
            return False

        channel = snapshot.get("delivery_channel")
        delivery = None
        for candidate in DeliveryChannel:
            if candidate.value == channel:
                delivery = CodeDelivery(candidate, channel.replace("_", " ").title(), "")
                break
        pending = PendingLogin(
            client=client,
            api_id=int(api_id),
            api_hash=str(api_hash),
            phone=str(snapshot["phone"]),
            phone_code_hash=str(snapshot["phone_code_hash"]),
            delivery=delivery,
            code_length=snapshot.get("code_length"),  # type: ignore[arg-type]
            resend_count=int(snapshot.get("resend_count") or 0),
        )
        self._set_pending(pending)
        # The interrupted attempt has already used its one automatic resend, if any.
        pending.channel_escalated = True
        await self._start_keepalive(client)
        logger.info(
            "Resumed the interrupted login attempt for %s (code still valid).",
            pending.phone,
        )
        return True

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
        blocked = delivery.channel is DeliveryChannel.SETUP_REQUIRED
        self._set_pending(
            PendingLogin(
                client=client,
                api_id=int(api_id),
                api_hash=api_hash.strip(),
                phone=parsed.e164,
                phone_code_hash=sent.phone_code_hash,
                delivery=delivery,
                code_length=delivery.length,
                email_setup_required=blocked,
            )
        )
        await self._start_keepalive(client)
        await self._save()
        # Keep this connection's session so a restart can pick the attempt up again.
        self._store_session(client)
        logger.info(
            "Login code requested for %s via %s (length=%s, email_setup_required=%s)",
            parsed.e164,
            delivery.channel,
            delivery.length,
            blocked,
        )
        if blocked:
            # Telegram genuinely sent nothing. Reporting "code sent" here is the
            # silent dead end that made the wizard look broken.
            return LoginResult(
                LoginStatus.ERROR,
                "Telegram refused to send a login code until a <b>login email</b> is set up "
                "for this account. I can set one up for you — tap the button below.",
                delivery=delivery,
            )
        return LoginResult(
            LoginStatus.CODE_SENT,
            f"Login code requested for {parsed.e164}.",
            delivery=delivery,
        )

    # -- login-email setup (unblocks SentCodeTypeSetUpEmailRequired) -----
    async def send_login_email_code(self, email: str) -> LoginResult:
        """Start Telegram's required login-email setup for a blocked account.

        Some accounts (commonly new numbers, or numbers Telegram wants to verify
        more strongly) get ``SentCodeTypeSetUpEmailRequired`` back instead of a
        code. Telegram then sends a verification code *to the email address*, and
        only after that email is verified will the login code be delivered.
        Previously the wizard simply showed text here and stopped, so nothing the
        owner did could ever produce a code.
        """
        pending = self._pending
        if pending is None:
            return LoginResult(LoginStatus.ERROR, "No login in progress. Send your phone number again.")

        address = (email or "").strip()
        if not _looks_like_email(address):
            return LoginResult(
                LoginStatus.ERROR,
                "That does not look like an email address. Send one like "
                "<code>you@gmail.com</code>.",
            )

        pending.touch()
        await self._ensure_connected(pending.client)
        try:
            sent = await pending.client(
                SendVerifyEmailCodeRequest(
                    types.EmailVerifyPurposeLoginSetup(
                        phone_number=pending.phone,
                        phone_code_hash=pending.phone_code_hash,
                    ),
                    address,
                )
            )
        except Exception as exc:
            logger.warning("SendVerifyEmailCode failed for %s: %s", address, exc)
            return translate_send_error(exc, pending.phone)

        pattern = getattr(sent, "email_pattern", address)
        length = getattr(sent, "length", None)
        pending.email_setup_sent_to = address
        pending.email_setup_length = length
        await self._save()
        logger.info("Login-email verification code sent to %s", pattern)
        return LoginResult(
            LoginStatus.CODE_SENT,
            f"Telegram sent a verification code to <b>{pattern}</b>. "
            f"It has <b>{length or 5} digits</b> — send it here.",
        )

    async def submit_login_email_code(self, raw_code: str) -> LoginResult:
        """Verify the login-email code; afterwards the normal login code is delivered."""
        pending = self._pending
        if pending is None:
            return LoginResult(LoginStatus.ERROR, "No login in progress. Send your phone number again.")

        digits = "".join(ch for ch in (raw_code or "") if ch.isdigit())
        if not digits:
            return LoginResult(LoginStatus.ERROR, "Please send the code from your email inbox.")
        if pending.email_setup_length and len(digits) != pending.email_setup_length:
            return LoginResult(
                LoginStatus.ERROR,
                f"The email code should be <b>{pending.email_setup_length} digits</b>, "
                f"but {len(digits)} were sent. Check the email again.",
            )

        pending.touch()
        await self._ensure_connected(pending.client)
        try:
            await pending.client(
                VerifyEmailRequest(
                    types.EmailVerifyPurposeLoginSetup(
                        phone_number=pending.phone,
                        phone_code_hash=pending.phone_code_hash,
                    ),
                    types.EmailVerificationCode(code=digits),
                )
            )
        except Exception as exc:
            logger.warning("VerifyEmail failed: %s", exc)
            return translate_send_error(exc, pending.phone)

        # Email verified: the block is lifted, so request the real login code.
        pending.email_setup_required = False
        await self._save()
        logger.info("Login email verified; requesting the login code now")
        result = await self.resend_code()
        if result.status is LoginStatus.CODE_SENT:
            return LoginResult(
                LoginStatus.CODE_SENT,
                f"✅ Login email verified! {result.message}",
                delivery=result.delivery,
            )
        return result

    # -- switch the code onto the login email ---------------------------
    async def use_login_email(self, email: str) -> LoginResult:
        """Move the login code onto an email address. Works *during* a login.

        This is the fix for the real dead end. Telegram stopped sending login
        codes by SMS to third-party clients in Feb 2023, and since early 2026 it
        also silently withholds the in-app code for many accounts: ``sendCode``
        answers ``SentCodeTypeApp`` with a valid hash and nothing is ever
        delivered (Telethon #4730). Retrying cannot help, and the QR route is
        worse — Telegram refuses ``tg://login`` links opened inside its own app
        (TDLib ``internalLinkTypeQrCodeAuthentication``: "must be scanned from
        QR-code using in-app camera"), so it demands a second screen and a
        camera.

        The email channel is different: Telegram's authorization docs treat
        ``auth.sentCodeTypeEmailCode`` as a first-class delivery method for
        third-party apps, and it is delivered by Telegram's own mail servers.
        ``account.sendVerifyEmailCode`` with an ``emailVerifyPurposeLoginSetup``
        purpose is legal *inside* an in-flight login and returns the login code
        straight back in ``account.emailVerifiedLogin.sentCode``, so the owner
        never has to leave the chat or wait for a delivery that never comes.
        """
        pending = self._pending
        if pending is None:
            return LoginResult(
                LoginStatus.ERROR, "No login in progress. Send your phone number again."
            )

        address = (email or "").strip()
        if not _looks_like_email(address):
            return LoginResult(
                LoginStatus.ERROR,
                "That does not look like an email address. Send one like "
                "<code>you@gmail.com</code>.",
            )

        pending.touch()
        await self._ensure_connected(pending.client)
        try:
            sent = await pending.client(
                SendVerifyEmailCodeRequest(
                    types.EmailVerifyPurposeLoginSetup(
                        phone_number=pending.phone,
                        phone_code_hash=pending.phone_code_hash,
                    ),
                    address,
                )
            )
        except Exception as exc:
            logger.warning("Login-email switch failed: %s", exc)
            return self._explain_email_failure(exc)

        pattern = getattr(sent, "email_pattern", address)
        length = getattr(sent, "length", None)
        pending.email_setup_sent_to = address
        pending.email_setup_length = length
        pending.email_setup_required = False
        await self._save()
        logger.info("Login code routed to email %s", pattern)
        return LoginResult(
            LoginStatus.CODE_SENT,
            f"📬 Telegram sent a login code to <b>{pattern}</b> "
            f"({length or 5} digits). Send it here to finish logging in.",
        )

    def _explain_email_failure(self, exc: Exception) -> LoginResult:
        """Say which reason Telegram gave, because the fix differs per reason."""
        if isinstance(exc, FloodWaitError):
            return _flood_result(exc, "email login code request")
        if isinstance(exc, EmailInvalidError):
            return LoginResult(
                LoginStatus.ERROR,
                "❌ Telegram rejected that email address. Send a normal mailbox such as "
                "<code>you@gmail.com</code>.",
            )
        if isinstance(exc, EmailVerifyExpiredError):
            return LoginResult(
                LoginStatus.ERROR,
                "⌛ That email verification expired. Tap 📧 Get the code by email to "
                "start it again.",
            )
        if isinstance(exc, EmailUnconfirmedError):
            return LoginResult(
                LoginStatus.ERROR,
                "📧 Telegram still considers that address unconfirmed. Send the "
                "verification code from the email before the login code.",
            )
        name = type(exc).__name__
        if name in {"EmailNotAllowedError", "EmailAddressNotAllowedError", "EMAIL_NOT_ALLOWED"}:
            return LoginResult(
                LoginStatus.ERROR,
                "🚫 Telegram will not accept an email code for this account right now. "
                "Try a different address, or use 📱 Phone + OTP and then 📷 Make a QR "
                "Code from another device.",
            )
        if isinstance(exc, PhoneCodeExpiredError):
            return LoginResult(
                LoginStatus.ERROR,
                "⌛ The login attempt expired. Send your phone number again, then choose "
                "📧 Get the code by email.",
            )
        return translate_send_error(exc, "your account")

    async def submit_email_login_code(self, raw_code: str) -> LoginResult:
        """Verify the code Telegram emailed, then continue with the login code.

        ``account.verifyEmail`` does **not** log you in: on success it returns
        ``account.emailVerifiedLogin`` carrying a *fresh* ``auth.sentCode`` — the
        login code for the next step. That code goes to the newly verified inbox
        (the docs' "you will receive Telegram login codes via email and not
        SMS"), so the owner reads one more email and types it once. Two emails
        is the price of a channel that actually delivers.
        """
        pending = self._pending
        if pending is None:
            return LoginResult(
                LoginStatus.ERROR, "No login in progress. Send your phone number again."
            )

        digits = "".join(ch for ch in (raw_code or "") if ch.isdigit())
        if not digits:
            return LoginResult(LoginStatus.ERROR, "Please send the code from your email.")

        expected = pending.email_setup_length
        if expected and len(digits) != expected:
            return LoginResult(
                LoginStatus.ERROR,
                f"The email code should be <b>{expected} digits</b>, but {len(digits)} "
                "were sent. Check the email again.",
            )

        pending.touch()
        await self._ensure_connected(pending.client)
        try:
            verified = await pending.client(
                VerifyEmailRequest(
                    types.EmailVerifyPurposeLoginSetup(
                        phone_number=pending.phone,
                        phone_code_hash=pending.phone_code_hash,
                    ),
                    types.EmailVerificationCode(code=digits),
                )
            )
        except SessionPasswordNeededError:
            pending.code_verified = True
            return LoginResult(
                LoginStatus.TWO_FA_REQUIRED,
                "Two-Step Verification (2FA) is enabled. Send your Telegram cloud password.",
            )
        except Exception as exc:
            logger.warning("VerifyEmail (login code) failed: %s", exc)
            return translate_signin_error(exc)

        # Telegram hands back the login-code descriptor for the next step.
        sent = getattr(verified, "sent_code", None)
        if sent is None:  # pragma: no cover - guarded by the API contract
            return LoginResult(
                LoginStatus.ERROR,
                "Telegram verified the email but did not send a login code. "
                "Tap 🔄 Resend Code.",
            )

        pending.phone_code_hash = getattr(sent, "phone_code_hash", "") or pending.phone_code_hash
        delivery = describe_delivery(sent)
        pending.delivery = delivery
        pending.code_length = delivery.length or pending.code_length
        pending.email_setup_required = False
        await self._save()
        logger.info("Email verified; login code dispatched via %s", delivery.channel)

        return LoginResult(
            LoginStatus.CODE_SENT,
            f"✅ Email verified. {delivery.title} — check that inbox (and spam) for the "
            f"<b>{delivery.length or 5}-digit</b> login code and send it here.",
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


# ---------------------------------------------------------------------------
# QR-code login: no code delivery involved at all
# ---------------------------------------------------------------------------


def render_qr_png(url: str, *, box_size: int = 8, border: int = 2) -> bytes | None:
    """Render a ``tg://login`` URL as a PNG QR code.

    Returns ``None`` when the optional ``qrcode`` dependency is unavailable so
    callers can fall back to showing the raw link.
    """
    try:
        import io

        import qrcode
    except Exception:  # pragma: no cover - optional dependency
        return None
    try:
        img = qrcode.make(url, box_size=box_size, border=border)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not render QR PNG: %s", exc)
        return None




@dataclass
class QrLoginState:
    """A live QR login attempt (no SMS/app code is ever sent).

    Telegram's ``tg://login`` token is valid for only ~30 seconds, and that TTL
    is enforced server-side -- a client cannot ask for a longer one. To stop the
    owner from constantly racing an expiry, the session keeps a *live* QR
    indefinitely: each expiring token is automatically replaced via
    ``QRLogin.recreate()`` on the same Telegram connection, and the bump counter
    lets the UI update the image in place.
    """

    client: TelegramClient
    api_id: int
    api_hash: str
    url: str
    expires_at: float
    login: object  # telethon.tl.custom.qrlogin.QRLogin
    task: asyncio.Task | None = None
    status: LoginStatus = LoginStatus.CODE_SENT
    message: str = ""
    #: Incremented every time a fresh token is generated, so the UI can tell
    #: that the scanned image is stale and should be replaced.
    generation: int = 0
    #: How many automatic refreshes have happened (diagnostics/UI wording).
    refreshes: int = 0
    #: Set when refreshing permanently failed (e.g. flood wait).
    fatal_error: str | None = None

    @property
    def seconds_left(self) -> int:
        return max(0, int(self.expires_at - time.time()))


class QrLoginSession:
    """QR-code login that stays valid until it is approved.

    Why this exists: it removes the *entire* code-delivery problem (no SMS, no
    service message, no email, no flood wait). The one weakness was that
    Telegram's QR token lives ~30 seconds, so a slow scan failed. This session
    now renews the token automatically for as long as the owner is on the
    screen, which is why "the QR expired" is no longer a failure mode.
    """

    #: How many consecutive automatic renewals before we give up. Each token is
    #: ~30s, so this covers hours of waiting without hammering Telegram.
    MAX_REFRESHES = 480
    #: Renew slightly before Telegram's own expiry to avoid a dead image.
    REFRESH_MARGIN = 6.0

    def __init__(self, *, poll_timeout: float = 300.0) -> None:
        self._state: QrLoginState | None = None
        #: Kept for compatibility: the session now outlives the initial window.
        self._poll_timeout = poll_timeout

    @property
    def state(self) -> QrLoginState | None:
        return self._state

    @property
    def active(self) -> bool:
        return self._state is not None

    async def start(self, api_id: int, api_hash: str) -> tuple[LoginResult, str | None]:
        """Begin a QR login. Returns (result, tg://login url to render as a QR)."""
        await self.close()
        client = make_login_client(api_id, api_hash)
        try:
            await client.connect()
            qr = await client.qr_login()
        except Exception as exc:
            try:
                await client.disconnect()
            except Exception:
                pass
            logger.warning("qr_login could not start: %s", exc)
            return translate_send_error(exc, "your account"), None

        expires_at = self._expiry_of(qr, fallback_seconds=30.0)
        state = QrLoginState(
            client=client,
            api_id=int(api_id),
            api_hash=api_hash.strip(),
            url=qr.url,
            expires_at=expires_at,
            login=qr,
            generation=1,
        )
        self._state = state
        state.task = asyncio.create_task(self._drive(qr, client, state))
        logger.info(
            "QR login started (token renews automatically every ~%ds)",
            max(1, int(expires_at - time.time())),
        )
        return (
            LoginResult(
                LoginStatus.CODE_SENT,
                "Scan this QR code with your phone's Telegram app to log in.",
            ),
            qr.url,
        )

    @staticmethod
    def _expiry_of(qr: object, *, fallback_seconds: float) -> float:
        expires = getattr(qr, "expires", None)
        if expires is not None:
            try:
                return float(expires.timestamp())
            except Exception:  # pragma: no cover - exotic tz objects
                pass
        return time.time() + fallback_seconds

    async def _drive(self, qr: object, client: TelegramClient, state: QrLoginState) -> None:
        """Wait for a scan, renewing the token before each expiry.

        This replaces the old one-shot ``wait(timeout=120)``: instead of dying
        when the 30-second token lapses, the loop asks Telegram for a new token
        on the *same* connection and keeps going until the QR is approved.
        """
        token = qr
        while True:
            wait_for = max(5.0, state.seconds_left - self.REFRESH_MARGIN)
            try:
                await token.wait(timeout=wait_for)  # type: ignore[attr-defined]
            except SessionPasswordNeededError:
                state.status = LoginStatus.TWO_FA_REQUIRED
                state.message = "QR scanned — this account has 2FA. Send your cloud password."
                return
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                # Expected: the token lapsed. Renew it and keep listening.
                if not await self._renew(state):
                    return
                token = state.login
                continue
            except Exception as exc:
                # A transient RPC/network error is not fatal -- renew and retry.
                logger.warning("QR wait error (%s); renewing token", exc)
                if not await self._renew(state):
                    return
                token = state.login
                continue

            # wait() only returns (without raising) once Telegram confirms a scan.
            state.status = LoginStatus.OK
            state.message = "QR code scanned and approved — logging in."
            return

    async def _renew(self, state: QrLoginState) -> bool:
        """Fetch a fresh token on the existing connection. False if unrecoverable."""
        if state.refreshes >= self.MAX_REFRESHES:
            state.status = LoginStatus.ERROR
            state.message = "The QR code was not scanned for a very long time. Tap 🔄 New QR Code."
            state.fatal_error = state.message
            return False

        if not state.client.is_connected():
            try:
                await state.client.connect()
            except Exception as exc:
                state.status = LoginStatus.ERROR
                state.message = f"Lost the Telegram connection: {exc}. Tap 🔄 New QR Code."
                state.fatal_error = state.message
                return False

        try:
            await state.login.recreate()  # type: ignore[attr-defined]
        except FloodWaitError as exc:
            state.status = LoginStatus.ERROR
            state.message = (
                f"Telegram asked us to wait {exc.seconds}s before issuing another QR code. "
                "Tap 🔄 New QR Code after that."
            )
            state.fatal_error = state.message
            return False
        except Exception as exc:
            logger.warning("Could not renew QR token: %s", exc)
            state.status = LoginStatus.ERROR
            state.message = f"Could not refresh the QR code: {exc}. Tap 🔄 New QR Code."
            state.fatal_error = state.message
            return False

        state.url = state.login.url  # type: ignore[attr-defined]
        state.expires_at = self._expiry_of(state.login, fallback_seconds=30.0)
        state.generation += 1
        state.refreshes += 1
        logger.debug(
            "QR token renewed (#%d), valid for ~%ds", state.refreshes, state.seconds_left
        )
        return True

    async def submit_password(self, password: str) -> LoginResult:
        """Complete a QR login on accounts with 2FA."""
        state = self._state
        if state is None:
            return LoginResult(LoginStatus.ERROR, "No QR login in progress.")
        try:
            await state.client.sign_in(password=password)
        except Exception as exc:
            return translate_signin_error(exc)
        state.status = LoginStatus.OK
        state.message = "Logged in with QR code + 2FA."
        return LoginResult(LoginStatus.OK, state.message)

    def poll(self) -> QrLoginState | None:
        return self._state

    def take_authorized_client(self) -> PendingLogin | None:
        """Return the authorized client in the shape the manager expects."""
        state, self._state = self._state, None
        if state is None:
            return None
        if state.task is not None and not state.task.done():
            state.task.cancel()
        return PendingLogin(
            client=state.client,
            api_id=state.api_id,
            api_hash=state.api_hash,
            phone="",
            phone_code_hash="",
        )

    async def close(self) -> None:
        state, self._state = self._state, None
        if state is None:
            return
        if state.task is not None and not state.task.done():
            state.task.cancel()
            try:
                await state.task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await state.client.disconnect()
        except Exception:  # pragma: no cover - best effort
            pass
