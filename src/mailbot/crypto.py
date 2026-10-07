"""Secret handling: encryption of mailbox passwords and signing of unsubscribe tokens.

One user-visible key (``MAILBOT_SECRET_KEY``, a Fernet key) feeds both. The signing key is a
separate HMAC derivation so the same bytes are never used for two different purposes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken

from .errors import ConfigError


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


class SecretBox:
    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode("ascii"))
            raw = base64.urlsafe_b64decode(key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise ConfigError(
                "MAILBOT_SECRET_KEY is not a valid Fernet key. Generate one with `mailbot gen-key`."
            ) from exc
        self._signing_key = hmac.new(raw, b"mailbot/signing/v1", hashlib.sha256).digest()

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ConfigError(
                "Cannot decrypt a stored password: MAILBOT_SECRET_KEY differs from the key it "
                "was encrypted with. Restore the old key or re-enter the account password."
            ) from exc

    def sign(self, message: str) -> str:
        """Return a 128-bit URL-safe MAC of ``message``."""
        digest = hmac.new(self._signing_key, message.encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest[:16]).decode("ascii").rstrip("=")

    def verify(self, message: str, signature: str) -> bool:
        return hmac.compare_digest(self.sign(message), signature)
