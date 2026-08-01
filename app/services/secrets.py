import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr


class SecretCipherError(RuntimeError):
    pass


class SecretCipher:
    def __init__(self, secret_key: SecretStr | str) -> None:
        value = secret_key.get_secret_value() if isinstance(secret_key, SecretStr) else secret_key
        if len(value) < 16:
            raise SecretCipherError("DEPLOYFORGE_SECRET_KEY must contain at least 16 characters")
        derived_key = base64.urlsafe_b64encode(hashlib.sha256(value.encode("utf-8")).digest())
        self._fernet = Fernet(derived_key)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise SecretCipherError(
                "Cannot decrypt a stored project variable; verify DEPLOYFORGE_SECRET_KEY"
            ) from exc
