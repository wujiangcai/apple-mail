import pytest

from mail_manager import create_app
from mail_manager.config import Settings


VALID_MAPPING = {
    "DATABASE_URL": "postgresql://mail:secret@db.example.test/mail_manager",
    "ACCESS_PASSWORD": "correct-horse-battery-staple",
    "SESSION_SECRET": "s" * 32,
    "CREDENTIALS_ENCRYPTION_KEY": (
        "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
    ),
    "PUBLIC_ORIGIN": "https://mail.example.test",
}


@pytest.fixture
def frontend_client():
    app = create_app(
        {"TESTING": True, "COOKIE_SECURE": False},
        settings=Settings.from_mapping(VALID_MAPPING),
    )
    return app.test_client()


def test_root_serves_the_real_static_entrypoint(frontend_client):
    response = frontend_client.get("/")

    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert b'id="login-form"' in response.data
    assert frontend_client.get("/static/js/app.js").status_code == 200


@pytest.mark.parametrize("path", ["/", "/static/styles.css"])
def test_pages_and_assets_receive_strict_browser_headers(frontend_client, path):
    response = frontend_client.get(path)
    csp = response.headers["Content-Security-Policy"]

    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "frame-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ):
        assert directive in csp
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "camera=()" in response.headers["Permissions-Policy"]


def test_api_responses_are_never_cached(frontend_client):
    response = frontend_client.get("/api/check-auth")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
