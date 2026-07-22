from datetime import UTC, datetime, timedelta

import pytest

from mail_manager.rentals import (
    InvalidRentalPayload,
    RentalService,
    _serialize,
    hash_access_code,
)
from tests.fakes import FakeCredentialCipher


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


class FakeRentalRepository:
    def __init__(self):
        self.created = []
        self.revoked = []
        self.items = []
        self.ciphertext = None
        self.reset_calls = []

    def list_rentals(self, now):
        self.listed_at = now
        return self.items

    def create_rental(
        self, account_id, code_hash, ciphertext, now, expires_at
    ):
        from mail_manager.models import RentalSummary

        self.created.append((account_id, code_hash, ciphertext, now, expires_at))
        return RentalSummary(
            id=1,
            account_id=account_id or 9,
            email="pickup@example.test",
            starts_at=now,
            expires_at=expires_at,
            ended_at=None,
            revoked_at=None,
            last_access_at=None,
            access_count=0,
        )

    def get_code_ciphertext(self, rental_id, now):
        from mail_manager.rentals import RentalCodeUnavailable

        if self.ciphertext is None:
            raise RentalCodeUnavailable
        return self.ciphertext

    def reset_code(self, rental_id, code_hash, ciphertext, now):
        self.reset_calls.append((rental_id, code_hash, ciphertext, now))
        self.ciphertext = ciphertext

    def revoke(self, rental_id, now):
        self.revoked.append((rental_id, now))
        return True


def _cipher():
    return FakeCredentialCipher()


def test_access_code_hash_is_deterministic_and_hides_plaintext():
    hashed = hash_access_code("customer-code")

    assert len(hashed) == 64
    assert hashed == hash_access_code("customer-code")
    assert "customer-code" not in hashed


def test_create_absolute_expiry_normalizes_to_utc_and_hashes_code():
    repository = FakeRentalRepository()
    cipher = _cipher()
    service = RentalService(
        repository,
        cipher,
        lambda: NOW,
        code_factory=lambda: "one-time-code",
    )

    rental, code = service.create(
        {"expiryMode": "at", "expiresAt": "2026-07-22T22:00:00+08:00"},
        5,
        120,
    )

    assert code == "one-time-code"
    assert rental.account_id == 9
    assert repository.created == [
        (
            None,
            hash_access_code(code),
            "opaque-ciphertext-1",
            NOW,
            NOW + timedelta(hours=2),
        )
    ]
    assert code not in repr(repository.created)


def test_create_never_expiring_rental_stores_null_expiry():
    repository = FakeRentalRepository()
    cipher = _cipher()
    service = RentalService(
        repository,
        cipher,
        lambda: NOW,
        code_factory=lambda: "permanent-code",
    )

    rental, _ = service.create(
        {"expiryMode": "never", "accountId": 7},
        5,
        120,
    )

    assert rental.expires_at is None
    assert repository.created[-1][0] == 7
    assert repository.created[-1][4] is None
    serialized = _serialize(rental, NOW)
    assert serialized["expiryMode"] == "never"
    assert serialized["expiresAt"] is None
    assert serialized["status"] == "active"


def test_reveal_and_reset_use_encrypted_code_and_replace_hash():
    repository = FakeRentalRepository()
    cipher = _cipher()
    repository.ciphertext = cipher.encrypt("existing-code")
    service = RentalService(
        repository,
        cipher,
        lambda: NOW,
        code_factory=lambda: "replacement-code",
    )

    assert service.reveal_code(1) == "existing-code"
    replacement = service.reset_code(1)

    assert replacement == "replacement-code"
    rental_id, code_hash, ciphertext, rotated_at = repository.reset_calls[0]
    assert rental_id == 1
    assert code_hash == hash_access_code("replacement-code")
    assert ciphertext != "replacement-code"
    assert rotated_at == NOW
    assert cipher.decrypt(ciphertext) == "replacement-code"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"durationMinutes": 60},
        {"expiryMode": "at"},
        {"expiryMode": "at", "expiresAt": "not-a-date"},
        {"expiryMode": "at", "expiresAt": "2026-07-22T13:00:00"},
        {"expiryMode": "at", "expiresAt": "2026-07-22T12:04:59Z"},
        {"expiryMode": "at", "expiresAt": "2026-07-22T14:00:01Z"},
        {"expiryMode": "never", "expiresAt": "2026-07-22T13:00:00Z"},
        {"expiryMode": "never", "accountId": 0},
        {"expiryMode": "other"},
        {"expiryMode": "never", "unknown": 1},
    ],
)
def test_create_validates_expiry_modes_and_optional_account(payload):
    service = RentalService(FakeRentalRepository(), _cipher(), lambda: NOW)

    with pytest.raises(InvalidRentalPayload):
        service.create(payload, 5, 120)
