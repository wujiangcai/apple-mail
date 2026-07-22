import pytest
from flask import Flask

from mail_manager import create_app
from mail_manager.config import Settings


VALID_MAPPING = {
    "DATABASE_URL": "postgresql://mail:secret@db.example.test/mail_manager",
    "ACCESS_PASSWORD": "correct-horse-battery-staple",
    "SESSION_SECRET": "s" * 32,
    "CREDENTIALS_ENCRYPTION_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    "PUBLIC_ORIGIN": "https://mail.example.test",
}


def test_create_app_returns_configured_flask_application():
    settings = Settings.from_mapping(VALID_MAPPING)
    app = create_app(
        {
            "TESTING": True,
            "MAX_CONTENT_LENGTH": 512,
            "SESSION_COOKIE_SECURE": False,
        },
        settings=settings,
    )

    assert isinstance(app, Flask)
    assert app.config["TESTING"] is True
    assert app.name == "mail_manager"
    assert app.config["DATABASE_URL"] == settings.DATABASE_URL
    assert app.config["ACCESS_PASSWORD"] == settings.ACCESS_PASSWORD
    assert app.config["SECRET_KEY"] == settings.SESSION_SECRET
    assert app.config["CREDENTIALS_ENCRYPTION_KEY"] == settings.CREDENTIALS_ENCRYPTION_KEY
    assert app.config["PUBLIC_ORIGIN"] == settings.PUBLIC_ORIGIN
    assert app.config["MAX_CONTENT_LENGTH"] == 512
    assert app.config["SESSION_COOKIE_SECURE"] is False


def test_test_config_cannot_bypass_runtime_settings(monkeypatch):
    for key in VALID_MAPPING:
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(ValueError, match="DATABASE_URL"):
        create_app({**VALID_MAPPING, "TESTING": True})
