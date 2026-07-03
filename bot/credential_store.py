from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from bot.config import Settings


class CredentialStoreError(RuntimeError):
    pass


class CredentialStore:
    def __init__(self, settings: Settings) -> None:
        configured = settings.credential_encryption_key
        if configured is None:
            self._fernet: Fernet | None = None
            return
        try:
            self._fernet = Fernet(configured.get_secret_value().encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise CredentialStoreError(
                "CREDENTIAL_ENCRYPTION_KEY is not a valid Fernet key"
            ) from exc

    @property
    def available(self) -> bool:
        return self._fernet is not None

    def encrypt(self, value: str) -> str:
        if self._fernet is None:
            raise CredentialStoreError("credential encryption is not configured")
        normalized = value.strip()
        if not normalized:
            raise CredentialStoreError("credential cannot be empty")
        return self._fernet.encrypt(normalized.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if self._fernet is None:
            raise CredentialStoreError("credential encryption is not configured")
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise CredentialStoreError("stored credential cannot be decrypted") from exc
