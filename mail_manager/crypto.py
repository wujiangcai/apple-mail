from cryptography.fernet import Fernet, InvalidToken


_VERSION_PREFIX = "v1:"


class CredentialCipher:
    __slots__ = ("_fernet",)

    def __init__(self, key: str) -> None:
        _require_string(key, "key")
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (UnicodeEncodeError, ValueError):
            raise ValueError("key must be a valid Fernet key") from None

    def __repr__(self) -> str:
        return "CredentialCipher()"

    def encrypt(self, plaintext: str) -> str:
        _require_string(plaintext, "plaintext")
        token = self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return f"{_VERSION_PREFIX}{token}"

    def decrypt(self, ciphertext: str) -> str:
        _require_string(ciphertext, "ciphertext")
        if not ciphertext.startswith(_VERSION_PREFIX):
            raise InvalidToken

        token = ciphertext[len(_VERSION_PREFIX) :]
        try:
            plaintext = self._fernet.decrypt(token.encode("ascii"))
            return plaintext.decode("utf-8")
        except (InvalidToken, UnicodeEncodeError, UnicodeDecodeError):
            raise InvalidToken from None


def _require_string(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
