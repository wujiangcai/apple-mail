from dataclasses import dataclass, field
from datetime import datetime


class MailManagerError(Exception):
    """Base class for expected mail manager domain errors."""


@dataclass(frozen=True)
class AccountMetadata:
    id: int
    email: str
    last_refresh_time: datetime | None
    bound: bool
    bound_at: datetime | None
    token_valid: bool
    rentable: bool = True
    rented: bool = False


@dataclass(frozen=True)
class RevealedCredentials:
    email: str
    password: str = field(repr=False)
    client_id: str
    refresh_token: str = field(repr=False)


@dataclass(frozen=True)
class EncryptedAccount:
    email: str
    email_normalized: str
    password_ciphertext: str = field(repr=False)
    client_id: str
    refresh_token_ciphertext: str = field(repr=False)


@dataclass(frozen=True)
class EncryptedCredentials:
    email: str
    password_ciphertext: str = field(repr=False)
    client_id: str
    refresh_token_ciphertext: str = field(repr=False)


@dataclass(frozen=True)
class MailMessage:
    id: str
    subject: str
    sender: str
    received_at: datetime
    body_preview: str


@dataclass(frozen=True)
class MailView:
    id: str
    subject: str
    sender: str
    date: str
    body_preview: str
    html: str = field(repr=False)


@dataclass(frozen=True)
class AccountSummary:
    id: int
    email: str


@dataclass(frozen=True)
class RefreshClaim:
    client_id: str
    refresh_token_ciphertext: str = field(repr=False)


@dataclass(frozen=True)
class RentalSummary:
    id: int
    account_id: int
    email: str
    starts_at: datetime
    expires_at: datetime | None
    ended_at: datetime | None
    revoked_at: datetime | None
    last_access_at: datetime | None
    access_count: int


@dataclass(frozen=True)
class PickupContext:
    rental_id: int
    account_id: int
    email: str
    expires_at: datetime
    rental_expires_at: datetime | None = None


@dataclass(frozen=True)
class PickupSession:
    session_hash: str
    csrf_hash: str
    context: PickupContext
