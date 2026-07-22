import traceback
from dataclasses import FrozenInstanceError
from ipaddress import ip_network

import pytest

from mail_manager.config import Settings


VALID_MAPPING = {
    "DATABASE_URL": "postgresql://mail:secret@db.example.test/mail_manager",
    "ACCESS_PASSWORD": "correct-horse-battery-staple",
    "SESSION_SECRET": "s" * 32,
    "CREDENTIALS_ENCRYPTION_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    "PUBLIC_ORIGIN": "https://mail.example.test",
}


@pytest.mark.parametrize("missing_key", tuple(VALID_MAPPING))
def test_from_mapping_requires_each_runtime_setting(missing_key):
    mapping = VALID_MAPPING.copy()
    del mapping[missing_key]

    with pytest.raises(ValueError, match=missing_key):
        Settings.from_mapping(mapping)


def test_settings_use_secure_bounded_defaults():
    settings = Settings.from_mapping(VALID_MAPPING)

    assert settings.cookie_secure is True
    assert settings.max_content_length == 2 * 1024 * 1024
    assert settings.import_limit == 1000
    assert settings.export_limit == 10000
    assert settings.bound_check_limit == 20
    assert settings.mail_limit == 100
    assert settings.trust_proxy is False
    assert settings.TRUSTED_PROXY_CIDRS == (
        ip_network("127.0.0.1/32"),
        ip_network("::1/128"),
    )


def test_settings_parse_trusted_proxy_cidrs():
    settings = Settings.from_mapping(
        {
            **VALID_MAPPING,
            "TRUSTED_PROXY_CIDRS": "10.0.0.0/8, 2001:db8::/32",
        }
    )

    assert settings.TRUSTED_PROXY_CIDRS == (
        ip_network("10.0.0.0/8"),
        ip_network("2001:db8::/32"),
    )


def test_settings_parse_positive_export_limit():
    settings = Settings.from_mapping({**VALID_MAPPING, "EXPORT_LIMIT": "250"})

    assert settings.export_limit == 250


@pytest.mark.parametrize("value", [0, -1, True, "not-an-integer"])
def test_settings_reject_invalid_export_limit(value):
    with pytest.raises(ValueError, match="EXPORT_LIMIT"):
        Settings.from_mapping({**VALID_MAPPING, "EXPORT_LIMIT": value})


@pytest.mark.parametrize(
    "value",
    ["", "not-a-network", "127.0.0.1/8", "127.0.0.1/32,,::1/128"],
)
def test_settings_reject_invalid_trusted_proxy_cidrs_without_echoing(value):
    with pytest.raises(ValueError, match="TRUSTED_PROXY_CIDRS") as error:
        Settings.from_mapping({**VALID_MAPPING, "TRUSTED_PROXY_CIDRS": value})

    if value:
        assert value not in str(error.value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("DATABASE_URL", "sqlite:///mail.db"),
        ("ACCESS_PASSWORD", "a" * 15),
        ("SESSION_SECRET", "s" * 31),
        ("CREDENTIALS_ENCRYPTION_KEY", "not-a-fernet-key"),
    ],
)
def test_settings_reject_invalid_security_values(key, value):
    mapping = {**VALID_MAPPING, key: value}

    with pytest.raises(ValueError, match=key):
        Settings.from_mapping(mapping)


def test_invalid_database_url_error_does_not_reveal_parser_input():
    secret = "database-secret"
    mapping = {**VALID_MAPPING, "DATABASE_URL": f"postgresql://db:{secret}"}

    with pytest.raises(ValueError, match="DATABASE_URL") as error:
        Settings.from_mapping(mapping)

    formatted_exception = "".join(traceback.format_exception(error.value))
    assert secret not in formatted_exception


@pytest.mark.parametrize(
    "origin",
    [
        "ftp://mail.example.test",
        "https://mail.example.test/",
        "https://mail.example.test/login",
        "https://mail.example.test?mode=admin",
        "https://mail.example.test#login",
        "https://one.example.test,https://two.example.test",
    ],
)
def test_settings_reject_non_origin_public_urls(origin):
    mapping = {**VALID_MAPPING, "PUBLIC_ORIGIN": origin}

    with pytest.raises(ValueError, match="PUBLIC_ORIGIN"):
        Settings.from_mapping(mapping)


def test_from_environ_reads_runtime_settings(monkeypatch):
    for key, value in VALID_MAPPING.items():
        monkeypatch.setenv(key, value)

    settings = Settings.from_environ()

    assert settings.DATABASE_URL == VALID_MAPPING["DATABASE_URL"]
    assert settings.PUBLIC_ORIGIN == VALID_MAPPING["PUBLIC_ORIGIN"]


def test_settings_are_immutable():
    settings = Settings.from_mapping(VALID_MAPPING)

    with pytest.raises(FrozenInstanceError):
        settings.ACCESS_PASSWORD = "a-different-password"
