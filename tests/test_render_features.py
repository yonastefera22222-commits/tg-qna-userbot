"""Tests for Render Free Tier features: portable BOT_STATE_B64 export/import & HTTP /health server."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from qna_bot.health import HealthServer
from qna_bot.storage.crypto import SecretCipher
from qna_bot.storage.database import Database


@pytest.mark.asyncio
async def test_state_b64_survives_full_disk_and_key_wipe(tmp_path: Path) -> None:
    """Simulates a Render Free Tier container restart with a brand new DB and encryption key."""
    # Container 1: configure account, keys, and groups
    dir1 = tmp_path / "container1"
    cipher1 = SecretCipher.from_env_or_file(None, dir1 / ".secret.key")
    db1 = Database(dir1 / "bot.db", cipher1)
    await db1.connect()
    try:
        await db1.set_setting("owner_id", 11223344)
        await db1.set_setting("setup_completed", True)
        await db1.set_setting("tg_api_id", 987654)
        await db1.set_setting("tg_api_hash", "hash_secret_abcdef")
        await db1.set_setting("tg_session", "1BVtsOHoBu_fake_telethon_session")
        await db1.set_provider_key("groq", "gsk_live_test_key")
        await db1.set_provider_key("gemini", "AIza_fake_gemini_key")
        await db1.set_setting("primary_provider", "groq")
        await db1.set_setting("primary_model", "llama-3.3-70b-versatile")
        await db1.set_setting("fallback_provider", "gemini")
        await db1.set_setting("fallback_model", "gemini-3.5-flash-lite")

        g1 = await db1.upsert_group_metadata(-100555111, "Giveaway Arena", "givearena")
        g1.enabled = True
        g1.sender_filter = "all"
        g1.cooldown_seconds = 0.0
        await db1.save_group(g1)

        # Disabled group should NOT bloat the exported snapshot
        await db1.upsert_group_metadata(-100555222, "Random Chat", None)

        exported = await db1.export_state_b64()
        assert len(exported) < 1500
    finally:
        await db1.close()

    # Container 2: fresh empty disk and brand new Fernet encryption key
    dir2 = tmp_path / "container2"
    cipher2 = SecretCipher.from_env_or_file(None, dir2 / ".secret.key")
    db2 = Database(dir2 / "bot.db", cipher2)
    await db2.connect()
    try:
        n_settings, n_groups = await db2.import_state_b64(exported)
        assert n_settings >= 10
        assert n_groups == 1

        assert await db2.get_setting("owner_id") == 11223344
        assert await db2.get_setting("setup_completed") is True
        assert await db2.get_setting("tg_api_id") == 987654
        assert await db2.get_setting("tg_api_hash") == "hash_secret_abcdef"
        assert await db2.get_setting("tg_session") == "1BVtsOHoBu_fake_telethon_session"
        assert await db2.get_provider_key("groq") == "gsk_live_test_key"
        assert await db2.get_provider_key("gemini") == "AIza_fake_gemini_key"

        restored_group = await db2.get_group(-100555111)
        assert restored_group is not None
        assert restored_group.enabled is True
        assert restored_group.sender_filter == "all"
        assert restored_group.cooldown_seconds == 0.0
        assert await db2.get_group(-100555222) is None
    finally:
        await db2.close()


@pytest.mark.asyncio
async def test_health_server_endpoints() -> None:
    server = HealthServer(
        port=0,  # OS assigns an available ephemeral port
        host="127.0.0.1",
        status_provider=lambda: {"userbot_connected": True, "active_groups": 3},
    )
    await server.start()
    try:
        port = server.bound_port
        assert port > 0
        async with httpx.AsyncClient() as client:
            r1 = await client.get(f"http://127.0.0.1:{port}/")
            assert r1.status_code == 200
            assert r1.json() == {"status": "ok", "userbot_connected": True, "active_groups": 3}

            r2 = await client.get(f"http://127.0.0.1:{port}/health")
            assert r2.status_code == 200
            assert r2.json()["status"] == "ok"
    finally:
        await server.stop()
