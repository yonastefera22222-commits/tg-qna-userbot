"""Tests for SecretCipher encryption and async SQLite Database operations."""

from __future__ import annotations

from pathlib import Path

import pytest

from qna_bot.storage.crypto import SecretCipher
from qna_bot.storage.database import Database


@pytest.mark.asyncio
async def test_cipher_roundtrip_and_keyfile(tmp_path: Path) -> None:
    key_file = tmp_path / ".secret.key"
    cipher1 = SecretCipher.from_env_or_file(None, key_file)
    assert key_file.exists()

    enc = cipher1.encrypt("my-super-secret-session")
    assert enc.startswith("enc::")
    assert "my-super-secret-session" not in enc
    assert cipher1.decrypt(enc) == "my-super-secret-session"

    # Reloading from the same key file decrypts cleanly
    cipher2 = SecretCipher.from_env_or_file(None, key_file)
    assert cipher2.decrypt(enc) == "my-super-secret-session"


@pytest.mark.asyncio
async def test_database_settings_groups_and_logs(tmp_path: Path) -> None:
    cipher = SecretCipher.from_env_or_file(None, tmp_path / ".secret.key")
    db = Database(tmp_path / "test.db", cipher)
    await db.connect()

    try:
        # Secret settings are encrypted in SQLite
        await db.set_provider_key("groq", "gsk_test_12345")
        await db.set_setting("tg_session", "session_abc_xyz")
        await db.set_setting("owner_id", 999000111)

        assert await db.get_provider_key("groq") == "gsk_test_12345"
        assert await db.get_setting("tg_session") == "session_abc_xyz"
        assert await db.get_setting("owner_id") == 999000111

        # Verify raw DB row for secret is NOT plaintext
        cur = await db.conn.execute("SELECT value FROM settings WHERE key = 'api_key_groq'")
        row = await cur.fetchone()
        assert row["value"].startswith("enc::")
        assert "gsk_test_12345" not in row["value"]

        # Group upsert preserves custom settings on title refresh
        g = await db.upsert_group_metadata(-1001234567890, "Crypto Giveaways", "cryptogive")
        assert g.enabled is False
        assert g.sender_filter == "admins"
        assert g.reply_to_message is True

        g.enabled = True
        g.answer_style = "short"
        g.trigger_keywords = "Q:, Trivia"
        await db.save_group(g)

        refreshed = await db.upsert_group_metadata(-1001234567890, "Crypto Giveaways VIP", "cryptogive")
        assert refreshed.title == "Crypto Giveaways VIP"
        assert refreshed.enabled is True
        assert refreshed.answer_style == "short"
        assert refreshed.trigger_keyword_list() == ["q:", "trivia"]

        # Toggle & list_groups
        toggled = await db.toggle_group_enabled(-1001234567890)
        assert toggled is not None and toggled.enabled is False
        assert await db.list_groups(only_enabled=True) == []

        # Logs & stats
        await db.log_answer(
            chat_id=-1001234567890,
            chat_title="Crypto Giveaways VIP",
            question_text="What is 12 * 12?",
            answer_text="144",
            provider="local_math",
            model="ast-instant",
            latency_ms=1,
        )
        await db.log_answer(
            chat_id=-1001234567890,
            chat_title="Crypto Giveaways VIP",
            question_text="Capital of France?",
            answer_text="Paris",
            provider="groq",
            model="llama-3.3-70b-versatile",
            latency_ms=259,
        )
        stats = await db.get_stats()
        assert stats["total"] == 2
        assert stats["today"] == 2
        assert stats["local_math_hits"] == 1
        assert stats["avg_latency_ms"] == 130

        logs = await db.get_recent_logs(limit=5)
        assert len(logs) == 2
        assert logs[0].answer_text == "Paris"
    finally:
        await db.close()
