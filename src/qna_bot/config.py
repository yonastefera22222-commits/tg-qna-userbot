"""Environment configuration loader."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _parse_int(value: str | None) -> int | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _normalise_public_url(value: str | None) -> str | None:
    """Strip whitespace and trailing slashes; ignore anything that is not http(s)."""
    url = (value or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        return None
    return url


@dataclass(frozen=True)
class EnvConfig:
    bot_token: str
    owner_id: int | None
    tg_api_id: int | None
    tg_api_hash: str | None
    tg_string_session: str | None
    groq_api_key: str | None
    gemini_api_key: str | None
    openrouter_api_key: str | None
    #: Exness Trader API credentials. The bot fetches every Forex price from the
    #: owner's own Exness account, so these are what make live prices possible
    #: at all. Blank means the bot answers "no live data" rather than quoting
    #: some other venue.
    exness_api_key: str | None
    exness_private_key: str | None
    exness_account_id: str | None
    exness_gateway_url: str | None
    bot_state_b64: str | None
    port: int | None
    #: Public base URL of this deployment, used to build the browser login page.
    #: Render sets RENDER_EXTERNAL_URL automatically; PUBLIC_URL overrides it.
    public_url: str | None
    data_dir: Path
    encryption_key: str | None
    log_level: str
    #: Seconds between self-pings that keep a free host from spinning down.
    #: Render Free sleeps after 15 idle minutes of *inbound* HTTP, and the bot's
    #: Telegram polling is outbound, so it has to generate its own traffic.
    keepalive_ping_seconds: int

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    @property
    def secret_key_path(self) -> Path:
        return self.data_dir / ".secret.key"


def load_env_config() -> EnvConfig:
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError(
            "BOT_TOKEN is required in your .env file or environment variables. "
            "Create a bot with @BotFather and set BOT_TOKEN."
        )

    data_dir = Path(os.getenv("DATA_DIR", "./data")).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    return EnvConfig(
        bot_token=bot_token,
        owner_id=_parse_int(os.getenv("OWNER_ID")),
        tg_api_id=_parse_int(os.getenv("TG_API_ID")),
        tg_api_hash=(os.getenv("TG_API_HASH") or "").strip() or None,
        tg_string_session=(os.getenv("TG_STRING_SESSION") or "").strip() or None,
        groq_api_key=(os.getenv("GROQ_API_KEY") or "").strip() or None,
        gemini_api_key=(os.getenv("GEMINI_API_KEY") or "").strip() or None,
        openrouter_api_key=(os.getenv("OPENROUTER_API_KEY") or "").strip() or None,
        exness_api_key=(os.getenv("EXNESS_API_KEY") or "").strip() or None,
        exness_private_key=(os.getenv("EXNESS_PRIVATE_KEY") or "").strip() or None,
        exness_account_id=(os.getenv("EXNESS_ACCOUNT_ID") or "").strip() or None,
        exness_gateway_url=(os.getenv("EXNESS_GATEWAY_URL") or "").strip() or None,
        bot_state_b64=(os.getenv("BOT_STATE_B64") or "").strip() or None,
        port=_parse_int(os.getenv("PORT")),
        public_url=_normalise_public_url(
            os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL")
        ),
        data_dir=data_dir,
        encryption_key=(os.getenv("ENCRYPTION_KEY") or "").strip() or None,
        log_level=(os.getenv("LOG_LEVEL") or "INFO").strip().upper(),
        keepalive_ping_seconds=_parse_int(os.getenv("KEEPALIVE_PING_SECONDS")) or 600,
    )
