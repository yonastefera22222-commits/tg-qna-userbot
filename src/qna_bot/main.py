"""Application entrypoint: boots storage, AI engine, Telethon userbot, Health HTTP, and Dashboard bot."""

from __future__ import annotations

import asyncio
import logging
import sys

from .ai.engine import AIEngine
from .ai.providers import PROVIDERS
from .config import EnvConfig, load_env_config
from .dashboard.bot import create_dashboard_bot
from .dashboard.context import AppContext
from .health import HealthServer
from .storage.crypto import SecretCipher
from .storage.database import Database
from .userbot.manager import UserbotManager

logger = logging.getLogger(__name__)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telethon.network").setLevel(logging.WARNING)


async def _seed_env_defaults(env: EnvConfig, db: Database) -> None:
    """Restore BOT_STATE_B64 (for Render Free ephemeral restarts) and seed optional .env keys."""
    if env.bot_state_b64 and not await db.get_setting("tg_session"):
        try:
            n_settings, n_groups = await db.import_state_b64(env.bot_state_b64)
            logger.info(
                "Restored saved configuration from BOT_STATE_B64 (%d settings, %d groups).",
                n_settings,
                n_groups,
            )
        except Exception as exc:
            logger.error("Failed to import BOT_STATE_B64: %s", exc)

    if env.owner_id is not None and await db.get_setting("owner_id") is None:
        await db.set_setting("owner_id", env.owner_id)

    if env.tg_api_id is not None and await db.get_setting("tg_api_id") is None:
        await db.set_setting("tg_api_id", env.tg_api_id)
    if env.tg_api_hash and await db.get_setting("tg_api_hash") is None:
        await db.set_setting("tg_api_hash", env.tg_api_hash)
    if env.tg_string_session and not await db.get_setting("tg_session"):
        await db.set_setting("tg_session", env.tg_string_session)

    env_keys = {
        "groq": env.groq_api_key,
        "gemini": env.gemini_api_key,
        "openrouter": env.openrouter_api_key,
    }
    first_seeded_provider: str | None = None
    for provider, key in env_keys.items():
        if key and not await db.get_provider_key(provider):
            await db.set_provider_key(provider, key)
            if first_seeded_provider is None:
                first_seeded_provider = provider

    if first_seeded_provider and await db.get_setting("primary_provider") is None:
        await db.set_setting("primary_provider", first_seeded_provider)
        await db.set_setting("primary_model", PROVIDERS[first_seeded_provider].default_model)


async def async_main() -> None:
    env = load_env_config()
    _setup_logging(env.log_level)

    cipher = SecretCipher.from_env_or_file(env.encryption_key, env.secret_key_path)
    db = Database(env.db_path, cipher)
    await db.connect()
    await _seed_env_defaults(env, db)

    ai_engine = AIEngine()
    await ai_engine.start()

    userbot = UserbotManager(db=db, ai_engine=ai_engine)
    ctx = AppContext(env=env, db=db, ai=ai_engine, userbot=userbot)

    # Start lightweight HTTP health server if PORT is provided (required on Render Free Web Service)
    health_server: HealthServer | None = None
    if env.port is not None:
        health_server = HealthServer(
            port=env.port,
            status_provider=lambda: {
                "userbot_connected": userbot.is_connected,
                "active_groups": len(userbot._enabled_groups),
                **userbot.counters,
            },
        )
        await health_server.start()

    owner_id = await ctx.get_owner_id()
    if owner_id is None:
        logger.warning("=" * 60)
        logger.warning("NO OWNER_ID SET! Send this one-time pairing code to your")
        logger.warning("Dashboard Bot in Telegram to claim ownership:")
        logger.warning("    PAIRING CODE:  %s", ctx.pairing_code)
        logger.warning("=" * 60)
    else:
        logger.info("Dashboard locked to OWNER_ID=%s", owner_id)

    # Auto-connect Telethon userbot if a session is already saved
    await userbot.start_from_storage()

    bot, dp = create_dashboard_bot(ctx)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        logger.info("Shutting down gracefully...")
        await userbot.stop()
        if health_server is not None:
            await health_server.stop()
        await ai_engine.close()
        await bot.session.close()
        await db.close()


def run() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass
