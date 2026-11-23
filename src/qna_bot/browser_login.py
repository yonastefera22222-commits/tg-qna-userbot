"""Browser-based Telegram login: the route that works from a single phone.

Why this module exists
----------------------
Two other routes were tried first, and both are dead ends for the "the code
never arrives" case:

1. **Phone + OTP.** Since Telegram stopped sending SMS to third-party clients
   (Feb 2023), ``auth.sendCode`` answers ``SentCodeTypeApp``: the code is pushed
   as a service message to *other already-logged-in sessions of that number*.
   If the number has no other live session — or Telegram's anti-spam heuristics
   decide not to deliver — the request succeeds, the hash is valid, and nothing
   ever arrives. No client-side retry changes that (Telethon #4730).

2. **The ``tg://login?token=...`` deep link as a button.** This does *not* work
   and was a real bug: Telegram's own deep-link documentation says of these
   links "only handle this deep link if the request to open it comes from
   **outside the app**; do **not** handle it if it was clicked **inside of the
   app**, for example in messages, buttons, ...". A ``url`` button in a bot
   message is precisely "inside the app", so tapping it is a no-op. The token
   is only meant to be rendered as a QR image for *another* device to scan, or
   for an OS to hand to Telegram from a browser.

This module takes the second mechanism and moves it to where it is legal: a
**web page** opened in the phone's browser. Opening ``tg://login?token=...``
from a browser is "outside the app", so Telegram takes over, shows its own
"Log in to Telegram?" confirmation, and one tap approves the session. No code
is delivered anywhere, so nothing can fail to arrive.

Security shape
--------------
* One login attempt at a time, created by the owner from the dashboard chat.
* The page only ever renders the *current* token; it never prints, logs or
  returns it as JSON.
* The page URL carries an unguessable per-attempt id, and it is only ever sent
  into the owner's private dashboard chat.
* The token is renewed continuously on one Telegram connection, so the page can
  simply keep polling and redraw instead of making the owner restart.
"""

from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
from dataclasses import dataclass

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from .userbot.login import (
    LoginResult,
    LoginStatus,
    PendingLogin,
    make_login_client,
    translate_send_error,
)

logger = logging.getLogger(__name__)

#: Telegram hands out login tokens that live ~30s. Renew before that so the page
#: is never showing a dead token when the owner finally taps it.
RENEW_MARGIN_SECONDS = 6.0
#: How many consecutive renewals before the attempt is abandoned. Each token is
#: ~30s, so this is a couple of hours of waiting without hammering Telegram.
MAX_RENEWALS = 480
#: A login attempt nobody has looked at for this long is cleaned up.
ATTEMPT_IDLE_SECONDS = 15 * 60


@dataclass
class BrowserLoginAttempt:
    """One live browser login, owned by one owner in one chat."""

    attempt_id: str
    client: TelegramClient
    api_id: int
    api_hash: str
    url: str
    expires_at: float
    login: object  # telethon.tl.custom.qrlogin.QRLogin
    owner_id: int | None = None
    status: LoginStatus = LoginStatus.CODE_SENT
    message: str = ""
    renewals: int = 0
    created_at: float = 0.0
    last_seen: float = 0.0
    task: asyncio.Task | None = None

    @property
    def seconds_left(self) -> int:
        return max(0, int(self.expires_at - time.time()))

    @property
    def approved(self) -> bool:
        return self.status is LoginStatus.OK

    @property
    def needs_2fa(self) -> bool:
        return self.status is LoginStatus.TWO_FA_REQUIRED

    @property
    def failed(self) -> bool:
        return self.status is LoginStatus.ERROR


class BrowserLoginManager:
    """Owns at most one live browser login attempt (Telegram login is per-account)."""

    def __init__(self) -> None:
        self._attempt: BrowserLoginAttempt | None = None

    # -- lifecycle -----------------------------------------------------
    @property
    def current(self) -> BrowserLoginAttempt | None:
        return self._attempt

    def snapshot(self) -> dict[str, object] | None:
        attempt = self._attempt
        if attempt is None:
            return None
        return {
            "attempt_id": attempt.attempt_id,
            "status": attempt.status,
            "message": attempt.message,
            "seconds_left": attempt.seconds_left,
            "renewals": attempt.renewals,
            "approved": attempt.approved,
            "needs_2fa": attempt.needs_2fa,
            "failed": attempt.failed,
            "url": attempt.url,
        }

    def page_url(self, base_url: str) -> str | None:
        attempt = self._attempt
        if attempt is None:
            return None
        return f"{base_url.rstrip('/')}/login/{attempt.attempt_id}"

    async def start(self, api_id: int, api_hash: str) -> tuple[LoginResult, str | None]:
        """Begin (or restart) the browser login. Returns (result, tg:// link)."""
        await self.close()
        client = make_login_client(int(api_id), api_hash)
        try:
            await client.connect()
            login = await client.qr_login()
        except Exception as exc:
            try:
                await client.disconnect()
            except Exception:  # pragma: no cover - best effort
                pass
            logger.warning("Browser login could not start: %s", exc)
            return translate_send_error(exc, "your account"), None

        now = time.time()
        attempt = BrowserLoginAttempt(
            attempt_id=secrets.token_urlsafe(18),
            client=client,
            api_id=int(api_id),
            api_hash=api_hash.strip(),
            url=login.url,
            expires_at=_expiry_of(login),
            login=login,
            created_at=now,
            last_seen=now,
        )
        self._attempt = attempt
        attempt.task = asyncio.create_task(self._drive(attempt))
        logger.info(
            "Browser login started (token renews every ~%ds while the page is open)",
            max(1, attempt.seconds_left),
        )
        return (
            LoginResult(LoginStatus.CODE_SENT, "Browser login ready."),
            attempt.url,
        )

    def find(self, attempt_id: str) -> BrowserLoginAttempt | None:
        attempt = self._attempt
        if attempt is None:
            return None
        # Constant-time compare; the id is a capability.
        if not secrets.compare_digest(attempt.attempt_id, attempt_id or ""):
            return None
        return attempt

    def touch(self, attempt: BrowserLoginAttempt) -> None:
        attempt.last_seen = time.time()

    async def close(self) -> None:
        attempt, self._attempt = self._attempt, None
        if attempt is None:
            return
        if attempt.task is not None and not attempt.task.done():
            attempt.task.cancel()
            try:
                await attempt.task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await attempt.client.disconnect()
        except Exception:  # pragma: no cover - best effort
            pass

    # -- token renewal --------------------------------------------------
    async def _drive(self, attempt: BrowserLoginAttempt) -> None:
        """Wait for approval, renewing the token before each expiry."""
        while True:
            wait_for = max(5.0, attempt.seconds_left - RENEW_MARGIN_SECONDS)
            try:
                await attempt.login.wait(timeout=wait_for)  # type: ignore[attr-defined]
            except SessionPasswordNeededError:
                attempt.status = LoginStatus.TWO_FA_REQUIRED
                attempt.message = (
                    "Approved — but this account has a Telegram cloud password (2FA)."
                )
                return
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                if not await self._renew(attempt):
                    return
                continue
            except Exception as exc:
                logger.warning("Browser login wait error (%s); renewing token", exc)
                if not await self._renew(attempt):
                    return
                continue

            attempt.status = LoginStatus.OK
            attempt.message = "Approved on the phone — logging in."
            return

    async def _renew(self, attempt: BrowserLoginAttempt) -> bool:
        if attempt.renewals >= MAX_RENEWALS:
            attempt.status = LoginStatus.ERROR
            attempt.message = (
                "Nobody approved the login for two hours. Start a new login link."
            )
            return False

        if not attempt.client.is_connected():
            try:
                await attempt.client.connect()
            except Exception as exc:
                attempt.status = LoginStatus.ERROR
                attempt.message = f"Lost the Telegram connection: {exc}"
                return False

        try:
            await attempt.login.recreate()  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("Could not renew the browser login token: %s", exc)
            attempt.status = LoginStatus.ERROR
            attempt.message = f"Could not refresh the login token: {exc}"
            return False

        attempt.url = attempt.login.url  # type: ignore[attr-defined]
        attempt.expires_at = _expiry_of(attempt.login)
        attempt.renewals += 1
        return True

    # -- completion -----------------------------------------------------
    async def submit_password(self, password: str) -> LoginResult:
        attempt = self._attempt
        if attempt is None:
            return LoginResult(LoginStatus.ERROR, "No login in progress.")
        try:
            await attempt.client.sign_in(password=password)
        except Exception as exc:
            from .userbot.login import translate_signin_error

            return translate_signin_error(exc)
        attempt.status = LoginStatus.OK
        attempt.message = "Logged in with the browser link + 2FA."
        return LoginResult(LoginStatus.OK, attempt.message)

    def take_authorized_client(self) -> PendingLogin | None:
        attempt, self._attempt = self._attempt, None
        if attempt is None or attempt.status is not LoginStatus.OK:
            if attempt is not None:
                self._attempt = attempt
            return None
        if attempt.task is not None and not attempt.task.done():
            attempt.task.cancel()
        return PendingLogin(
            client=attempt.client,
            api_id=attempt.api_id,
            api_hash=attempt.api_hash,
            phone="",
            phone_code_hash="",
        )


def _expiry_of(login: object) -> float:
    expires = getattr(login, "expires", None)
    if expires is not None:
        try:
            return float(expires.timestamp())
        except Exception:  # pragma: no cover - exotic tz objects
            pass
    return time.time() + 30.0


# ---------------------------------------------------------------------------
# The page itself
# ---------------------------------------------------------------------------

#: Deliberately plain: no framework, no external asset, no build step. It has to
#: render instantly on a phone on a slow connection, which is the whole point.
_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<title>Log in with Telegram</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center;
    justify-content: center; padding: 24px;
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #f4f6fa; color: #10131a;
  }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #0f1320; color: #eef2fb; }}
    .card {{ background: #171c2c; box-shadow: none; border-color: #262e46; }}
    .sub {{ color: #a8b3cc; }}
    .code {{ background: #0f1320; border-color: #2a3350; }}
  }}
  .card {{
    width: 100%; max-width: 460px; background: #fff; border: 1px solid #e3e8f2;
    border-radius: 20px; padding: 28px 24px; box-shadow: 0 12px 40px rgba(16,19,26,.08);
    text-align: center;
  }}
  h1 {{ font-size: 1.35rem; margin: 0 0 6px; letter-spacing: -.01em; }}
  .sub {{ color: #5b6580; font-size: .95rem; margin: 0 0 22px; }}
  .btn {{
    display: block; width: 100%; padding: 17px 20px; border-radius: 14px;
    background: #2aabee; color: #fff; font-weight: 650; font-size: 1.05rem;
    text-decoration: none; border: 0; cursor: pointer;
  }}
  .btn:active {{ transform: scale(.99); }}
  .btn[aria-disabled="true"] {{ background: #9fb4c6; pointer-events: none; }}
  .btn.secondary {{
    background: transparent; color: inherit; border: 1px solid #c9d2e4;
    font-weight: 550; margin-top: 10px;
  }}
  .code {{
    margin: 18px 0 0; padding: 14px; border: 1px dashed #c9d2e4; border-radius: 12px;
    font: 600 1.35rem/1.3 ui-monospace, SFMono-Regular, Menlo, monospace;
    letter-spacing: .18em; user-select: all;
  }}
  .hint {{ font-size: .82rem; color: #6b7692; margin: 10px 0 0; }}
  .timer {{ font-variant-numeric: tabular-nums; }}
  .state {{ margin: 20px 0 0; padding: 14px; border-radius: 12px; font-size: .95rem; }}
  .state.ok {{ background: #e7f8ee; color: #0a6b3d; }}
  .state.warn {{ background: #fff4e0; color: #8a5200; }}
  .state.err {{ background: #fdeaea; color: #9b1c1c; }}
  ol {{ text-align: left; margin: 14px 0 0; padding-left: 22px; font-size: .92rem; }}
  li {{ margin: 6px 0; }}
</style>
</head>
<body>
  <main class="card">
    <h1>Log in with Telegram</h1>
    <p class="sub">One tap. No code to type, and nothing to receive.</p>

    <a id="main" class="btn" href="{url}" rel="noreferrer">Open Telegram &amp; Approve</a>
    <button id="copy" class="btn secondary" type="button" hidden>
      Buttons do nothing here? Copy the link
    </button>
    <p class="hint" id="timer">
      Link renews by itself &middot; <span class="timer">{seconds_left}s</span> left
    </p>

    <div id="state" class="state ok" hidden></div>

    <ol id="fallback" hidden>
      <li>Tap <b>Copy the link</b> above.</li>
      <li>Open <b>Telegram</b> &rarr; <b>Saved Messages</b>.</li>
      <li>Paste the link into a message to yourself and tap it there.</li>
      <li>Telegram asks <b>&ldquo;Log in?&rdquo;</b> &mdash; tap <b>Approve</b>.</li>
    </ol>
    <p class="hint" id="windowhint" hidden>
      Already on your phone? Your browser may not be allowed to open Telegram from
      a web page. Use <b>Copy the link</b> &rarr; paste it in <b>Saved Messages</b>
      &rarr; tap it there.
    </p>
  </main>

<script>
(function () {{
  var attempt = {attempt_id};
  var stateBox = document.getElementById('state');
  var timer = document.querySelector('.timer');
  var mainBtn = document.getElementById('main');
  var copyBtn = document.getElementById('copy');
  var fallback = document.getElementById('fallback');
  var windowHint = document.getElementById('windowhint');
  var copyLink = null;
  var stopped = false;

  function setState(kind, text) {{
    stateBox.hidden = false;
    stateBox.className = 'state ' + kind;
    stateBox.innerHTML = text;
  }}

  function showFallback() {{
    copyBtn.hidden = false;
    fallback.hidden = false;
    windowHint.hidden = false;
  }}

  copyBtn.addEventListener('click', function () {{
    if (!copyLink) return;
    var done = function () {{
      copyBtn.textContent = 'Copied \u2713';
      setTimeout(function () {{ copyBtn.textContent = 'Copy the link'; }}, 1800);
    }};
    if (navigator.clipboard && navigator.clipboard.writeText) {{
      navigator.clipboard.writeText(copyLink).then(done, showFallback);
    }}
    var ta = document.createElement('textarea');
    ta.value = copyLink; document.body.appendChild(ta); ta.select();
    try {{ document.execCommand('copy'); done(); }} catch (e) {{}}
    document.body.removeChild(ta);
  }});

  // If Telegram never got focus we cannot detect it; but if the page is still
  // visible a few seconds after the tap, the link almost certainly did nothing
  // (that is the "clicked inside the app" case Telegram refuses to handle), so
  // offer the copy-paste route instead of leaving the owner stuck.
  mainBtn.addEventListener('click', function () {{
    setTimeout(function () {{ if (!document.hidden && !stopped) showFallback(); }}, 2500);
  }});

  function poll() {{
    if (stopped) return;
    fetch('/login/' + attempt + '/state', {{ cache: 'no-store' }})
      .then(function (r) {{ return r.json(); }})
      .then(function (data) {{
        if (data.approved) {{
          stopped = true;
          mainBtn.setAttribute('aria-disabled', 'true');
          mainBtn.textContent = 'Approved \u2713';
          setState('ok', '<b>You are logged in.</b><br>Go back to the Telegram bot \u2014 it will continue on its own.');
          return;
        }}
        if (data.needs_2fa) {{
          stopped = true;
          setState('warn', '<b>Approved.</b> This account has a <b>2FA password</b>.<br>Go back to the Telegram bot and send it there.');
          return;
        }}
        if (data.failed) {{
          stopped = true;
          setState('err', 'This login link expired.<br>Ask the bot for a new one.');
          return;
        }}
        if (data.url && data.url !== mainBtn.getAttribute('href')) {{
          mainBtn.setAttribute('href', data.url);
          copyLink = data.url;
        }}
        if (typeof data.seconds_left === 'number') {{
          timer.textContent = data.seconds_left + 's';
        }}
        setTimeout(poll, 2000);
      }})
      .catch(function () {{ setTimeout(poll, 4000); }});
  }}

  copyLink = mainBtn.getAttribute('href');
  if (!copyLink || copyLink.indexOf('tg://') !== 0) {{
    setState('err', 'This login link is not valid.<br>Ask the bot for a new one.');
    return;
  }}
  // A phone browser may refuse to follow tg:// from a page. Detect that the tap
  // did not leave the page and surface the copy route immediately.
  setTimeout(function () {{ if (!document.hidden) {{ /* page still here */ }} }}, 0);
  poll();
}})();
</script>
</body>
</html>
"""


def render_login_page(attempt: BrowserLoginAttempt) -> str:
    """Render the self-contained login page for one attempt."""
    return _PAGE_TEMPLATE.format(
        url=html.escape(attempt.url, quote=True),
        seconds_left=attempt.seconds_left,
        attempt_id=_js_string(attempt.attempt_id),
    )


def _js_string(value: str) -> str:
    """JSON-encode a value for safe embedding inside a <script> block."""
    import json

    return json.dumps(value).replace("</", "<\\/")
