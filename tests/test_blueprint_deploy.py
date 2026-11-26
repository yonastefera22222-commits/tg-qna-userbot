"""A blueprint deploy sets only BOT_TOKEN and OWNER_ID. Two things then break.

Reported as "I used blueprint and only filled the owner id and bot token", after
the OTP still failed. Both causes are here, and neither is Telegram:

1. **The API ID / API Hash are not in `render.yaml`.** They are not environment
   variables at all, so a blueprint deploy leaves them unset and the wizard has to
   collect them. The wizard then kept them **only in the conversation's FSM
   state** and never wrote them to the database, so the next restart lost them and
   the login failed with "Missing API credentials" - which sends the owner round
   the same loop again.

2. **`ENCRYPTION_KEY` is `sync: false` too.** Left blank, the bot generates one
   into `data/.secret.key` on the container disk. Render Free wipes that disk on
   every restart, so a *new* key is generated and every stored secret silently
   becomes unreadable. Reading one raised `ValueError` out of `get_setting`,
   taking down whatever called it (boot, diagnostics, the wizard) with a traceback
   the owner never sees. The bot simply looked broken.

Both are fixed to degrade with an explanation instead of a traceback.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from qna_bot.storage.crypto import SecretCipher
from qna_bot.storage.database import Database


async def _open(db_path, key: bytes) -> Database:
    db = Database(db_path, SecretCipher(key))
    await db.connect()
    return db


@pytest.mark.asyncio
async def test_an_unreadable_secret_is_reported_as_missing_not_raised(tmp_path) -> None:
    """This is the blueprint restart: new generated key, old ciphertext."""
    db_path = tmp_path / "bot.db"

    db = await _open(db_path, Fernet.generate_key())
    await db.set_setting("tg_api_hash", "0" * 32)
    await db.close()

    # A restart with a different key — exactly what a wiped disk produces.
    db2 = await _open(db_path, Fernet.generate_key())
    try:
        value = await db2.get_setting("tg_api_hash")
    finally:
        await db2.close()

    assert value is None, "an unreadable secret must read as unset"
    assert "tg_api_hash" in db2.undecryptable


@pytest.mark.asyncio
async def test_the_undecryptable_set_is_what_the_wizard_reports(tmp_path) -> None:
    """The wizard distinguishes "never entered" from "cannot be read"."""
    db_path = tmp_path / "bot.db"

    db = await _open(db_path, Fernet.generate_key())
    await db.set_setting("tg_api_id", 123456)
    await db.set_setting("tg_api_hash", "0" * 32)
    await db.close()

    db2 = await _open(db_path, Fernet.generate_key())
    try:
        # tg_api_id is not a SECRET key - it is stored as plain JSON, so it
        # survives a key change untouched. That makes the half-broken state
        # especially confusing: the API ID is still there, the hash is not.
        assert await db2.get_setting("tg_api_id") == 123456
        assert await db2.get_setting("tg_api_hash") is None
        assert "tg_api_hash" in db2.undecryptable
        # Which is why the wizard's guard checks both, not just the id.
        lost = db2.undecryptable & {"tg_api_id", "tg_api_hash"}
        assert lost == {"tg_api_hash"}
    finally:
        await db2.close()


@pytest.mark.asyncio
async def test_a_readable_secret_is_still_returned(tmp_path) -> None:
    """The stable-key case must be unchanged."""
    key = Fernet.generate_key()
    db = await _open(tmp_path / "bot.db", key)
    try:
        await db.set_setting("tg_api_hash", "abcdef0123456789abcdef0123456789")
        assert await db.get_setting("tg_api_hash") == "abcdef0123456789abcdef0123456789"
        assert db.undecryptable == set()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_non_secret_setting_is_never_affected(tmp_path) -> None:
    """`tg_api_id` is plain JSON; a key change must not touch it."""
    db_path = tmp_path / "bot.db"
    db = await _open(db_path, Fernet.generate_key())
    await db.set_setting("tg_api_id", 987654)
    await db.close()

    db2 = await _open(db_path, Fernet.generate_key())
    try:
        assert await db2.get_setting("tg_api_id") == 987654
    finally:
        await db2.close()


@pytest.mark.asyncio
async def test_overwriting_a_lost_secret_repairs_it(tmp_path) -> None:
    """Re-entering the API Hash must actually fix the deployment."""
    db_path = tmp_path / "bot.db"
    db = await _open(db_path, Fernet.generate_key())
    await db.set_setting("tg_api_hash", "old" * 10)
    await db.close()

    db2 = await _open(db_path, Fernet.generate_key())
    try:
        assert await db2.get_setting("tg_api_hash") is None
        await db2.set_setting("tg_api_hash", "new" * 10)
        assert await db2.get_setting("tg_api_hash") == "new" * 10
    finally:
        await db2.close()


# ---------------------------------------------------------------------------
# The wizard must write what it collects
# ---------------------------------------------------------------------------


def test_the_api_hash_step_persists_both_values() -> None:
    """It used to keep them in FSM state only, so a restart lost them.

    Pinned at the source because the handler needs a full aiogram Message; what
    matters is that the write happens at all.
    """
    import inspect

    from qna_bot.dashboard.handlers import wizard

    src = inspect.getsource(wizard.msg_api_hash)
    assert 'set_setting("tg_api_id"' in src
    assert 'set_setting("tg_api_hash"' in src


def test_the_env_blueprint_does_not_declare_api_credentials() -> None:
    """If this ever changes, the setup guide's instruction to enter them is wrong."""
    from pathlib import Path

    yaml_text = Path(__file__).resolve().parents[1] / "render.yaml"
    text = yaml_text.read_text(encoding="utf-8")

    assert "TG_API_ID" not in text
    assert "TG_API_HASH" not in text
    # And the two keys a blueprint deploy does ask for are still declared.
    assert "BOT_TOKEN" in text
    assert "OWNER_ID" in text
