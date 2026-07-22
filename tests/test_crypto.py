import traceback

import pytest
from cryptography.fernet import Fernet, InvalidToken

from mail_manager.crypto import CredentialCipher


def _key():
    return Fernet.generate_key().decode("ascii")


def _tamper(ciphertext):
    characters = list(ciphertext)
    index = len("v1:") + 10
    characters[index] = "A" if characters[index] != "A" else "B"
    return "".join(characters)


def test_encrypt_decrypt_round_trip_hides_plaintext():
    cipher = CredentialCipher(_key())
    plaintext = "mailbox-password"

    ciphertext = cipher.encrypt(plaintext)

    assert ciphertext.startswith("v1:")
    assert plaintext not in ciphertext
    assert cipher.decrypt(ciphertext) == plaintext


def test_encrypt_uses_randomized_tokens():
    cipher = CredentialCipher(_key())

    first = cipher.encrypt("same-password")
    second = cipher.encrypt("same-password")

    assert first != second
    assert cipher.decrypt(first) == cipher.decrypt(second) == "same-password"


def test_decrypt_rejects_tampered_ciphertext():
    cipher = CredentialCipher(_key())
    tampered = _tamper(cipher.encrypt("mailbox-password"))

    with pytest.raises(InvalidToken):
        cipher.decrypt(tampered)


def test_decrypt_rejects_ciphertext_from_another_key():
    ciphertext = CredentialCipher(_key()).encrypt("mailbox-password")

    with pytest.raises(InvalidToken):
        CredentialCipher(_key()).decrypt(ciphertext)


def test_decrypt_rejects_unknown_ciphertext_version():
    cipher = CredentialCipher(_key())
    ciphertext = cipher.encrypt("mailbox-password")
    unknown_version = f"v2:{ciphertext.removeprefix('v1:')}"

    with pytest.raises(InvalidToken):
        cipher.decrypt(unknown_version)


@pytest.mark.parametrize(
    "ciphertext", ["v1:not-a-token", "v1:\N{SNOWMAN}", "missing-version"]
)
def test_decrypt_rejects_malformed_tokens(ciphertext):
    with pytest.raises(InvalidToken):
        CredentialCipher(_key()).decrypt(ciphertext)


@pytest.mark.parametrize("plaintext", ["", "密碼-пароль-🔐"])
def test_encrypt_supports_empty_and_unicode_plaintext(plaintext):
    cipher = CredentialCipher(_key())

    assert cipher.decrypt(cipher.encrypt(plaintext)) == plaintext


class SensitiveValue:
    def __repr__(self):
        return "sensitive-value-marker"


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (lambda value: CredentialCipher(value), "key must be str"),
        (
            lambda value: CredentialCipher(_key()).encrypt(value),
            "plaintext must be str",
        ),
        (
            lambda value: CredentialCipher(_key()).decrypt(value),
            "ciphertext must be str",
        ),
    ],
)
def test_non_string_inputs_raise_redacted_type_errors(operation, message):
    with pytest.raises(TypeError, match=message) as error:
        operation(SensitiveValue())

    assert "sensitive-value-marker" not in str(error.value)


def test_repr_does_not_reveal_encryption_key():
    key = _key()

    rendered = repr(CredentialCipher(key))

    assert key not in rendered
    assert "CredentialCipher" in rendered


def test_invalid_token_errors_do_not_reveal_ciphertext_or_keys():
    key = _key()
    wrong_key = _key()
    cipher = CredentialCipher(key)
    valid_ciphertext = cipher.encrypt("traceback-secret")
    invalid_cases = [
        (cipher, f"v2:{valid_ciphertext.removeprefix('v1:')}"),
        (cipher, "v1:malformed-ciphertext-marker"),
        (CredentialCipher(wrong_key), valid_ciphertext),
    ]

    for decryptor, ciphertext in invalid_cases:
        with pytest.raises(InvalidToken) as error:
            decryptor.decrypt(ciphertext)

        formatted_exception = "".join(traceback.format_exception(error.value))
        assert str(error.value) == ""
        assert ciphertext not in formatted_exception
        assert key not in formatted_exception
        assert wrong_key not in formatted_exception
