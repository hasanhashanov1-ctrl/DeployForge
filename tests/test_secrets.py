import pytest

from app.services.secrets import SecretCipher, SecretCipherError


def test_secret_cipher_round_trip_and_wrong_key() -> None:
    cipher = SecretCipher("a-stable-test-key-for-deployforge")
    encrypted = cipher.encrypt("sensitive value")

    assert encrypted != "sensitive value"
    assert cipher.decrypt(encrypted) == "sensitive value"

    with pytest.raises(SecretCipherError, match="DEPLOYFORGE_SECRET_KEY"):
        SecretCipher("another-stable-key-for-testing").decrypt(encrypted)


def test_secret_cipher_rejects_short_master_key() -> None:
    with pytest.raises(SecretCipherError, match="at least 16"):
        SecretCipher("too-short")
