"""Telethon Userbot lifecycle, in-bot interactive login, group sync, and Q&A responder."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable

from telethon import TelegramClient, events
from telethon.errors import (
    ChatWriteForbiddenError,
    FloodWaitError,
    UserBannedInChannelError,
)
from telethon.sessions import StringSession
from telethon.tl.types import (
    ChannelParticipantsAdmins,
    Document,
    MessageMediaPhoto,
    PeerChannel,
    Photo,
    User,
)

from ..ai.engine import AIEngine, AnswerResult
from ..ai.providers import DEFAULT_SPEED_MODE, PROVIDERS
from ..ai.text_cleanup import prepare_incoming, wants_inherited_image
from ..storage.database import Database, GroupConfig
from .filters import describe_reason, should_evaluate_message
from .first_comment import FIRST_COMMENT_ANSWER, is_first_comment
from .login import (
    CodeDelivery,
    LoginResult,
    LoginStatus,
    PhoneLoginSession,
    QrLoginSession,
    make_login_client,
    render_qr_png,
    translate_send_error,
)

__all__ = ["CodeDelivery", "LoginResult", "LoginStatus", "UserbotManager"]

logger = logging.getLogger(__name__)

NotifyCallback = Callable[[str], Awaitable[None]]


class UserbotManager:
    """Manages the connected Telegram user account (Telethon) and real-time Q&A pipeline."""

    ADMIN_CACHE_TTL = 600.0  # 10 minutes
    ADMIN_REFRESH_INTERVAL = 240.0  # refresh in the background every 4 minutes

    def __init__(self, db: Database, ai_engine: AIEngine) -> None:
        self._db = db
        self._ai = ai_engine
        self._client: TelegramClient | None = None
        self._login = PhoneLoginSession()
        self._login.set_persistence(self._persist_pending_login, self._clear_pending_login)
        self._qr = QrLoginSession()
        self._me_id: int | None = None
        self._me_name: str | None = None
        self._me_username: str | None = None

        # Fast in-memory runtime caches for sub-millisecond giveaway handling
        self._enabled_groups: dict[int, GroupConfig] = {}
        self._global_enabled: bool = True
        # chat_id -> (fetched_at, admin_ids, verified). ``verified`` is False when
        # Telegram refused/failed to answer; an unverified empty set must never
        # be treated as "nobody is an admin".
        self._admin_cache: dict[int, tuple[float, set[int], bool]] = {}
        self._admin_refreshing: set[int] = set()
        self._admin_refresher: asyncio.Task | None = None
        # Recent skip reasons, exposed to /diag so silent drops are visible.
        self._last_decisions: deque[dict[str, object]] = deque(maxlen=40)
        # Live counters for /diag and /health.
        self._seen_count = 0
        self._answered_count = 0
        self._last_reply_ts: dict[int, float] = {}
        self._recent_msg_ids: deque[tuple[int, int]] = deque(maxlen=500)
        self._recent_context: dict[int, deque[str]] = {}
        self._notify_owner: NotifyCallback | None = None

    def set_notify_callback(self, callback: NotifyCallback) -> None:
        self._notify_owner = callback

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected()

    @property
    def account_display(self) -> str:
        if not self.is_connected:
            return "Disconnected"
        name = self._me_name or "Telegram Account"
        if self._me_username:
            return f"{name} (@{self._me_username})"
        return name

    @property
    def has_pending_login(self) -> bool:
        return self._login.has_pending

    async def reload_cache(self) -> None:
        """Refresh active groups and global enabled toggle from SQLite into memory."""
        self._global_enabled = bool(await self._db.get_setting("global_enabled", True))
        groups = await self._db.list_groups(only_enabled=True)
        self._enabled_groups = {g.chat_id: g for g in groups}
        logger.info(
            "Userbot cache reloaded: global_enabled=%s, active_groups=%d",
            self._global_enabled,
            len(self._enabled_groups),
        )

    async def start_from_storage(self) -> bool:
        """Auto-connect userbot on startup if credentials & StringSession exist in DB."""
        await self.reload_cache()
        # A phone-login attempt cannot survive a process restart (its auth key is
        # gone), so clear it instead of letting the wizard show a dead attempt.
        if await self.drop_stale_pending_login():
            logger.info("Discarded a login attempt left over from the previous run.")
        api_id = await self._db.get_setting("tg_api_id")
        api_hash = await self._db.get_setting("tg_api_hash")
        session_str = await self._db.get_setting("tg_session")

        if not (api_id and api_hash and session_str):
            logger.info("No saved Telethon session yet; waiting for in-bot setup wizard.")
            return False

        if not isinstance(api_id, int):
            try:
                api_id = int(str(api_id))
            except (TypeError, ValueError):
                logger.error("Stored tg_api_id is not numeric; cannot restore session.")
                return False

        try:
            client = make_login_client(api_id, str(api_hash), session=StringSession(str(session_str)))
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning(
                    "Saved Telethon session is no longer authorized (it may have been revoked "
                    "or logged out elsewhere). Re-run /start to log in again."
                )
                await client.disconnect()
                return False
            await self._attach_client(client)
            return True
        except Exception as exc:
            logger.exception("Failed to start userbot from stored session: %s", exc)
            return False

    async def stop(self) -> None:
        await self._stop_admin_refresher()
        await self._login.close()
        await self._qr.close()
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
        self._me_id = None

    async def logout(self) -> None:
        """Log out and clear the stored session from SQLite."""
        await self._stop_admin_refresher()
        await self.cancel_pending_login()
        if self._client is not None:
            try:
                await self._client.log_out()
            except Exception:
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
            self._client = None

        self._me_id = None
        self._me_name = None
        self._me_username = None
        self._admin_cache.clear()
        await self._db.delete_setting("tg_session")
        await self._db.delete_setting("tg_account_name")
        await self._db.delete_setting("tg_account_username")
        await self._db.delete_setting("tg_account_id")

    # ------------------------------------------------------------------
    # Interactive In-Bot Telegram Login Flow
    # ------------------------------------------------------------------

    @property
    def last_code_delivery(self) -> CodeDelivery | None:
        """Where Telegram actually delivered the latest login code (for the wizard UI)."""
        pending = self._login.pending
        return pending.delivery if pending is not None else None

    @property
    def pending_phone(self) -> str | None:
        """The normalised E.164 number of the in-progress login, if any."""
        pending = self._login.pending
        return pending.phone if pending is not None else None

    async def _persist_pending_login(self, snapshot: dict[str, object] | None) -> None:
        await self._db.set_setting("tg_pending_login", snapshot)

    async def _clear_pending_login(self) -> None:
        await self._db.delete_setting("tg_pending_login")

    async def drop_stale_pending_login(self) -> bool:
        """Clear a login attempt left over from a previous process run.

        Telegram's ``phone_code_hash`` is tied to the temporary client's auth
        key, so after a restart the saved attempt can never be completed. The
        old code left the owner staring at "Requesting login code..." with no
        way forward; now the bot asks for the number again on /start.
        """
        if not await self._db.get_setting("tg_pending_login"):
            return False
        await self._clear_pending_login()
        return True

    @property
    def pending_code_length(self) -> int | None:
        pending = self._login.pending
        return pending.code_length if pending is not None else None

    async def cancel_pending_login(self) -> None:
        await self._login.close()
        await self._qr.close()

    async def start_phone_login(self, api_id: int, api_hash: str, phone: str) -> LoginResult:
        """Step 1 of login: validate the number, request the OTP and report the delivery channel."""
        return await self._login.request_code(int(api_id), str(api_hash), phone)

    async def resend_phone_code(self) -> LoginResult:
        """Request another code without throwing away a still-valid login attempt."""
        return await self._login.resend_code()

    async def submit_otp_code(self, code: str) -> LoginResult:
        """Step 2 of login: verify OTP digits entered via inline keypad or spaced text."""
        result = await self._login.submit_code(code)
        if result.status is LoginStatus.OK:
            return await self._finalize_pending_login()
        return result

    async def submit_2fa_password(self, password: str) -> LoginResult:
        """Step 3 of login (optional): verify Telegram cloud password."""
        result = await self._login.submit_password(password)
        if result.status is LoginStatus.OK:
            return await self._finalize_pending_login()
        return result

    @property
    def email_setup_required(self) -> bool:
        """Telegram is blocking code delivery until a login email is set up."""
        pending = self._login.pending
        return bool(pending and pending.email_setup_required)

    async def submit_login_email(self, email: str) -> LoginResult:
        """Send Telegram the login email it demands before it will deliver a code."""
        return await self._login.send_login_email_code(email)

    async def submit_login_email_code(self, code: str) -> LoginResult:
        """Verify the emailed setup code; afterwards the real login code is delivered."""
        return await self._login.submit_login_email_code(code)

    async def use_login_email(self, email: str) -> LoginResult:
        """Route the login code to an inbox — the channel that actually delivers."""
        return await self._login.use_login_email(email)

    async def submit_email_login_code(self, code: str) -> LoginResult:
        """Verify a code that Telegram emailed as the login code; finishes the login."""
        result = await self._login.submit_email_login_code(code)
        if result.status is LoginStatus.OK:
            return await self._finalize_pending_login()
        return result

    @property
    def pending_email_length(self) -> int | None:
        pending = self._login.pending
        return pending.email_setup_length if pending is not None else None

    # -- QR login -------------------------------------------------------
    async def start_qr_login(self, api_id: int, api_hash: str) -> tuple[LoginResult, str | None]:
        result, url = await self._qr.start(int(api_id), str(api_hash))
        if result.status is LoginStatus.OK:
            return await self._finalize_qr_login(), url
        return result, url

    def qr_login_status(self) -> tuple[LoginStatus, str]:
        state = self._qr.poll()
        if state is None:
            return LoginStatus.ERROR, "No QR login in progress."
        return state.status, state.message

    def qr_snapshot(self) -> dict[str, object] | None:
        """Current QR state for the UI (url, generation counter, seconds left).

        ``generation`` increases every time Telegram's short-lived token is
        renewed, which tells the wizard its on-screen image is stale.
        """
        state = self._qr.poll()
        if state is None:
            return None
        return {
            "url": state.url,
            "generation": state.generation,
            "refreshes": state.refreshes,
            "seconds_left": state.seconds_left,
            "status": state.status,
            "message": state.message,
        }

    def qr_code(self) -> bytes | None:
        """Render the *current* token as a PNG, so the image can be swapped in place."""
        state = self._qr.poll()
        if state is None:
            return None
        return render_qr_png(state.url)

    async def submit_qr_2fa(self, password: str) -> LoginResult:
        result = await self._qr.submit_password(password)
        if result.status is LoginStatus.OK:
            return await self._finalize_qr_login()
        return result

    async def _finalize_qr_login(self) -> LoginResult:
        pending = self._qr.take_authorized_client()
        if pending is None:
            return LoginResult(LoginStatus.ERROR, "QR login session was lost. Start again.")
        session_str = pending.client.session.save()
        await self._db.set_setting("tg_api_id", pending.api_id)
        await self._db.set_setting("tg_api_hash", pending.api_hash)
        await self._db.set_setting("tg_session", session_str)
        await self._attach_client(pending.client)
        logger.info("Telegram account connected via QR login: %s", self.account_display)
        return LoginResult(LoginStatus.OK, f"Successfully connected as {self.account_display}!")

    async def adopt_browser_login(self, manager: object) -> LoginResult:
        """Take over a session approved on the browser login page.

        The page drives its own Telethon connection (Telegram binds the login
        token to the connection that asked for it), so once it is approved the
        authorized client is transferred here and persisted exactly like a QR or
        phone login.
        """
        take = getattr(manager, "take_authorized_client", None)
        pending = take() if callable(take) else None
        if pending is None:
            return LoginResult(
                LoginStatus.ERROR,
                "The login was not approved yet. Approve it in Telegram, then tap "
                "🔄 I Approved — Continue.",
            )

        session_str = pending.client.session.save()
        await self._db.set_setting("tg_api_id", pending.api_id)
        await self._db.set_setting("tg_api_hash", pending.api_hash)
        await self._db.set_setting("tg_session", session_str)
        await self._attach_client(pending.client)
        logger.info("Telegram account connected via browser login: %s", self.account_display)
        return LoginResult(LoginStatus.OK, f"Successfully connected as {self.account_display}!")

    async def finish_qr_login_if_scanned(self) -> LoginResult | None:
        """Called by the wizard's refresh button once the phone approved the QR."""
        status, message = self.qr_login_status()
        if status is LoginStatus.OK:
            return await self._finalize_qr_login()
        if status is LoginStatus.ERROR:
            return LoginResult(LoginStatus.ERROR, message)
        return None

    async def cancel_qr_login(self) -> None:
        await self._qr.close()

    async def login_with_string_session(
        self, api_id: int, api_hash: str, session_str: str
    ) -> LoginResult:
        """Alternative login: paste an existing Telethon StringSession."""
        await self.cancel_pending_login()
        session = StringSession(session_str.strip())
        client = make_login_client(int(api_id), api_hash, session=session)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                return LoginResult(
                    LoginStatus.ERROR,
                    "That StringSession is not authorized or has expired. "
                    "Generate a fresh one, or use the phone + code login instead.",
                )

            await self._db.set_setting("tg_api_id", int(api_id))
            await self._db.set_setting("tg_api_hash", api_hash.strip())
            await self._db.set_setting("tg_session", session_str.strip())
            await self._attach_client(client)
            return LoginResult(LoginStatus.OK, f"Connected as {self.account_display}!")
        except Exception as exc:
            try:
                await client.disconnect()
            except Exception:
                pass
            return LoginResult(
                LoginStatus.ERROR, translate_send_error(exc, "your account").message
            )

    async def _finalize_pending_login(self) -> LoginResult:
        p = self._login.take_authorized_client()
        if p is None:
            return LoginResult(LoginStatus.ERROR, "Login session was lost. Please start again.")

        session_str = p.client.session.save()
        await self._db.set_setting("tg_api_id", p.api_id)
        await self._db.set_setting("tg_api_hash", p.api_hash)
        await self._db.set_setting("tg_session", session_str)

        await self._attach_client(p.client)
        logger.info("Telegram account connected via phone login: %s", self.account_display)
        return LoginResult(LoginStatus.OK, f"Successfully connected as {self.account_display}!")

    async def _attach_client(self, client: TelegramClient) -> None:
        # Disconnect any previously active client cleanly
        if self._client is not None and self._client is not client:
            try:
                await self._client.disconnect()
            except Exception:
                pass

        self._client = client
        me = await client.get_me()
        self._me_id = me.id
        full_name = " ".join(part for part in (me.first_name, me.last_name) if part).strip()
        self._me_name = full_name or "Telegram User"
        self._me_username = me.username

        await self._db.set_setting("tg_account_id", self._me_id)
        await self._db.set_setting("tg_account_name", self._me_name)
        await self._db.set_setting("tg_account_username", self._me_username)

        # Register the single unified NewMessage handler
        client.add_event_handler(self._on_new_message, events.NewMessage(incoming=True))
        await self.reload_cache()
        logger.info("Userbot attached and listening as %s (id=%s)", self.account_display, self._me_id)

        # Pre-warm admin caches in the background for enabled groups, then keep
        # them warm off the hot path.
        asyncio.create_task(self._prewarm_admin_caches())
        self._start_admin_refresher()

    # ------------------------------------------------------------------
    # Group Sync & Admin Caching
    # ------------------------------------------------------------------

    async def sync_groups(self, limit: int = 200) -> list[GroupConfig]:
        """Fetch joined Telegram groups & supergroups and upsert metadata into SQLite."""
        if not self.is_connected or self._client is None:
            raise RuntimeError("Userbot is not connected.")

        seen_ids: set[int] = set()
        async for dialog in self._client.iter_dialogs(limit=limit):
            if not dialog.is_group:
                continue
            chat_id = int(dialog.id)
            if chat_id in seen_ids:
                continue
            seen_ids.add(chat_id)
            title = (dialog.name or f"Group {chat_id}").strip()
            username = getattr(dialog.entity, "username", None)
            await self._db.upsert_group_metadata(chat_id, title, username)

        await self.reload_cache()
        return await self._db.list_groups()

    async def refresh_group_admins(self, chat_id: int) -> set[int]:
        """Fetch and cache admin user IDs for a group to keep giveaway checks O(1).

        ``iter_participants(filter=ChannelParticipantsAdmins)`` only works for
        channels/supergroups, and needs the account to be allowed to list
        members. It raises for basic groups and for groups where member listing
        is restricted. Previously that failure was logged at DEBUG (invisible in
        Render's INFO logs) and returned an empty set, which — combined with the
        default ``admins`` sender filter — silently dropped *every* message.
        Now failures are recorded as *unverified* so the caller can fail open.
        """
        if not self.is_connected or self._client is None:
            self._cache_admins(chat_id, set(), verified=False)
            return set()

        admin_ids: set[int] = set()
        verified = False
        try:
            async for user in self._client.iter_participants(
                chat_id, filter=ChannelParticipantsAdmins
            ):
                if getattr(user, "id", None):
                    admin_ids.add(int(user.id))
            verified = True
        except Exception as exc:
            logger.warning(
                "Could not list admins for chat %s (%s: %s). Admin-only filtering will "
                "fail open for this group until it can be fetched.",
                chat_id,
                type(exc).__name__,
                exc,
            )
            # Retry without the filter for basic groups (Chat), where the
            # ChannelParticipantsAdmins filter is not valid.
            try:
                participants = await self._client.get_participants(chat_id, limit=200)
                admin_ids = {
                    int(p.id)
                    for p in participants
                    if getattr(p, "id", None) and getattr(p, "participant", None) is not None
                }
                verified = bool(admin_ids)
            except Exception as exc2:
                logger.debug("Fallback participant fetch failed for %s: %s", chat_id, exc2)

        self._cache_admins(chat_id, admin_ids, verified=verified)
        return admin_ids

    async def _refresh_admins_quietly(self, chat_id: int) -> None:
        """Refresh an admin list in the background, one scan per group at a time."""
        if chat_id in self._admin_refreshing:
            return
        self._admin_refreshing.add(chat_id)
        try:
            await self.refresh_group_admins(chat_id)
        except Exception as exc:  # never let a background task raise
            logger.debug("Background admin refresh failed for %s: %s", chat_id, exc)
        finally:
            self._admin_refreshing.discard(chat_id)

    def _cache_admins(self, chat_id: int, admin_ids: set[int], *, verified: bool) -> None:
        self._admin_cache[chat_id] = (time.monotonic(), admin_ids, verified)

    async def _admin_refresher_loop(self) -> None:
        """Refresh admin lists in the background, one group at a time.

        Previously the *message handler* refreshed the list inline whenever the
        10-minute TTL expired. That is a full Telegram participant scan - the
        single slowest thing in the pre-AI path - and because every enabled
        group expired together, the first question after each window was
        answered seconds late. Refreshing off the hot path removes that stall.
        """
        while True:
            try:
                await asyncio.sleep(self.ADMIN_REFRESH_INTERVAL)
                for chat_id, group in list(self._enabled_groups.items()):
                    if group.sender_filter != "admins":
                        continue
                    entry = self._admin_cache.get(chat_id)
                    if entry is not None and (time.monotonic() - entry[0]) < self.ADMIN_CACHE_TTL / 2:
                        continue
                    await self.refresh_group_admins(chat_id)
                    # Space the scans out so a big group list never hammers the API.
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the responder alive no matter what
                logger.debug("Admin cache refresher error: %s", exc)

    def _start_admin_refresher(self) -> None:
        if self._admin_refresher is not None and not self._admin_refresher.done():
            return
        self._admin_refresher = asyncio.create_task(self._admin_refresher_loop())

    async def _stop_admin_refresher(self) -> None:
        task, self._admin_refresher = self._admin_refresher, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _prewarm_admin_caches(self) -> None:
        for chat_id, group in list(self._enabled_groups.items()):
            if group.sender_filter == "admins":
                admins = await self.refresh_group_admins(chat_id)
                entry = self._admin_cache.get(chat_id)
                verified = bool(entry and entry[2])
                logger.info(
                    "Admin cache for '%s': %d admins (%s)",
                    group.title,
                    len(admins),
                    "verified" if verified else "UNVERIFIED - admin filter will fail open",
                )
                await asyncio.sleep(0.2)

    async def _is_admin(self, chat_id: int, event: events.NewMessage.Event) -> bool:
        sender_id = event.sender_id
        # Anonymous group admins post as the group itself (from_id is None or PeerChannel == chat)
        if sender_id is None or sender_id == chat_id:
            return True
        if isinstance(event.message.from_id, PeerChannel):
            # Channel/anonymous admin posting inside the supergroup
            if int(f"-100{event.message.from_id.channel_id}") == chat_id:
                return True

        cached = self._admin_cache.get(chat_id)
        now = time.monotonic()
        if cached is None:
            # First sighting of this group: there is nothing to decide with, so
            # a one-off fetch is unavoidable.
            await self.refresh_group_admins(chat_id)
            entry = self._admin_cache.get(chat_id)
            admins = entry[1] if entry else set()
            verified = bool(entry and entry[2])
        else:
            admins = cached[1]
            verified = cached[2]
            if (now - cached[0]) > self.ADMIN_CACHE_TTL:
                # Stale list: answer with what we already have and refresh in
                # the background. Blocking here is what made a giveaway answer
                # arrive seconds late once every 10 minutes.
                asyncio.create_task(self._refresh_admins_quietly(chat_id))

        if int(sender_id) in admins:
            return True

        if not verified:
            # We could not determine who the admins are (basic group, restricted
            # member list, temporary API error). Refusing the message here is what
            # made the userbot look completely dead, so fail open and answer.
            logger.info(
                "Admin list unavailable for chat %s; allowing message from %s through "
                "(fail-open).",
                chat_id,
                sender_id,
            )
            return True

        return False

    # ------------------------------------------------------------------
    # Real-Time Group Q&A Handler
    # ------------------------------------------------------------------

    #: Hard cap so a huge photo can never stall the responder or the AI bill.
    MAX_IMAGE_BYTES = 4 * 1024 * 1024

    #: Image formats that are decoration by nature: Telegram draws stickers as
    #: ``application/x-tgsticker`` (animated) or ``image/webp`` (static), and
    #: neither carries a legible question.
    _NON_QUESTION_IMAGE_MIMES = frozenset({"application/x-tgsticker", "image/gif"})

    @staticmethod
    def _message_has_image(event: events.NewMessage.Event) -> bool:
        """True when the message carries a real photo or image document."""
        return UserbotManager._payload_has_image(getattr(event, "message", None))

    @staticmethod
    def _payload_has_image(message: object) -> bool:
        """True when a message object carries a real photo or image document.

        The types are checked explicitly rather than using truthiness: Telegram
        sets ``photo=None``/``document=None`` for plain text, and a loose check
        would misclassify text messages as images.

        Takes the message rather than the event so a *replied-to* message can be
        checked the same way: the poster is usually the message being replied to,
        not the reply.
        """
        if message is None:
            return False
        if isinstance(getattr(message, "photo", None), MessageMediaPhoto | Photo):
            return True
        document = getattr(message, "document", None)
        if isinstance(document, Document):
            mime = str(getattr(document, "mime_type", "") or "").lower()
            # Stickers and animated emoji are image/* documents too, but they are
            # reaction-sized artwork: there is never a question inside one, and
            # now that a short caption beside an image is treated as a real photo
            # question, a "🔥🔥" caption on a sticker would otherwise be sent to a
            # vision model on every reaction. Only the still vector formats are
            # excluded - a WebP *photo* exported from a design tool is a
            # perfectly normal way to post a giveaway card.
            if mime in UserbotManager._NON_QUESTION_IMAGE_MIMES:
                return False
            return mime.startswith("image/")
        return False

    #: How far back a replied-to message is fetched. One level only: the poster a
    #: member answers directly. Chasing a chain of replies would happily walk up
    #: through unrelated conversation and answer an old card nobody is asking about.
    async def _replied_image_source(self, event: events.NewMessage.Event, group: GroupConfig):
        """The message being replied to, when it is a photo worth reading.

        Returns None when the reply has no target, the target is not an image, or
        the group has photo answers switched off - so a group that never wanted
        photos answered never fetches one either.
        """
        if not getattr(group, "vision_enabled", True):
            return None
        reply_to = getattr(getattr(event, "message", None), "reply_to_msg_id", None)
        if not reply_to:
            return None
        try:
            target = await event.get_reply_message()
        except Exception as exc:  # noqa: BLE001 - a missing target is not fatal
            logger.debug("Could not load the replied-to message: %s", exc)
            return None
        if target is None or not self._payload_has_image(target):
            return None
        return target

    async def _download_image(self, event: events.NewMessage.Event) -> tuple[bytes | None, str]:
        """Download the photo for vision models, rejecting anything too large."""
        return await self._download_message_image(getattr(event, "message", None), event)

    async def _download_message_image(
        self, message: object, source: object
    ) -> tuple[bytes | None, str]:
        """Download an image from ``message``, using ``source`` to fetch bytes.

        Split from :meth:`_download_image` so a *replied-to* message can supply
        the picture: the file belongs to that message, but the client call that
        fetches it is the same.
        """
        mime = "image/jpeg"
        document = getattr(message, "document", None)
        if isinstance(document, Document) and str(
            getattr(document, "mime_type", "") or ""
        ).startswith("image/"):
            mime = str(document.mime_type)
        try:
            size = getattr(getattr(message, "file", None), "size", None)
            if size and size > self.MAX_IMAGE_BYTES:
                logger.info("Skipping photo: %d bytes exceeds the size cap", size)
                return None, mime
            data = await source.download_media(file=bytes)
            if not data:
                return None, mime
            payload = bytes(data)
            if len(payload) > self.MAX_IMAGE_BYTES:
                logger.info("Skipping photo: %d bytes exceeds the size cap", len(payload))
                return None, mime
            return payload, mime
        except Exception as exc:
            logger.warning("Could not download photo for vision: %s", exc)
            return None, mime

    def _record(self, chat_id: int | None, sender_id: int | None, text: str, outcome: str,
                detail: str = "") -> None:
        """Keep a short in-memory trail of why a message was or was not answered."""
        self._last_decisions.append(
            {
                "chat_id": chat_id,
                "sender_id": sender_id,
                "text": (text or "")[:80],
                "outcome": outcome,
                "detail": detail,
                "at": time.strftime("%H:%M:%S"),
            }
        )

    @property
    def recent_decisions(self) -> list[dict[str, object]]:
        return list(self._last_decisions)

    @property
    def counters(self) -> dict[str, int]:
        return {
            "messages_seen": self._seen_count,
            "answers_sent": self._answered_count,
            "active_groups": len(self._enabled_groups),
        }

    async def _answer_first_comment(
        self,
        event: events.NewMessage.Event,
        group: GroupConfig,
        chat_id: int,
        text: str,
        dedup_key: tuple[int, int],
    ) -> None:
        """Post the fixed "I win" line. No model, no prompt, no API key.

        Kept separate from the AI path so the hot path is a dict lookup, a regex
        and one Telethon send - the same shape as the local math solver.
        """
        if group.reply_to_message:
            send = event.reply
        else:
            send = event.respond
        self._recent_msg_ids.append(dedup_key)
        self._last_reply_ts[int(chat_id)] = time.monotonic()
        try:
            await send(FIRST_COMMENT_ANSWER)
        except FloodWaitError as exc:
            logger.warning("FloodWaitError (%ss) on first-comment reply in %s", exc.seconds, group.title)
            return
        except (ChatWriteForbiddenError, UserBannedInChannelError):
            logger.warning("Cannot write in group %s; disabling it.", group.title)
            group.enabled = False
            await self._db.save_group(group)
            await self.reload_cache()
            return
        except Exception as exc:  # noqa: BLE001 - never take the userbot down
            logger.exception("Failed sending first-comment reply in %s: %s", group.title, exc)
            return

        self._answered_count += 1
        self._record(
            chat_id,
            event.sender_id,
            text,
            "answered",
            "first_comment (local, no AI)",
        )
        await self._db.log_answer(
            chat_id=int(chat_id),
            chat_title=group.title,
            question_text=text,
            answer_text=FIRST_COMMENT_ANSWER,
            provider="local",
            model="first-comment",
            latency_ms=0,
        )

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        if not self._global_enabled:
            self._record(event.chat_id, event.sender_id, event.raw_text, "skip:global_paused")
            return
        if event.out or event.is_private:
            return

        chat_id = event.chat_id
        if chat_id is None:
            return

        self._seen_count += 1

        group = self._enabled_groups.get(int(chat_id))
        if group is None or not group.enabled:
            self._record(
                chat_id,
                event.sender_id,
                event.raw_text,
                "skip:group_not_enabled",
                f"chat not in the enabled list ({len(self._enabled_groups)} enabled)",
            )
            return

        text = (event.raw_text or "").strip()

        # Giveaway questions are decorated with emoji and invisible bidi marks.
        # Normalise before the filter, so "🎯 47 * 83 ✅" is arithmetic rather
        # than "unparseable text", and send the model the words, not the noise.
        clean_text = prepare_incoming(text)

        # A photo is content too: giveaway questions are often a screenshot or a
        # picture of the question. Only text-less messages with no image are useless.
        has_image = self._message_has_image(event)
        # ...and a picture can be one that was posted earlier. The common shape in
        # a giveaway is the host posting a card and members answering *under it*:
        # "What is the answer" as a reply, with no image of their own. Read on its
        # own, that reply is a caption pointing at nothing, and the photo it is
        # answering is one tap away in the reply. Resolving it here means the
        # reply is judged as a photo question, which it is.
        replied = None
        if not has_image and wants_inherited_image(clean_text):
            replied = await self._replied_image_source(event, group)
            if replied is not None:
                has_image = True
        if not text.strip() and not has_image:
            self._record(chat_id, event.sender_id, "", "skip:no_text")
            return

        # Keep rolling message context if enabled for this group
        if group.context_messages > 0:
            buf = self._recent_context.setdefault(int(chat_id), deque(maxlen=5))
        else:
            buf = None

        # Deduplicate message IDs
        dedup_key = (int(chat_id), int(event.id))
        if dedup_key in self._recent_msg_ids:
            return

        # Ignore own messages defensively
        if self._me_id is not None and event.sender_id == self._me_id:
            return

        # Sender filter check (admins / humans / all)
        if group.sender_filter == "admins":
            if not await self._is_admin(int(chat_id), event):
                if buf is not None:
                    buf.append(text[:200])
                self._record(
                    chat_id,
                    event.sender_id,
                    text,
                    "skip:not_admin",
                    "sender is not an admin and admins WERE verified",
                )
                return
        elif group.sender_filter == "humans":
            sender = await event.get_sender()
            if isinstance(sender, User) and sender.bot:
                self._record(chat_id, event.sender_id, text, "skip:is_bot")
                return

        # "First comment" race: recognised and answered in-process, exactly like
        # the local math solver. This has to run before the pre-filter (a bare
        # "first comment" has no question word and would be dropped) and before
        # any AI call (a 400ms model round-trip loses the race outright), and it
        # deliberately ignores the per-group cooldown: this is a one-shot burst,
        # and a cooldown would silently skip the message that wins it.
        if group.first_comment_enabled and not has_image and is_first_comment(clean_text):
            await self._answer_first_comment(event, group, chat_id, text, dedup_key)
            return

        # Local pre-filter (runs in <0.1ms before any AI call)
        decision = should_evaluate_message(clean_text, group, has_image=has_image)
        if not decision.should_process:
            if buf is not None:
                buf.append(clean_text[:200])
            self._record(
                chat_id,
                event.sender_id,
                text,
                "skip:prefilter",
                f"{decision.reason} -> {describe_reason(decision.reason)} "
                f"(detection={group.detection_mode})",
            )
            return

        # Per-group cooldown check
        now = time.monotonic()
        last_ts = self._last_reply_ts.get(int(chat_id), 0.0)
        if group.cooldown_seconds > 0 and (now - last_ts) < group.cooldown_seconds:
            self._record(chat_id, event.sender_id, text, "skip:cooldown")
            return

        # Gather optional short context before appending current message
        context_lines = list(buf)[-group.context_messages :] if (buf and group.context_messages > 0) else None
        if buf is not None:
            buf.append(clean_text[:200])

        # Load active AI settings in two queries rather than eight. On every
        # answered message this used to be ~8 serialised SQLite round-trips
        # before the request could even be built.
        provider_keys = await self._db.get_all_provider_keys()
        settings = await self._db.get_settings(
            (
                "primary_provider",
                "primary_model",
                "fallback_provider",
                "fallback_model",
                "ai_temperature",
                "ai_max_tokens",
                "ai_speed_mode",
            )
        )
        primary_provider = str(settings.get("primary_provider", "groq"))
        default_model = PROVIDERS.get(primary_provider, PROVIDERS["groq"]).default_model
        primary_model = str(settings.get("primary_model", default_model))
        fallback_provider = settings.get("fallback_provider")
        fallback_model = settings.get("fallback_model")
        temperature = float(settings.get("ai_temperature", 0.1))
        max_tokens = int(settings.get("ai_max_tokens", 120))
        speed_mode = str(settings.get("ai_speed_mode", DEFAULT_SPEED_MODE))

        # Fetch the photo (bounded) only after every cheap gate has passed.
        image_bytes: bytes | None = None
        mime = "image/jpeg"
        if has_image:
            if self._message_has_image(event):
                image_bytes, mime = await self._download_image(event)
            else:
                assert replied is not None  # implied by `has_image` above
                image_bytes, mime = await self._download_message_image(replied, event)
            if image_bytes is None:
                self._record(
                    chat_id,
                    event.sender_id,
                    text,
                    "skip:image_unavailable",
                    "could not download or the photo exceeded the size cap",
                )
                return

        result: AnswerResult = await self._ai.answer_question(
            text=clean_text,
            forex_group=group.forex_enabled,
            live_data=group.forex_live_data,
            answer_style=group.answer_style,
            primary_provider=primary_provider,
            primary_model=primary_model,
            provider_keys=provider_keys,
            fallback_provider=str(fallback_provider) if fallback_provider else None,
            fallback_model=str(fallback_model) if fallback_model else None,
            local_math_enabled=group.local_math_enabled,
            context_lines=context_lines,
            temperature=temperature,
            max_tokens=max_tokens,
            image=image_bytes,
            image_mime=mime,
            speed_mode=speed_mode,
        )

        if result.error:
            logger.warning("AI error in group '%s': %s", group.title, result.error)
            self._record(chat_id, event.sender_id, text, "skip:ai_error", str(result.error))
            return

        if not result.answered or not result.answer:
            self._record(
                chat_id,
                event.sender_id,
                text,
                "skip:ai_declined",
                "the model judged this not to be a question (SKIP)",
            )
            return

        # Optional human-like delay (0ms by default for maximum giveaway speed)
        if group.human_delay_ms > 0:
            await asyncio.sleep(group.human_delay_ms / 1000.0)

        # Mark dedup & cooldown immediately before sending to prevent race conditions
        self._recent_msg_ids.append(dedup_key)
        self._last_reply_ts[int(chat_id)] = time.monotonic()

        try:
            if group.reply_to_message:
                await event.reply(result.answer)
            else:
                await event.respond(result.answer)
        except FloodWaitError as exc:
            logger.warning("FloodWaitError (%ss) replying in %s", exc.seconds, group.title)
            if self._notify_owner:
                safe_title = html.escape(group.title)
                await self._notify_owner(
                    f"⚠️ <b>Telegram FloodWait ({exc.seconds}s)</b> in <b>{safe_title}</b>. "
                    "Consider increasing cooldown slightly."
                )
            return
        except (ChatWriteForbiddenError, UserBannedInChannelError):
            logger.warning("Cannot write in group %s; disabling it.", group.title)
            group.enabled = False
            await self._db.save_group(group)
            await self.reload_cache()
            if self._notify_owner:
                safe_title = html.escape(group.title)
                await self._notify_owner(
                    f"🚫 Cannot send messages in <b>{safe_title}</b>. "
                    "I automatically disabled monitoring for this group."
                )
            return
        except Exception as exc:
            logger.exception("Failed sending reply in %s: %s", group.title, exc)
            return

        self._answered_count += 1
        self._record(
            chat_id,
            event.sender_id,
            text,
            "answered",
            f"{result.provider}/{result.model} in {result.latency_ms}ms",
        )

        # Record log in SQLite
        await self._db.log_answer(
            chat_id=int(chat_id),
            chat_title=group.title,
            question_text=text,
            answer_text=result.answer,
            provider=result.provider,
            model=result.model,
            latency_ms=result.latency_ms,
        )

        # Optional live owner notification in dashboard chat
        if bool(await self._db.get_setting("notify_on_answer", False)) and self._notify_owner:
            fb_tag = " (fallback)" if result.used_fallback else ""
            q_preview = html.escape(text[:120] + ("..." if len(text) > 120 else ""))
            safe_title = html.escape(group.title)
            safe_ans = html.escape(result.answer)
            await self._notify_owner(
                f"⚡ <b>Answered in {safe_title}</b>\n"
                f"❓ <i>{q_preview}</i>\n"
                f"💬 <code>{safe_ans}</code>\n"
                f"🤖 {html.escape(result.provider)}{fb_tag} · {result.latency_ms}ms"
            )
