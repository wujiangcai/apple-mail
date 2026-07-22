import os
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network, ip_network
from urllib.parse import urlsplit

from cryptography.fernet import Fernet


_REQUIRED_SETTINGS = (
    "DATABASE_URL",
    "ACCESS_PASSWORD",
    "SESSION_SECRET",
    "CREDENTIALS_ENCRYPTION_KEY",
    "PUBLIC_ORIGIN",
)
_DEFAULT_TRUSTED_PROXY_CIDRS = (
    ip_network("127.0.0.1/32"),
    ip_network("::1/128"),
)


@dataclass(frozen=True)
class Settings:
    DATABASE_URL: str
    ACCESS_PASSWORD: str
    SESSION_SECRET: str
    CREDENTIALS_ENCRYPTION_KEY: str
    PUBLIC_ORIGIN: str
    cookie_secure: bool = True
    max_content_length: int = 2 * 1024 * 1024
    import_limit: int = 1000
    export_limit: int = 10000
    bound_check_limit: int = 20
    mail_limit: int = 100
    rental_min_minutes: int = 5
    rental_max_minutes: int = 10080
    rental_default_minutes: int = 60
    pickup_session_minutes: int = 60
    trust_proxy: bool = False
    TRUSTED_PROXY_CIDRS: tuple[IPv4Network | IPv6Network, ...] = (
        _DEFAULT_TRUSTED_PROXY_CIDRS
    )

    def __post_init__(self) -> None:
        for name in _REQUIRED_SETTINGS:
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} is required")

        _validate_database_url(self.DATABASE_URL)

        if len(self.ACCESS_PASSWORD) < 16:
            raise ValueError("ACCESS_PASSWORD must be at least 16 characters")
        if len(self.SESSION_SECRET) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters")

        try:
            Fernet(self.CREDENTIALS_ENCRYPTION_KEY.encode("ascii"))
        except (TypeError, ValueError, UnicodeEncodeError):
            raise ValueError(
                "CREDENTIALS_ENCRYPTION_KEY must be a valid Fernet key"
            ) from None

        _validate_public_origin(self.PUBLIC_ORIGIN)
        if self.rental_min_minutes > self.rental_max_minutes:
            raise ValueError("RENTAL_MIN_MINUTES must not exceed RENTAL_MAX_MINUTES")
        if not self.rental_min_minutes <= self.rental_default_minutes <= self.rental_max_minutes:
            raise ValueError("RENTAL_DEFAULT_MINUTES must be within the rental limits")

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> "Settings":
        required = {}
        for name in _REQUIRED_SETTINGS:
            if name not in mapping:
                raise ValueError(f"{name} is required")
            required[name] = mapping[name]

        return cls(
            **required,
            cookie_secure=_read_bool(mapping, "cookie_secure", "COOKIE_SECURE", True),
            max_content_length=_read_positive_int(
                mapping,
                "max_content_length",
                "MAX_CONTENT_LENGTH",
                2 * 1024 * 1024,
            ),
            import_limit=_read_positive_int(
                mapping, "import_limit", "IMPORT_LIMIT", 1000
            ),
            export_limit=_read_positive_int(
                mapping, "export_limit", "EXPORT_LIMIT", 10000
            ),
            bound_check_limit=_read_positive_int(
                mapping, "bound_check_limit", "BOUND_CHECK_LIMIT", 20
            ),
            mail_limit=_read_positive_int(mapping, "mail_limit", "MAIL_LIMIT", 100),
            rental_min_minutes=_read_positive_int(
                mapping, "rental_min_minutes", "RENTAL_MIN_MINUTES", 5
            ),
            rental_max_minutes=_read_positive_int(
                mapping, "rental_max_minutes", "RENTAL_MAX_MINUTES", 10080
            ),
            rental_default_minutes=_read_positive_int(
                mapping, "rental_default_minutes", "RENTAL_DEFAULT_MINUTES", 60
            ),
            pickup_session_minutes=_read_positive_int(
                mapping, "pickup_session_minutes", "PICKUP_SESSION_MINUTES", 60
            ),
            trust_proxy=_read_bool(mapping, "trust_proxy", "TRUST_PROXY", False),
            TRUSTED_PROXY_CIDRS=_read_trusted_proxy_cidrs(mapping),
        )

    @classmethod
    def from_environ(cls) -> "Settings":
        return cls.from_mapping(os.environ)


def _validate_database_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("DATABASE_URL must be a valid PostgreSQL URL") from None

    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not parsed.netloc
        or not parsed.hostname
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ValueError("DATABASE_URL must be a valid PostgreSQL URL")


def _validate_public_origin(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("PUBLIC_ORIGIN must be a single http/https origin") from None

    if (
        any(character in value for character in ", \t\r\n?#")
        or parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ValueError("PUBLIC_ORIGIN must be a single http/https origin")


def _mapping_value(
    mapping: Mapping[str, object],
    field_name: str,
    environment_name: str,
    default: object,
) -> object:
    if field_name in mapping:
        return mapping[field_name]
    return mapping.get(environment_name, default)


def _read_bool(
    mapping: Mapping[str, object],
    field_name: str,
    environment_name: str,
    default: bool,
) -> bool:
    value = _mapping_value(mapping, field_name, environment_name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{environment_name} must be a boolean")


def _read_positive_int(
    mapping: Mapping[str, object],
    field_name: str,
    environment_name: str,
    default: int,
) -> int:
    value = _mapping_value(mapping, field_name, environment_name, default)
    if isinstance(value, bool):
        raise ValueError(f"{environment_name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{environment_name} must be a positive integer") from None
    if parsed <= 0:
        raise ValueError(f"{environment_name} must be a positive integer")
    return parsed


def _read_trusted_proxy_cidrs(
    mapping: Mapping[str, object],
) -> tuple[IPv4Network | IPv6Network, ...]:
    value = _mapping_value(
        mapping,
        "trusted_proxy_cidrs",
        "TRUSTED_PROXY_CIDRS",
        _DEFAULT_TRUSTED_PROXY_CIDRS,
    )
    if isinstance(value, str):
        entries = value.split(",")
    else:
        try:
            entries = tuple(value)
        except TypeError:
            raise ValueError(
                "TRUSTED_PROXY_CIDRS must contain valid IP networks"
            ) from None

    if not entries:
        raise ValueError("TRUSTED_PROXY_CIDRS must contain valid IP networks")

    networks = []
    try:
        for entry in entries:
            if isinstance(entry, (IPv4Network, IPv6Network)):
                networks.append(entry)
                continue
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError
            networks.append(ip_network(entry.strip()))
    except ValueError:
        raise ValueError("TRUSTED_PROXY_CIDRS must contain valid IP networks") from None
    return tuple(networks)
