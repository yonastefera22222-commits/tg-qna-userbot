"""Symmetric encryption helper (Fernet) for storing sessions and API keys safely at rest."""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretCipher:
    """Encrypts and decrypts secret strings stored in SQLite."""

    PREFIX = "enc::"

    def __init__(self, key_bytes: bytes) -> None:
        self._fernet = Fernet(key_bytes)

    @classmethod
    def from_env_or_file(cls, env_key: str | None, key_file: Path) -> SecretCipher:
        if env_key:
            key_bytes = env_key.strip().encode("utf-8")
            # Validate key format immediately
            return cls(key_bytes)

        if key_file.exists():
            key_bytes = key_file.read_bytes().strip()
            return cls(key_bytes)

        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_bytes = Fernet.generate_key()
        key_file.write_bytes(key_bytes)
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
        return cls(key_bytes)

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        token = self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")
        return f"{self.PREFIX}{token}"

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            return ""
        if not ciphertext.startswith(self.PREFIX):
            return ciphertext
        raw = ciphertext[len(self.PREFIX) :].encode("utf-8")
        try:
            return self._fernet.decrypt(raw).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError(
                "Failed to decrypt stored secret. Did ENCRYPTION_KEY or .secret.key change?"
            ) from exc
