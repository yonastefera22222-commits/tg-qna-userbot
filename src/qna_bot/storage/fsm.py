"""SQLite-backed aiogram FSM storage that survives restarts and keeps the wizard in sync.

Why this exists
---------------
The Dashboard bot used ``MemoryStorage``, which lives in RAM inside one process.
That breaks the setup wizard in three realistic ways:

1. **Render Free restarts.** Render sleeps a free Web Service after 15 minutes of
   inactivity and wipes/restarts the container on redeploys. With ``MemoryStorage``
   every wizard step is forgotten: the owner taps a keypad or sends a phone number
   and *nothing happens at all*, because the FSM state that would route the message
   no longer exists. That is indistinguishable from "Telegram never sent a code".

2. **Duplicate instances.** ``TelegramConflictError`` showed two copies of the bot
   polling the same token. Updates are delivered to whichever instance answers, so
   with RAM-only state a message could land on an instance that never saw the
   wizard started — silent drop again.

3. **No persistence across ``/cancel`` bugs or crashes.** Any restart mid-login
   lost the ``PhoneLoginSession`` *and* the conversation state together.

Keeping FSM state in the same SQLite file as the rest of the configuration means a
restarted process resumes the wizard exactly where the owner left off, and all
instances share one view of the conversation.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import aiosqlite
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey

logger = logging.getLogger(__name__)

#: FSM entries older than this are dropped so the DB cannot grow without bound.
DEFAULT_TTL_SECONDS = 60 * 60 * 24  # 24 hours


class SQLiteStorage(BaseStorage):
    """Persist FSM state/data in the same SQLite database the bot already uses."""

    def __init__(self, db_path: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._db_path = db_path
        self._ttl_seconds = ttl_seconds
        self._conn: aiosqlite.Connection | None = None

    async def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            self._conn = await aiosqlite.connect(self._db_path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL;")
            await self._conn.execute("PRAGMA busy_timeout=5000;")
            await self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fsm_states (
                    storage_key TEXT PRIMARY KEY,
                    state TEXT,
                    data TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL
                );
                """
            )
            await self._conn.commit()
        return self._conn

    @staticmethod
    def _key(key: StorageKey) -> str:
        # bot_id is intentionally excluded: there is exactly one dashboard bot, and
        # including it would silently orphan state if the token is rotated.
        parts = [str(key.chat_id), str(key.user_id)]
        if key.thread_id is not None:
            parts.append(str(key.thread_id))
        if key.business_connection_id is not None:
            parts.append(str(key.business_connection_id))
        if key.destiny:  # aiogram >= 3.14 differentiates "state" vs "data" storage
            parts.append(str(key.destiny))
        return ":".join(parts)

    async def set_state(self, key: StorageKey, state: str | State | None = None) -> None:
        state_value = state.state if isinstance(state, State) else state
        await self._upsert(self._key(key), state=state_value)

    async def get_state(self, key: StorageKey) -> str | None:
        row = await self._fetch(self._key(key))
        return row["state"] if row is not None else None

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        await self._upsert(self._key(key), data=json.dumps(data, default=str))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        row = await self._fetch(self._key(key))
        if row is None or not row["data"]:
            return {}
        try:
            loaded = json.loads(row["data"])
        except (TypeError, ValueError):  # pragma: no cover - corrupt row
            logger.warning("Discarding unreadable FSM data for %s", self._key(key))
            return {}
        return loaded if isinstance(loaded, dict) else {}

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # -- internals -----------------------------------------------------
    async def _upsert(
        self,
        storage_key: str,
        *,
        state: object = ...,
        data: str | None = None,
    ) -> None:
        conn = await self._connection()
        now = time.time()
        cursor = await conn.execute(
            "SELECT state, data FROM fsm_states WHERE storage_key = ?", (storage_key,)
        )
        row = await cursor.fetchone()

        new_state = row["state"] if row is not None else None
        new_data = row["data"] if row is not None else "{}"
        if state is not ...:
            new_state = state
        if data is not None:
            new_data = data

        await conn.execute(
            """
            INSERT INTO fsm_states (storage_key, state, data, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(storage_key) DO UPDATE SET
                state = excluded.state,
                data = excluded.data,
                updated_at = excluded.updated_at
            """,
            (storage_key, new_state, new_data, now),
        )
        await conn.commit()
        await self._maybe_gc(conn, now)

    async def _fetch(self, storage_key: str) -> aiosqlite.Row | None:
        conn = await self._connection()
        cursor = await conn.execute(
            "SELECT state, data, updated_at FROM fsm_states WHERE storage_key = ?",
            (storage_key,),
        )
        return await cursor.fetchone()

    async def _maybe_gc(self, conn: aiosqlite.Connection, now: float) -> None:
        if int(now) % 60 != 0:  # cheap throttle: run at most once a minute
            return
        cutoff = time.time() - self._ttl_seconds
        await conn.execute("DELETE FROM fsm_states WHERE updated_at < ?", (cutoff,))
        await conn.commit()
