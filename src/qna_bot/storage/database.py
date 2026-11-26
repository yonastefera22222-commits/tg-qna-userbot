"""Async SQLite persistence for settings, groups, credentials, and Q&A logs."""

from __future__ import annotations

import base64
import gzip
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from .crypto import SecretCipher

logger = logging.getLogger(__name__)

SECRET_SETTING_KEYS = frozenset(
    {
        "tg_api_hash",
        "tg_session",
        "api_key_groq",
        "api_key_gemini",
        "api_key_openrouter",
        # Exness Trader API credentials. The private key signs every request,
        # so it is as sensitive as any AI key and is encrypted the same way.
        "exness_api_key",
        "exness_private_key",
    }
)

#: Settings that must never be copied into the portable BOT_STATE_B64 snapshot.
#: ``tg_pending_login`` records an in-progress phone login so a restart mid-login
#: can be detected; it is worthless after the process dies and stays out of exports.
EPHEMERAL_SETTING_KEYS = frozenset(
    {
        # Holds the live Telethon StringSession of a *temporary* login client. It is
        # not a secret worth keeping beyond the attempt: once the login completes the
        # real session is written to ``tg_session``, and if the attempt is abandoned
        # the temporary client is worthless.
        "tg_pending_login",
        "tg_pending_login_session",
    }
)

#: Settings that identify the deployment's owner and must NEVER travel to
#: someone else. A Telethon session string is a complete account login: whoever
#: holds it *is* that Telegram account until the session is revoked. Sharing a
#: snapshot containing it hands over the account, not just a config.
IDENTITY_SETTING_KEYS = SECRET_SETTING_KEYS | frozenset({"owner_id", "tg_api_id"})

#: Export profiles. ``backup`` is for the same deployment surviving a restart and
#: keeps everything. ``share`` is for giving someone else a starting
#: configuration and carries no credentials or owner identity at all.
EXPORT_PROFILES = ("backup", "share")


@dataclass
class GroupConfig:
    chat_id: int
    title: str
    username: str | None = None
    enabled: bool = False
    # "admins" | "humans" | "all"
    sender_filter: str = "admins"
    # "giveaway" | "short" | "detailed"
    answer_style: str = "giveaway"
    # "giveaway_aggressive" | "balanced" | "strict"
    detection_mode: str = "giveaway_aggressive"
    local_math_enabled: bool = True
    cooldown_seconds: float = 1.0
    human_delay_ms: int = 0
    reply_to_message: bool = True
    trigger_keywords: str = ""
    context_messages: int = 0
    #: Answer questions that are asked as an image (screenshot of a quiz, photo
    #: of a question). Requires a vision-capable model.
    vision_enabled: bool = True
    #: Treat this group as a Forex/trading group: recognise trader shorthand
    #: ("EURUSD?", "GU bias") and answer with FX conventions plus a strict
    #: no-invented-prices rule.
    forex_enabled: bool = False
    #: Forex groups only. When off, the bot still answers with FX conventions but
    #: refuses live questions instead of fetching a price.
    forex_live_data: bool = True
    #: Answer "first comment" race signals with a fixed line, instantly and
    #: without an AI call. Off by default: it posts on its own.
    first_comment_enabled: bool = False

    def trigger_keyword_list(self) -> list[str]:
        if not self.trigger_keywords.strip():
            return []
        return [kw.strip().lower() for kw in self.trigger_keywords.split(",") if kw.strip()]


@dataclass
class AnswerLog:
    id: int
    chat_id: int
    chat_title: str
    question_text: str
    answer_text: str
    provider: str
    model: str
    latency_ms: int
    created_at: str


class Database:
    """Async SQLite repository with encrypted secret settings."""

    def __init__(self, db_path: Path, cipher: SecretCipher) -> None:
        self._db_path = db_path
        self._cipher = cipher
        self._conn: aiosqlite.Connection | None = None
        #: Secret keys whose stored value could not be decrypted with the current
        #: key. Populated on read so the dashboard/diagnostics can tell the owner
        #: "this was lost when ENCRYPTION_KEY changed" instead of showing a blank.
        self.undecryptable: set[str] = set()

    async def connect(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA synchronous=NORMAL;")
        await self._init_schema()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected.")
        return self._conn

    async def _init_schema(self) -> None:
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS groups (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                username TEXT,
                enabled INTEGER NOT NULL DEFAULT 0,
                sender_filter TEXT NOT NULL DEFAULT 'admins',
                answer_style TEXT NOT NULL DEFAULT 'giveaway',
                detection_mode TEXT NOT NULL DEFAULT 'giveaway_aggressive',
                local_math_enabled INTEGER NOT NULL DEFAULT 1,
                cooldown_seconds REAL NOT NULL DEFAULT 1.0,
                human_delay_ms INTEGER NOT NULL DEFAULT 0,
                reply_to_message INTEGER NOT NULL DEFAULT 1,
                trigger_keywords TEXT NOT NULL DEFAULT '',
                context_messages INTEGER NOT NULL DEFAULT 0,
                vision_enabled INTEGER NOT NULL DEFAULT 1,
                forex_enabled INTEGER NOT NULL DEFAULT 0,
                forex_live_data INTEGER NOT NULL DEFAULT 1,
                first_comment_enabled INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS answer_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                chat_title TEXT NOT NULL,
                question_text TEXT NOT NULL,
                answer_text TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                latency_ms INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_answer_logs_created_at
                ON answer_logs (created_at DESC);
            """
        )
        await self.conn.commit()
        await self._migrate_schema()

    async def _migrate_schema(self) -> None:
        """Additive migrations for databases created by older versions.

        ``CREATE TABLE IF NOT EXISTS`` never adds columns to an existing table,
        so without this an upgraded bot would hit "no such column" on every group
        query. Each step is idempotent.
        """
        cursor = await self.conn.execute("PRAGMA table_info(groups)")
        existing = {str(row["name"]) for row in await cursor.fetchall()}

        additions = {
            "vision_enabled": "INTEGER NOT NULL DEFAULT 1",
            "forex_enabled": "INTEGER NOT NULL DEFAULT 0",
        "forex_live_data": "INTEGER NOT NULL DEFAULT 1",
        "first_comment_enabled": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, ddl in additions.items():
            if column not in existing:
                logger.info("Migrating groups table: adding column %s", column)
                await self.conn.execute(f"ALTER TABLE groups ADD COLUMN {column} {ddl}")
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Key-Value Settings
    # ------------------------------------------------------------------

    async def get_setting(self, key: str, default: Any = None) -> Any:
        cursor = await self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        if row is None:
            return default
        raw_val = row["value"]
        if key in SECRET_SETTING_KEYS:
            try:
                return self._cipher.decrypt(raw_val)
            except ValueError:
                # The value was encrypted with a different key: ENCRYPTION_KEY was
                # blanked, replaced, or the generated key file was lost when the
                # host wiped its disk. Raising here took down whatever called it
                # (a boot step, a diagnostics command, the wizard) with a traceback
                # the owner never sees, so the bot looked simply dead.
                #
                # Reading a setting must not be able to crash the process. The
                # secret is unrecoverable either way; the only useful response is
                # to report it as absent and let the caller ask for it again.
                logger.error(
                    "Cannot decrypt %s: stored with a different ENCRYPTION_KEY. "
                    "Treating it as unset; re-enter it to fix this.",
                    key,
                )
                self.undecryptable.add(key)
                return default
        try:
            return json.loads(raw_val)
        except json.JSONDecodeError:
            return raw_val

    async def set_setting(self, key: str, value: Any) -> None:
        if value is None:
            await self.delete_setting(key)
            return
        if key in SECRET_SETTING_KEYS:
            stored = self._cipher.encrypt(str(value))
        else:
            stored = json.dumps(value)
        await self.conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, stored),
        )
        await self.conn.commit()

    async def delete_setting(self, key: str) -> None:
        await self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        await self.conn.commit()

    async def get_provider_key(self, provider: str) -> str | None:
        val = await self.get_setting(f"api_key_{provider}")
        return str(val) if val else None

    async def set_provider_key(self, provider: str, api_key: str | None) -> None:
        await self.set_setting(f"api_key_{provider}", api_key)

    async def get_settings(self, keys: tuple[str, ...]) -> dict[str, Any]:
        """Fetch several settings in one query (one await instead of N).

        Every group message used to issue six separate SELECTs before the AI
        call could even start. Reading them together removes that serialised
        database round-tripping from the hot path.
        """
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        cursor = await self.conn.execute(
            f"SELECT key, value FROM settings WHERE key IN ({placeholders})", keys
        )
        rows = await cursor.fetchall()
        found = {row["key"]: row["value"] for row in rows}

        result: dict[str, Any] = {}
        for key in keys:
            if key not in found:
                continue
            raw_val = found[key]
            if key in SECRET_SETTING_KEYS:
                result[key] = self._cipher.decrypt(raw_val)
                continue
            try:
                result[key] = json.loads(raw_val)
            except (json.JSONDecodeError, TypeError):
                result[key] = raw_val
        return result

    async def get_all_provider_keys(self) -> dict[str, str]:
        keys: dict[str, str] = {}
        for provider in ("groq", "gemini", "openrouter"):
            val = await self.get_provider_key(provider)
            if val:
                keys[provider] = val
        return keys

    # ------------------------------------------------------------------
    # Group Configurations
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_group(row: aiosqlite.Row) -> GroupConfig:
        return GroupConfig(
            chat_id=int(row["chat_id"]),
            title=str(row["title"]),
            username=row["username"],
            enabled=bool(row["enabled"]),
            sender_filter=str(row["sender_filter"]),
            answer_style=str(row["answer_style"]),
            detection_mode=str(row["detection_mode"]),
            local_math_enabled=bool(row["local_math_enabled"]),
            cooldown_seconds=float(row["cooldown_seconds"]),
            human_delay_ms=int(row["human_delay_ms"]),
            reply_to_message=bool(row["reply_to_message"]),
            trigger_keywords=str(row["trigger_keywords"]),
            context_messages=int(row["context_messages"]),
            vision_enabled=bool(row["vision_enabled"]) if "vision_enabled" in row.keys() else True,
            forex_enabled=bool(row["forex_enabled"]) if "forex_enabled" in row.keys() else False,
            forex_live_data=bool(row["forex_live_data"]) if "forex_live_data" in row.keys() else True,
            first_comment_enabled=(
                bool(row["first_comment_enabled"])
                if "first_comment_enabled" in row.keys()
                else False
            ),
        )

    async def upsert_group_metadata(
        self, chat_id: int, title: str, username: str | None = None
    ) -> GroupConfig:
        """Insert a group with defaults if new, or refresh title/username preserving settings."""
        await self.conn.execute(
            """
            INSERT INTO groups (chat_id, title, username)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                username = excluded.username
            """,
            (chat_id, title, username),
        )
        await self.conn.commit()
        group = await self.get_group(chat_id)
        assert group is not None
        return group

    async def save_group(self, group: GroupConfig) -> None:
        data = asdict(group)
        await self.conn.execute(
            """
            INSERT INTO groups (
                chat_id, title, username, enabled, sender_filter, answer_style,
                detection_mode, local_math_enabled, cooldown_seconds,
                human_delay_ms, reply_to_message, trigger_keywords, context_messages,
                vision_enabled, forex_enabled, forex_live_data,
                first_comment_enabled
            ) VALUES (
                :chat_id, :title, :username, :enabled, :sender_filter, :answer_style,
                :detection_mode, :local_math_enabled, :cooldown_seconds,
                :human_delay_ms, :reply_to_message, :trigger_keywords, :context_messages,
                :vision_enabled, :forex_enabled, :forex_live_data,
                :first_comment_enabled
            )
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                username = excluded.username,
                enabled = excluded.enabled,
                sender_filter = excluded.sender_filter,
                answer_style = excluded.answer_style,
                detection_mode = excluded.detection_mode,
                local_math_enabled = excluded.local_math_enabled,
                cooldown_seconds = excluded.cooldown_seconds,
                human_delay_ms = excluded.human_delay_ms,
                reply_to_message = excluded.reply_to_message,
                trigger_keywords = excluded.trigger_keywords,
                context_messages = excluded.context_messages,
                vision_enabled = excluded.vision_enabled,
                forex_enabled = excluded.forex_enabled,
                forex_live_data = excluded.forex_live_data,
                first_comment_enabled = excluded.first_comment_enabled
            """,
            {
                **data,
                "enabled": int(group.enabled),
                "local_math_enabled": int(group.local_math_enabled),
                "reply_to_message": int(group.reply_to_message),
                "vision_enabled": int(group.vision_enabled),
                "forex_enabled": int(group.forex_enabled),
                "forex_live_data": int(group.forex_live_data),
                "first_comment_enabled": int(group.first_comment_enabled),
            },
        )
        await self.conn.commit()

    async def get_group(self, chat_id: int) -> GroupConfig | None:
        cursor = await self.conn.execute("SELECT * FROM groups WHERE chat_id = ?", (chat_id,))
        row = await cursor.fetchone()
        return self._row_to_group(row) if row else None

    async def list_groups(self, only_enabled: bool = False) -> list[GroupConfig]:
        if only_enabled:
            query = "SELECT * FROM groups WHERE enabled = 1 ORDER BY LOWER(title) ASC"
        else:
            query = "SELECT * FROM groups ORDER BY enabled DESC, LOWER(title) ASC"
        cursor = await self.conn.execute(query)
        rows = await cursor.fetchall()
        return [self._row_to_group(r) for r in rows]

    async def toggle_group_enabled(self, chat_id: int) -> GroupConfig | None:
        group = await self.get_group(chat_id)
        if not group:
            return None
        group.enabled = not group.enabled
        await self.save_group(group)
        return group

    async def disable_all_groups(self) -> None:
        await self.conn.execute("UPDATE groups SET enabled = 0")
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Answer Logs & Statistics
    # ------------------------------------------------------------------

    async def log_answer(
        self,
        *,
        chat_id: int,
        chat_title: str,
        question_text: str,
        answer_text: str,
        provider: str,
        model: str,
        latency_ms: int,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        await self.conn.execute(
            """
            INSERT INTO answer_logs (
                chat_id, chat_title, question_text, answer_text,
                provider, model, latency_ms, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                chat_title,
                question_text[:500],
                answer_text[:500],
                provider,
                model,
                int(latency_ms),
                now,
            ),
        )
        await self.conn.commit()

    async def get_recent_logs(self, limit: int = 10) -> list[AnswerLog]:
        cursor = await self.conn.execute(
            "SELECT * FROM answer_logs ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [
            AnswerLog(
                id=int(r["id"]),
                chat_id=int(r["chat_id"]),
                chat_title=str(r["chat_title"]),
                question_text=str(r["question_text"]),
                answer_text=str(r["answer_text"]),
                provider=str(r["provider"]),
                model=str(r["model"]),
                latency_ms=int(r["latency_ms"]),
                created_at=str(r["created_at"]),
            )
            for r in rows
        ]

    async def get_stats(self) -> dict[str, Any]:
        today_prefix = datetime.now(UTC).date().isoformat()
        cur_total = await self.conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(AVG(latency_ms), 0) AS avg_latency,
                SUM(CASE WHEN provider = 'local_math' THEN 1 ELSE 0 END) AS local_math_hits
            FROM answer_logs
            """
        )
        row_total = await cur_total.fetchone()

        cur_today = await self.conn.execute(
            "SELECT COUNT(*) AS today_count FROM answer_logs WHERE created_at LIKE ?",
            (f"{today_prefix}%",),
        )
        row_today = await cur_today.fetchone()

        return {
            "total": int(row_total["total"] or 0),
            "today": int(row_today["today_count"] or 0),
            "avg_latency_ms": int(round(float(row_total["avg_latency"] or 0))),
            "local_math_hits": int(row_total["local_math_hits"] or 0),
        }

    async def clear_logs(self) -> None:
        await self.conn.execute("DELETE FROM answer_logs")
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Portable State Export / Import (for Ephemeral Hosts like Render Free)
    # ------------------------------------------------------------------

    async def export_state_b64(self, profile: str = "backup") -> str:
        """Export settings + enabled groups as a compact gzip+base64 string.

        ``profile="backup"`` (default) keeps credentials so the *same*
        deployment can restore itself on an ephemeral host.
        ``profile="share"`` strips every credential and owner identity, so the
        snapshot is safe to hand to another person. Handing over a backup
        snapshot instead would give them the owner's Telegram account.
        """
        if profile not in EXPORT_PROFILES:
            raise ValueError(f"Unknown export profile: {profile}")
        skip = EPHEMERAL_SETTING_KEYS
        if profile == "share":
            skip = skip | IDENTITY_SETTING_KEYS

        cursor = await self.conn.execute("SELECT key FROM settings")
        rows = await cursor.fetchall()
        settings_dump: dict[str, Any] = {}
        for row in rows:
            key = str(row["key"])
            if key in skip:
                continue
            settings_dump[key] = await self.get_setting(key)

        enabled_groups = await self.list_groups(only_enabled=True)
        payload = {
            "v": 2,
            "profile": profile,
            "settings": settings_dump,
            "groups": [asdict(g) for g in enabled_groups],
        }
        raw_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        compressed = gzip.compress(raw_bytes, compresslevel=9)
        return base64.urlsafe_b64encode(compressed).decode("ascii")

    async def import_state_b64(self, encoded: str) -> tuple[int, int]:
        """Restore settings + groups from a BOT_STATE_B64 string. Returns (settings_n, groups_n)."""
        cleaned = "".join(encoded.split())
        compressed = base64.urlsafe_b64decode(cleaned.encode("ascii"))
        payload = json.loads(gzip.decompress(compressed).decode("utf-8"))

        settings_dump = payload.get("settings") or {}
        groups_dump = payload.get("groups") or []

        # Never let a snapshot take over this deployment's identity. An old
        # "backup" snapshot contains the original owner's Telegram session and
        # AI keys; importing it into someone else's deployment would answer
        # from the wrong account and bill the wrong person. Credentials and
        # owner identity always come from THIS deployment's env/dashboard.
        imported_credentials = 0
        skipped_identity: list[str] = []
        current_owner = await self.get_setting("owner_id")
        for key, value in settings_dump.items():
            if key in IDENTITY_SETTING_KEYS:
                # A snapshot may only restore a credential if this deployment has
                # not claimed an owner of its own yet, i.e. it is the same
                # deployment restoring itself:
                #   * never-claimed deployment  -> original owner's snapshot, OK
                #   * different owner claimed   -> someone else's snapshot, refused
                if current_owner is None:
                    imported_credentials += 1
                    await self.set_setting(key, value)
                else:
                    skipped_identity.append(key)
                continue
            await self.set_setting(key, value)

        if skipped_identity:
            logger.warning(
                "Refused %d credential/identity setting(s) from an imported snapshot: "
                "this deployment is already owned by %s, so importing another "
                "account's session or API keys would hijack it (%s). Set them from "
                "this deployment's own dashboard or env.",
                len(skipped_identity),
                current_owner,
                ", ".join(sorted(skipped_identity)),
            )
        if imported_credentials:
            logger.info(
                "Restored %d credential setting(s) from the snapshot for an "
                "unclaimed deployment.",
                imported_credentials,
            )

        restored_groups = 0
        for g_dict in groups_dump:
            group = GroupConfig(
                chat_id=int(g_dict["chat_id"]),
                title=str(g_dict.get("title", f"Group {g_dict['chat_id']}")),
                username=g_dict.get("username"),
                enabled=bool(g_dict.get("enabled", True)),
                sender_filter=str(g_dict.get("sender_filter", "admins")),
                answer_style=str(g_dict.get("answer_style", "giveaway")),
                detection_mode=str(g_dict.get("detection_mode", "giveaway_aggressive")),
                local_math_enabled=bool(g_dict.get("local_math_enabled", True)),
                cooldown_seconds=float(g_dict.get("cooldown_seconds", 1.0)),
                human_delay_ms=int(g_dict.get("human_delay_ms", 0)),
                reply_to_message=bool(g_dict.get("reply_to_message", True)),
                trigger_keywords=str(g_dict.get("trigger_keywords", "")),
                context_messages=int(g_dict.get("context_messages", 0)),
                vision_enabled=bool(g_dict.get("vision_enabled", True)),
                forex_enabled=bool(g_dict.get("forex_enabled", False)),
                forex_live_data=bool(g_dict.get("forex_live_data", True)),
                first_comment_enabled=bool(g_dict.get("first_comment_enabled", False)),
            )
            await self.save_group(group)
            restored_groups += 1

        return len(settings_dump), restored_groups
