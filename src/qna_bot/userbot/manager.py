"""Telethon Userbot lifecycle, in-bot interactive login, group sync, and Q&A responder."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from telethon import TelegramClient, events
from telethon.errors import (
    ApiIdInvalidError,
    ChatWriteForbiddenError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
    UserBannedInChannelError,
)
from telethon.sessions import StringSession
from telethon.tl.types import ChannelParticipantsAdmins, PeerChannel, User

from ..ai.engine import AIEngine, AnswerResult
from ..ai.providers import PROVIDERS
from ..storage.database import Database, GroupConfig
from .filters import should_evaluate_message

logger = logging.getLogger(__name__)

NotifyCallback = Callable[[str], Awaitable[None]]


@dataclass
class PendingLogin:
    client: TelegramClient
    api_id: int
    api_hash: str
    phone: str
    phone_code_hash: str


@dataclass
class LoginResult:
    status: str  # "ok" | "code_sent" | "2fa_required" | "error"
    message: str


class UserbotManager:
    """Manages the connected Telegram user account (Telethon) and real-time Q&A pipeline."""

    ADMIN_CACHE_TTL = 600.0  # 10 minutes

    def __init__(self, db: Database, ai_engine: AIEngine) -> None:
        self._db = db
        self._ai = ai_engine
        self._client: TelegramClient | None = None
        self._pending_login: PendingLogin | None = None
        self._me_id: int | None = None
        self._me_name: str | None = None
        self._me_username: str | None = None

        # Fast in-memory runtime caches for sub-millisecond giveaway handling
        self._enabled_groups: dict[int, GroupConfig] = {}
        self._global_enabled: bool = True
        self._admin_cache: dict[int, tuple[float, set[int]]] = {}
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
        return self._pending_login is not None

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
        api_id = await self._db.get_setting("tg_api_id")
        api_hash = await self._db.get_setting("tg_api_hash")
        session_str = await self._db.get_setting("tg_session")

        if not (api_id and api_hash and session_str):
            logger.info("No saved Telethon session yet; waiting for in-bot setup wizard.")
            return False

        try:
            client = TelegramClient(StringSession(str(session_str)), int(api_id), str(api_hash))
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning("Saved Telethon session is no longer authorized.")
                await client.disconnect()
                return False
            await self._attach_client(client)
            return True
        except Exception as exc:
            logger.exception("Failed to start userbot from stored session: %s", exc)
            return False

    async def stop(self) -> None:
        await self.cancel_pending_login()
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
        self._me_id = None

    async def logout(self) -> None:
        """Log out and clear the stored session from SQLite."""
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

    async def cancel_pending_login(self) -> None:
        if self._pending_login is not None:
            try:
                await self._pending_login.client.disconnect()
            except Exception:
                pass
            self._pending_login = None

    async def start_phone_login(self, api_id: int, api_hash: str, phone: str) -> LoginResult:
        """Step 1 of login: connect temporary Telethon client and request OTP code."""
        await self.cancel_pending_login()
        cleaned_phone = phone.strip().replace(" ", "").replace("-", "")
        client = TelegramClient(StringSession(), int(api_id), api_hash.strip())
        try:
            await client.connect()
            sent = await client.send_code_request(cleaned_phone)
            self._pending_login = PendingLogin(
                client=client,
                api_id=int(api_id),
                api_hash=api_hash.strip(),
                phone=cleaned_phone,
                phone_code_hash=sent.phone_code_hash,
            )
            return LoginResult("code_sent", f"Login code sent to {cleaned_phone}.")
        except ApiIdInvalidError:
            await client.disconnect()
            return LoginResult("error", "Invalid API ID or API Hash. Check my.telegram.org.")
        except PhoneNumberInvalidError:
            await client.disconnect()
            return LoginResult(
                "error",
                "Invalid phone number. Include country code (e.g. +251912345678 or +15551234567).",
            )
        except FloodWaitError as exc:
            await client.disconnect()
            return LoginResult(
                "error", f"Telegram rate limit: please wait {exc.seconds} seconds before retrying."
            )
        except Exception as exc:
            await client.disconnect()
            return LoginResult("error", f"Could not send login code: {exc}")

    async def resend_phone_code(self) -> LoginResult:
        if self._pending_login is None:
            return LoginResult("error", "No login in progress. Start login again.")
        p = self._pending_login
        return await self.start_phone_login(p.api_id, p.api_hash, p.phone)

    async def submit_otp_code(self, code: str) -> LoginResult:
        """Step 2 of login: verify OTP digits entered via inline keypad or spaced text."""
        if self._pending_login is None:
            return LoginResult("error", "No active login session. Please enter your phone again.")

        digits = "".join(ch for ch in code if ch.isdigit())
        if len(digits) < 4:
            return LoginResult("error", "Code looks too short. Enter all digits sent by Telegram.")

        p = self._pending_login
        try:
            await p.client.sign_in(
                phone=p.phone,
                code=digits,
                phone_code_hash=p.phone_code_hash,
            )
        except SessionPasswordNeededError:
            return LoginResult(
                "2fa_required",
                "Two-Step Verification (2FA) is enabled on your account. Please send your 2FA password.",
            )
        except PhoneCodeInvalidError:
            return LoginResult("error", "Incorrect login code. Please clear and try again.")
        except PhoneCodeExpiredError:
            return LoginResult(
                "error",
                "That login code expired (or Telegram invalidated it if sent as plain text). "
                "Tap Resend Code and use the keypad buttons!",
            )
        except FloodWaitError as exc:
            return LoginResult("error", f"Telegram flood wait: please wait {exc.seconds}s.")
        except Exception as exc:
            return LoginResult("error", f"Sign-in failed: {exc}")

        return await self._finalize_pending_login()

    async def submit_2fa_password(self, password: str) -> LoginResult:
        """Step 3 of login (optional): verify Telegram cloud password."""
        if self._pending_login is None:
            return LoginResult("error", "No active login session. Please restart login.")
        p = self._pending_login
        try:
            await p.client.sign_in(password=password)
        except PasswordHashInvalidError:
            return LoginResult("error", "Incorrect 2FA password. Please try again.")
        except FloodWaitError as exc:
            return LoginResult("error", f"Telegram flood wait: please wait {exc.seconds}s.")
        except Exception as exc:
            return LoginResult("error", f"2FA verification failed: {exc}")

        return await self._finalize_pending_login()

    async def login_with_string_session(
        self, api_id: int, api_hash: str, session_str: str
    ) -> LoginResult:
        """Alternative login: paste an existing Telethon StringSession."""
        await self.cancel_pending_login()
        client = TelegramClient(StringSession(session_str.strip()), int(api_id), api_hash.strip())
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                return LoginResult("error", "That StringSession is not authorized or has expired.")

            await self._db.set_setting("tg_api_id", int(api_id))
            await self._db.set_setting("tg_api_hash", api_hash.strip())
            await self._db.set_setting("tg_session", session_str.strip())
            await self._attach_client(client)
            return LoginResult("ok", f"Connected as {self.account_display}!")
        except Exception as exc:
            try:
                await client.disconnect()
            except Exception:
                pass
            return LoginResult("error", f"Failed to connect with StringSession: {exc}")

    async def _finalize_pending_login(self) -> LoginResult:
        assert self._pending_login is not None
        p = self._pending_login
        self._pending_login = None

        session_str = p.client.session.save()
        await self._db.set_setting("tg_api_id", p.api_id)
        await self._db.set_setting("tg_api_hash", p.api_hash)
        await self._db.set_setting("tg_session", session_str)

        await self._attach_client(p.client)
        return LoginResult("ok", f"Successfully connected as {self.account_display}!")

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

        # Pre-warm admin caches in the background for enabled groups
        asyncio.create_task(self._prewarm_admin_caches())

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
        """Fetch and cache admin user IDs for a group to keep giveaway checks O(1)."""
        if not self.is_connected or self._client is None:
            return set()
        admin_ids: set[int] = set()
        try:
            async for user in self._client.iter_participants(
                chat_id, filter=ChannelParticipantsAdmins
            ):
                if getattr(user, "id", None):
                    admin_ids.add(int(user.id))
            self._admin_cache[chat_id] = (time.monotonic(), admin_ids)
        except Exception as exc:
            logger.debug("Could not fetch admins for chat %s: %s", chat_id, exc)
        return admin_ids

    async def _prewarm_admin_caches(self) -> None:
        for chat_id, group in list(self._enabled_groups.items()):
            if group.sender_filter == "admins":
                await self.refresh_group_admins(chat_id)
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
        if cached is None or (now - cached[0]) > self.ADMIN_CACHE_TTL:
            admins = await self.refresh_group_admins(chat_id)
        else:
            admins = cached[1]
        return int(sender_id) in admins

    # ------------------------------------------------------------------
    # Real-Time Group Q&A Handler
    # ------------------------------------------------------------------

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        if not self._global_enabled:
            return
        if event.out or event.is_private:
            return

        chat_id = event.chat_id
        if chat_id is None:
            return

        group = self._enabled_groups.get(int(chat_id))
        if group is None or not group.enabled:
            return

        text = (event.raw_text or "").strip()
        if not text:
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
                return
        elif group.sender_filter == "humans":
            sender = await event.get_sender()
            if isinstance(sender, User) and sender.bot:
                return

        # Local pre-filter (runs in <0.1ms before any AI call)
        decision = should_evaluate_message(text, group)
        if not decision.should_process:
            if buf is not None:
                buf.append(text[:200])
            return

        # Per-group cooldown check
        now = time.monotonic()
        last_ts = self._last_reply_ts.get(int(chat_id), 0.0)
        if group.cooldown_seconds > 0 and (now - last_ts) < group.cooldown_seconds:
            return

        # Gather optional short context before appending current message
        context_lines = list(buf)[-group.context_messages :] if (buf and group.context_messages > 0) else None
        if buf is not None:
            buf.append(text[:200])

        # Load active AI settings
        provider_keys = await self._db.get_all_provider_keys()
        primary_provider = str(await self._db.get_setting("primary_provider", "groq"))
        default_model = PROVIDERS.get(primary_provider, PROVIDERS["groq"]).default_model
        primary_model = str(await self._db.get_setting("primary_model", default_model))
        fallback_provider = await self._db.get_setting("fallback_provider")
        fallback_model = await self._db.get_setting("fallback_model")
        temperature = float(await self._db.get_setting("ai_temperature", 0.1))
        max_tokens = int(await self._db.get_setting("ai_max_tokens", 120))

        result: AnswerResult = await self._ai.answer_question(
            text=text,
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
        )

        if result.error:
            logger.warning("AI error in group '%s': %s", group.title, result.error)
            return

        if not result.answered or not result.answer:
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
