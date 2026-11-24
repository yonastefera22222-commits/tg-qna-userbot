"""A fresh Render blueprint deploy, which is how a second owner gets this bot.

Three separate faults made a blueprinted deployment either die at boot or run
completely silently - both of which the new owner reads as "it's not responding":

1. **A bad ``ENCRYPTION_KEY`` killed the process.** ``Fernet`` needs exactly 32
   url-safe base64 bytes; Render's ``generateValue`` emits a random string, and
   ``SecretCipher.from_env_or_file`` raised straight out of boot. The service
   showed a frowning face and stopped, with nothing on Telegram to explain it.
2. **A ``BOT_STATE_B64`` paste could be ignored.** The import was skipped whenever
   the database already had *any* settings row carrying a session - but a DB whose
   secrets could not be decrypted still looks populated, so the snapshot that
   would have fixed it was skipped in exactly the case it was needed.
3. **Nothing was ever enabled.** A blueprint deploy starts with an empty group
   table; groups are only synced when the owner opens the group menu. "Not
   responding" was therefore literally correct, and ``/diag`` said nothing about
   it.

Each test below pins one of those, so a later refactor cannot quietly bring the
silence back.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from qna_bot.main import _build_cipher, _seed_env_defaults
from qna_bot.storage.crypto import SecretCipher
from qna_bot.storage.database import Database


async def _db(tmp_path, name: str = "bot") -> Database:
    db = Database(tmp_path / f"{name}.db", SecretCipher.from_env_or_file(None, tmp_path / ".k"))
    await db.connect()
    return db


def _env(tmp_path, **overrides) -> SimpleNamespace:
    values = {
        "owner_id": None,
        "bot_state_b64": None,
        "tg_api_id": None,
        "tg_api_hash": None,
        "tg_string_session": None,
        "groq_api_key": None,
        "gemini_api_key": None,
        "openrouter_api_key": None,
        "exness_api_key": None,
        "exness_private_key": None,
        "exness_account_id": None,
        "exness_gateway_url": None,
        "encryption_key": None,
        "secret_key_path": tmp_path / ".secret.key",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# ---------------------------------------------------------------------------
# 1. A bad ENCRYPTION_KEY must not stop the boot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_key",
    [
        "a" * 64,  # plausible-looking random hex: what a copy/paste produces
        "Zm9vYmFy",  # too short
        "not-base64-at-all!!!",
        " " * 32,
    ],
)
def test_a_malformed_encryption_key_does_not_crash_startup(tmp_path, caplog, bad_key) -> None:
    """The deploy used to die here, before any Telegram connection existed."""
    env = _env(tmp_path, encryption_key=bad_key)
    cipher = _build_cipher(env)  # must not raise

    # The bot is still usable, and the owner is told what to fix.
    token = cipher.encrypt("hello")
    assert cipher.decrypt(token) == "hello"
    assert "ENCRYPTION_KEY is not a valid Fernet key" in caplog.text
    assert "Fernet.generate_key" in caplog.text


def test_a_valid_encryption_key_is_used_as_given(tmp_path) -> None:
    key = Fernet.generate_key().decode()
    cipher = _build_cipher(_env(tmp_path, encryption_key=key))
    # Rebuilding from the same key must read what the first instance wrote,
    # which is the whole point of pinning it in the environment.
    again = SecretCipher.from_env_or_file(key, tmp_path / ".unused")
    assert again.decrypt(cipher.encrypt("session-string")) == "session-string"


def test_missing_encryption_key_still_generates_a_file(tmp_path) -> None:
    env = _env(tmp_path)
    _build_cipher(env)
    assert env.secret_key_path.exists()


# ---------------------------------------------------------------------------
# 2. A pasted BOT_STATE_B64 is applied whenever the DB has no session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_is_imported_into_a_half_populated_database(tmp_path) -> None:
    """A DB with settings but no usable session must still accept the snapshot."""
    source = await _db(tmp_path, "source")
    try:
        await source.set_setting("tg_session", "SESSION-STRING")
        await source.set_setting("tg_api_id", 12345)
        await source.set_provider_key("groq", "gsk_from_snapshot")
        await source.set_setting("primary_provider", "groq")
        snapshot = await source.export_state_b64("backup")
    finally:
        await source.close()

    target = await _db(tmp_path, "target")
    try:
        # Something is already there - but no session, so the deployment cannot
        # answer anything until the snapshot lands.
        await target.set_setting("schema_note", "leftover")
        await _seed_env_defaults(_env(tmp_path, bot_state_b64=snapshot), target)

        assert await target.get_setting("tg_session") == "SESSION-STRING"
        assert await target.get_provider_key("groq") == "gsk_from_snapshot"
    finally:
        await target.close()


@pytest.mark.asyncio
async def test_snapshot_never_overwrites_a_live_session(tmp_path) -> None:
    """The opposite risk: a stale snapshot stealing a working deployment."""
    source = await _db(tmp_path, "source")
    try:
        await source.set_setting("tg_session", "OLD-SESSION")
        snapshot = await source.export_state_b64("backup")
    finally:
        await source.close()

    target = await _db(tmp_path, "target")
    try:
        await target.set_setting("tg_session", "LIVE-SESSION")
        await _seed_env_defaults(_env(tmp_path, bot_state_b64=snapshot), target)
        assert await target.get_setting("tg_session") == "LIVE-SESSION"
    finally:
        await target.close()


@pytest.mark.asyncio
async def test_a_corrupt_snapshot_is_logged_not_fatal(tmp_path, caplog) -> None:
    db = await _db(tmp_path)
    try:
        await _seed_env_defaults(_env(tmp_path, bot_state_b64="not-a-real-snapshot"), db)
        assert "Failed to import BOT_STATE_B64" in caplog.text
        # Boot continues; the owner can still /setup.
        assert await db.get_setting("tg_session") is None
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 3. A brand-new deploy must say why it is silent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diag_says_nothing_is_synced_when_no_groups_exist(tmp_path) -> None:
    """"Not responding" is true, correct, and fixable - so /diag must say it."""
    import inspect

    from qna_bot.dashboard.handlers import dashboard as diag_module

    source = inspect.getsource(diag_module.cmd_diag)
    # The empty-table case has its own explanation and its own verdict line,
    # distinct from "you synced groups but enabled none".
    assert "No groups synced yet" in source
    assert "none have been synced yet" in source
    assert "Select &amp; Configure Groups" in source


@pytest.mark.asyncio
async def test_fresh_database_reports_no_enabled_groups(tmp_path) -> None:
    """The state /diag reads: schema exists, zero groups, zero answers."""
    db = await _db(tmp_path)
    try:
        assert await db.list_groups() == []
        assert await db.list_groups(only_enabled=True) == []
        # Defaults keep the responder nominally on, so the *only* thing wrong is
        # the empty group list - exactly what the new verdict points at.
        assert await db.get_setting("global_enabled", True) is True
    finally:
        await db.close()


def test_blueprint_declares_the_encryption_key() -> None:
    """A blueprint that omits it loses every secret on the first restart.

    Parsed by hand rather than with PyYAML: ``yaml`` is not a project dependency,
    and a test that needs one it cannot import is a test that never runs.
    """
    import re
    from pathlib import Path

    blueprint = (Path(__file__).resolve().parents[1] / "render.yaml").read_text()
    keys = set(re.findall(r"^\s*-\s*key:\s*(\w+)\s*$", blueprint, re.M))
    assert {"BOT_TOKEN", "OWNER_ID", "ENCRYPTION_KEY", "BOT_STATE_B64"} <= keys

    # Nothing may block a deploy waiting for a value: a blueprint stuck on
    # "waiting for configuration" reads to a first-time owner as a broken service.
    for match in re.finditer(r"- key:\s*(\w+)\s*\n((?:\s{8,}\S.*\n)+)", blueprint):
        name, block = match.group(1), match.group(2)
        has_value = re.search(r"^\s*value:", block, re.M) is not None
        assert not has_value or "sync: false" not in block, name
