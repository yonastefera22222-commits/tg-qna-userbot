"""Sharing the bot with another person must never hand over the owner's account."""

from __future__ import annotations

import base64
import gzip
import json
from types import SimpleNamespace

import pytest

from qna_bot.main import _seed_env_defaults
from qna_bot.storage.database import (
    IDENTITY_SETTING_KEYS,
    SECRET_SETTING_KEYS,
    Database,
    SecretCipher,
)

OWNER_ID = 111_111
OTHER_ID = 999_999


async def _db(tmp_path, name: str) -> Database:
    db = Database(tmp_path / f"{name}.db", SecretCipher.from_env_or_file(None, tmp_path / ".k"))
    await db.connect()
    return db


async def _configured_owner(tmp_path) -> Database:
    db = await _db(tmp_path, "owner")
    await db.set_setting("owner_id", OWNER_ID)
    await db.set_setting("tg_api_id", 12345)
    await db.set_setting("tg_api_hash", "OWNER_HASH")
    await db.set_setting("tg_session", "OWNER_SESSION_STRING")
    await db.set_provider_key("groq", "gsk_OWNER")
    await db.set_provider_key("gemini", "AIza_OWNER")
    group = await db.upsert_group_metadata(-100, "Quiz Group")
    group.enabled = True
    group.detection_mode = "balanced"
    await db.save_group(group)
    return db


def _settings(encoded: str) -> dict:
    payload = json.loads(gzip.decompress(base64.urlsafe_b64decode(encoded)).decode())
    return payload["settings"]


# ---------------------------------------------------------------------------
# What each profile actually contains
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_share_profile_carries_no_credentials(tmp_path) -> None:
    db = await _configured_owner(tmp_path)
    try:
        settings = _settings(await db.export_state_b64("share"))
        for key in SECRET_SETTING_KEYS | {"owner_id", "tg_api_id"}:
            assert key not in settings, f"{key} must never be shared"
        assert settings == {}, "a share snapshot is groups + non-identity settings only"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_share_profile_still_carries_groups(tmp_path) -> None:
    db = await _configured_owner(tmp_path)
    try:
        encoded = await db.export_state_b64("share")
        payload = json.loads(gzip.decompress(base64.urlsafe_b64decode(encoded)).decode())
        assert [g["title"] for g in payload["groups"]] == ["Quiz Group"]
        assert payload["groups"][0]["enabled"] is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_backup_profile_keeps_credentials_for_the_same_deployment(tmp_path) -> None:
    db = await _configured_owner(tmp_path)
    try:
        settings = _settings(await db.export_state_b64("backup"))
        assert settings["tg_session"] == "OWNER_SESSION_STRING"
        assert settings["api_key_groq"] == "gsk_OWNER"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unknown_profile_is_rejected(tmp_path) -> None:
    db = await _configured_owner(tmp_path)
    try:
        with pytest.raises(ValueError):
            await db.export_state_b64("everything")
    finally:
        await db.close()


def test_identity_keys_cover_every_secret() -> None:
    assert SECRET_SETTING_KEYS <= IDENTITY_SETTING_KEYS
    assert {"owner_id", "tg_api_id"} <= IDENTITY_SETTING_KEYS


# ---------------------------------------------------------------------------
# Importing must never hijack the receiving deployment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_importing_a_backup_cannot_hijack_an_owned_deployment(tmp_path) -> None:
    """The dangerous path: someone pastes the *owner's* backup by mistake."""
    owner = await _configured_owner(tmp_path)
    try:
        backup = await owner.export_state_b64("backup")
    finally:
        await owner.close()

    other = await _db(tmp_path, "other")
    try:
        await other.set_setting("owner_id", OTHER_ID)
        await other.import_state_b64(backup)

        assert await other.get_setting("owner_id") == OTHER_ID, "ownership must not transfer"
        assert await other.get_setting("tg_session") is None, "the owner's session must be refused"
        assert await other.get_provider_key("groq") is None, "the owner's key must be refused"
        # Groups are safe and useful, so they still arrive.
        assert [g.title for g in await other.list_groups()] == ["Quiz Group"]
    finally:
        await other.close()


@pytest.mark.asyncio
async def test_same_deployment_can_restore_itself(tmp_path) -> None:
    """A never-claimed deployment is the legitimate restore case."""
    owner = await _configured_owner(tmp_path)
    try:
        backup = await owner.export_state_b64("backup")
    finally:
        await owner.close()

    restored = await _db(tmp_path, "restored")
    try:
        await restored.import_state_b64(backup)
        assert await restored.get_setting("owner_id") == OWNER_ID
        assert await restored.get_setting("tg_session") == "OWNER_SESSION_STRING"
        assert await restored.get_provider_key("groq") == "gsk_OWNER"
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_importing_a_share_snapshot_changes_no_identity(tmp_path) -> None:
    owner = await _configured_owner(tmp_path)
    try:
        share = await owner.export_state_b64("share")
    finally:
        await owner.close()

    other = await _db(tmp_path, "other")
    try:
        await other.set_setting("owner_id", OTHER_ID)
        n_settings, n_groups = await other.import_state_b64(share)
        assert n_groups == 1
        assert n_settings == 0
        assert await other.get_setting("owner_id") == OTHER_ID
        assert await other.get_setting("tg_session") is None
    finally:
        await other.close()


# ---------------------------------------------------------------------------
# Boot order: the deployment's own env must win
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_env_wins_over_a_pasted_backup_at_boot(tmp_path) -> None:
    """OWNER_ID is seeded before the import, so a stale snapshot cannot claim it."""
    owner = await _configured_owner(tmp_path)
    try:
        backup = await owner.export_state_b64("backup")
    finally:
        await owner.close()

    other = await _db(tmp_path, "booted")
    try:
        env = SimpleNamespace(
            owner_id=OTHER_ID,
            bot_state_b64=backup,
            tg_api_id=None,
            tg_api_hash=None,
            tg_string_session=None,
            groq_api_key=None,
            gemini_api_key=None,
            openrouter_api_key=None,
            exness_api_key=None,
            exness_private_key=None,
            exness_account_id=None,
            exness_gateway_url=None,
        )
        await _seed_env_defaults(env, other)

        assert await other.get_setting("owner_id") == OTHER_ID
        assert await other.get_setting("tg_session") is None
        assert await other.get_provider_key("groq") is None
    finally:
        await other.close()


@pytest.mark.asyncio
async def test_own_env_keys_are_not_overwritten_by_a_snapshot(tmp_path) -> None:
    owner = await _configured_owner(tmp_path)
    try:
        backup = await owner.export_state_b64("backup")
    finally:
        await owner.close()

    other = await _db(tmp_path, "envkeys")
    try:
        env = SimpleNamespace(
            owner_id=None,
            bot_state_b64=backup,
            tg_api_id=777,
            tg_api_hash="THEIR_HASH",
            tg_string_session=None,
            groq_api_key="gsk_THEIRS",
            gemini_api_key=None,
            openrouter_api_key=None,
            exness_api_key="exnsk-THEIRS",
            exness_private_key=None,
            exness_account_id=None,
            exness_gateway_url=None,
        )
        await _seed_env_defaults(env, other)
        # Env keys are seeded after the import, so they win.
        assert await other.get_provider_key("groq") == "gsk_THEIRS"
        assert await other.get_setting("tg_api_hash") == "THEIR_HASH"
    finally:
        await other.close()


@pytest.mark.asyncio
async def test_refusal_is_logged(tmp_path, caplog) -> None:
    owner = await _configured_owner(tmp_path)
    try:
        backup = await owner.export_state_b64("backup")
    finally:
        await owner.close()

    other = await _db(tmp_path, "logged")
    try:
        await other.set_setting("owner_id", OTHER_ID)
        with caplog.at_level("WARNING"):
            await other.import_state_b64(backup)
        assert any("hijack" in record.message for record in caplog.records)
    finally:
        await other.close()
