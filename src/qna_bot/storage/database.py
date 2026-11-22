"""Async SQLite persistence for settings, groups, credentials, and Q&A logs."""

from __future__ import annotations

import base64
import gzip
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from .crypto import SecretCipher

SECRET_SETTING_KEYS = frozenset(
    {
        "tg_api_hash",
        "tg_session",
        "api_key_groq",
        "api_key_gemini",
        "api_key_openrouter",
    }
)

#: Settings that must never be copied into the portable BOT_STATE_B64 snapshot.
#: ``tg_pending_login`` records an in-progress phone login so a restart mid-login
#: can be detected; it is worthless after the process dies and stays out of exports.
EPHEMERAL_SETTING_KEYS = frozenset({"tg_pending_login"})


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
                context_messages INTEGER NOT NULL DEFAULT 0
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
            return self._cipher.decrypt(raw_val)
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
                human_delay_ms, reply_to_message, trigger_keywords, context_messages
            ) VALUES (
                :chat_id, :title, :username, :enabled, :sender_filter, :answer_style,
                :detection_mode, :local_math_enabled, :cooldown_seconds,
                :human_delay_ms, :reply_to_message, :trigger_keywords, :context_messages
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
                context_messages = excluded.context_messages
            """,
            {
                **data,
                "enabled": int(group.enabled),
                "local_math_enabled": int(group.local_math_enabled),
                "reply_to_message": int(group.reply_to_message),
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

    async def export_state_b64(self) -> str:
        """Export settings + enabled groups as a compact gzip+base64 string for Render env vars."""
        cursor = await self.conn.execute("SELECT key FROM settings")
        rows = await cursor.fetchall()
        settings_dump: dict[str, Any] = {}
        for row in rows:
            key = str(row["key"])
            if key in EPHEMERAL_SETTING_KEYS:
                continue
            settings_dump[key] = await self.get_setting(key)

        enabled_groups = await self.list_groups(only_enabled=True)
        payload = {
            "v": 1,
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

        for key, value in settings_dump.items():
            await self.set_setting(str(key), value)

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
            )
            await self.save_group(group)
            restored_groups += 1

        return len(settings_dump), restored_groups
