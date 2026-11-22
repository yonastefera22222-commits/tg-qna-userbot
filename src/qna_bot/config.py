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
    bot_state_b64: str | None
    port: int | None
    data_dir: Path
    encryption_key: str | None
    log_level: str

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
        bot_state_b64=(os.getenv("BOT_STATE_B64") or "").strip() or None,
        port=_parse_int(os.getenv("PORT")),
        data_dir=data_dir,
        encryption_key=(os.getenv("ENCRYPTION_KEY") or "").strip() or None,
        log_level=(os.getenv("LOG_LEVEL") or "INFO").strip().upper(),
    )
