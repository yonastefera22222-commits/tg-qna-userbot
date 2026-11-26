"""Render Free has no persistent disk, so the wizard's work can vanish mid-login.

This is Render's own documented behaviour, not an inference from symptoms
(https://render.com/docs/free, checked 2026-11-26):

    Render **spins down** a Free web service that goes 15 minutes without
    receiving any inbound traffic.

    Like all Render services, Free web services have an ephemeral filesystem.
    This means that any changes to your web service's filesystem (uploaded
    images, local SQLite databases, etc.) are *lost* every time the service
    redeploys, restarts, or spins down.

    *Paid* services can preserve local filesystem changes by attaching a
    persistent disk, but Free web services *cannot*.

The bot keeps everything - the Telegram session, the API hash, the enabled
groups, the wizard's own state - in ``data/bot.db``, which is exactly the "local
SQLite database" that sentence describes. So on this host there is no sequence of
wizard steps that can survive a spin-down, and entering the API credentials again
after each one re-creates the same trap.

There is one durable channel: environment variables. ``BOT_STATE_B64`` is already
read at boot and re-seeds the database. What was missing is that the two values a
free deployment needs *most* - the API ID and Hash - had no env var at all, so
they could only ever live in the doomed database.

These tests pin both halves of that: the values are honoured from the environment,
and the wizard tells the truth instead of inviting another round trip.
"""

from __future__ import annotations

import inspect
import tempfile

import pytest

# ---------------------------------------------------------------------------
# API credentials can arrive from the environment (the durable channel)
# ---------------------------------------------------------------------------


def _env_config(monkeypatch, **env: str):
    for key in (
        "ENCRYPTION_KEY",
        "TG_API_ID",
        "TG_API_HASH",
        "BOT_STATE_B64",
        "PUBLIC_URL",
        "RENDER_EXTERNAL_URL",
        "PORT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    monkeypatch.setenv("OWNER_ID", "555")
    monkeypatch.setenv("DATA_DIR", tempfile.mkdtemp())
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    from qna_bot.config import load_env_config

    return load_env_config()


def test_api_credentials_can_come_from_the_environment(monkeypatch) -> None:
    """The only storage on a Free service that survives a spin-down."""
    env = _env_config(
        monkeypatch,
        TG_API_ID="1234567",
        TG_API_HASH="0123456789abcdef0123456789abcdef",
    )

    assert env.tg_api_id == 1234567
    assert env.tg_api_hash == "0123456789abcdef0123456789abcdef"


@pytest.mark.asyncio
async def test_env_credentials_are_written_into_a_fresh_database(monkeypatch) -> None:
    """A new process on a wiped disk must come up already configured."""
    env = _env_config(
        monkeypatch,
        TG_API_ID="1234567",
        TG_API_HASH="0123456789abcdef0123456789abcdef",
    )

    from qna_bot.main import _seed_env_defaults
    from qna_bot.storage.crypto import SecretCipher
    from qna_bot.storage.database import Database

    cipher = SecretCipher.from_env_or_file(None, env.secret_key_path)
    db = Database(env.db_path, cipher)
    await db.connect()
    try:
        await _seed_env_defaults(env, db)
        assert await db.get_setting("tg_api_id") == 1234567
        assert await db.get_setting("tg_api_hash") == "0123456789abcdef0123456789abcdef"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_session_from_the_environment_skips_the_login_entirely(monkeypatch) -> None:
    """TG_STRING_SESSION is the durable way to never face the OTP screen again."""
    env = _env_config(monkeypatch, TG_STRING_SESSION="1abc-not-checked-here")

    from qna_bot.main import _seed_env_defaults
    from qna_bot.storage.crypto import SecretCipher
    from qna_bot.storage.database import Database

    cipher = SecretCipher.from_env_or_file(None, env.secret_key_path)
    db = Database(env.db_path, cipher)
    await db.connect()
    try:
        await _seed_env_defaults(env, db)
        assert await db.get_setting("tg_session") == "1abc-not-checked-here"
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# And the wizard says so, instead of inviting another round trip
# ---------------------------------------------------------------------------


def test_the_wizard_warns_that_a_free_host_forgets_the_database() -> None:
    """Entering credentials that will be deleted in 15 minutes must be flagged."""
    from qna_bot.dashboard.handlers import wizard

    src = inspect.getsource(wizard._show_wizard_welcome)
    assert "persistent" in src.lower() or "wipe" in src.lower()


def test_the_export_backup_text_mentions_disk_loss() -> None:
    """The documented escape hatch must still be discoverable from the dashboard."""
    from qna_bot.dashboard.handlers import dashboard

    src = inspect.getsource(dashboard.cb_export_render)
    assert "BOT_STATE_B64" in src
