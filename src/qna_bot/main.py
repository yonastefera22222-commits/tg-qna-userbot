"""Application entrypoint: boots storage, AI engine, Telethon userbot, Health HTTP, and Dashboard bot."""

from __future__ import annotations

import asyncio
import logging
import sys

from .ai.engine import AIEngine
from .ai.providers import PROVIDERS
from .browser_login import BrowserLoginManager
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
    # The deployment's own env must claim ownership BEFORE any snapshot is
    # imported. Otherwise an unclaimed deployment would adopt the snapshot's
    # owner_id, and that stale id would then block this deployment's real
    # OWNER_ID below (it is only seeded when unset).
    if env.owner_id is not None and await db.get_setting("owner_id") is None:
        await db.set_setting("owner_id", env.owner_id)
        logger.info("Owner claimed from OWNER_ID=%s", env.owner_id)

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

    # A session from the snapshot is refused above when this deployment already
    # has an owner, so only fill these in when they are genuinely empty.
    if env.tg_api_id is not None:
        await db.set_setting("tg_api_id", env.tg_api_id)
    if env.tg_api_hash:
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
        if not key:
            continue
        # This deployment's own env wins over anything a pasted snapshot
        # brought in. Otherwise someone sharing a host could silently be
        # billed on the original owner's AI key.
        existing = await db.get_provider_key(provider)
        if existing and existing != key:
            logger.info(
                "Env %s_API_KEY overrides the value from the imported snapshot.",
                provider.upper(),
            )
            await db.set_provider_key(provider, key)
        elif not existing:
            await db.set_provider_key(provider, key)
        if first_seeded_provider is None:
            first_seeded_provider = provider

    # Exness credentials: env wins over the database, exactly like the AI keys,
    # so a host can be provisioned without anyone opening the dashboard.
    for setting, value in (
        ("exness_api_key", env.exness_api_key),
        ("exness_private_key", env.exness_private_key),
        ("exness_account_id", env.exness_account_id),
        ("exness_gateway_url", env.exness_gateway_url),
    ):
        if value:
            await db.set_setting(setting, value)

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
    # Forex answers are priced from the owner's own Exness account. The loader
    # is a thunk, not a snapshot, so a key saved from the dashboard applies to
    # the very next answer.
    ai_engine.attach_market_settings(
        lambda: db.get_settings(AIEngine.MARKET_SETTING_KEYS)
    )
    await ai_engine.start()

    userbot = UserbotManager(db=db, ai_engine=ai_engine)
    browser_login = BrowserLoginManager()
    ctx = AppContext(
        env=env, db=db, ai=ai_engine, userbot=userbot, browser_login=browser_login
    )

    # Start lightweight HTTP health server if PORT is provided (required on Render Free Web Service).
    # It also serves the browser login page, which is the only login route that
    # does not depend on Telegram delivering anything.
    health_server: HealthServer | None = None
    if env.port is not None:
        health_server = HealthServer(
            port=env.port,
            status_provider=lambda: {
                "userbot_connected": userbot.is_connected,
                "active_groups": len(userbot._enabled_groups),
                **userbot.counters,
            },
            login_manager=browser_login,
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
        await browser_login.close()
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
