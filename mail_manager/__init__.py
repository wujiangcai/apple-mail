from collections.abc import Mapping
from ipaddress import ip_address

from flask import Flask, request
from werkzeug.middleware.proxy_fix import ProxyFix

from mail_manager.config import Settings
from mail_manager import db
from mail_manager import auth
from mail_manager import accounts
from mail_manager import mail
from mail_manager import rentals
from mail_manager import pickup
from mail_manager import operations


__all__ = ["Settings", "create_app"]


CONTENT_SECURITY_POLICY = "; ".join(
    (
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
    )
)


class _TrustedProxyFix:
    def __init__(self, application, trusted_proxy_cidrs):
        self._application = application
        self._proxy_application = ProxyFix(application, x_for=1, x_proto=1)
        self._trusted_proxy_cidrs = trusted_proxy_cidrs

    def __call__(self, environ, start_response):
        try:
            peer_address = ip_address(environ.get("REMOTE_ADDR", ""))
        except ValueError:
            peer_address = None
        if peer_address is not None and any(
            peer_address in network for network in self._trusted_proxy_cidrs
        ):
            return self._proxy_application(environ, start_response)
        return self._application(environ, start_response)


def create_app(
    test_config: Mapping[str, object] | None = None,
    *,
    settings: Settings | None = None,
) -> Flask:
    runtime_settings = settings if settings is not None else Settings.from_environ()
    app = Flask(__name__)

    app.config.from_mapping(
        DATABASE_URL=runtime_settings.DATABASE_URL,
        ACCESS_PASSWORD=runtime_settings.ACCESS_PASSWORD,
        SESSION_SECRET=runtime_settings.SESSION_SECRET,
        SECRET_KEY=runtime_settings.SESSION_SECRET,
        CREDENTIALS_ENCRYPTION_KEY=runtime_settings.CREDENTIALS_ENCRYPTION_KEY,
        PUBLIC_ORIGIN=runtime_settings.PUBLIC_ORIGIN,
        COOKIE_SECURE=runtime_settings.cookie_secure,
        SESSION_COOKIE_SECURE=runtime_settings.cookie_secure,
        MAX_CONTENT_LENGTH=runtime_settings.max_content_length,
        IMPORT_LIMIT=runtime_settings.import_limit,
        EXPORT_LIMIT=runtime_settings.export_limit,
        BOUND_CHECK_LIMIT=runtime_settings.bound_check_limit,
        MAIL_LIMIT=runtime_settings.mail_limit,
        RENTAL_MIN_MINUTES=runtime_settings.rental_min_minutes,
        RENTAL_MAX_MINUTES=runtime_settings.rental_max_minutes,
        RENTAL_DEFAULT_MINUTES=runtime_settings.rental_default_minutes,
        PICKUP_SESSION_MINUTES=runtime_settings.pickup_session_minutes,
        TOKEN_REFRESH_WORKERS=runtime_settings.token_refresh_workers,
        TOKEN_REFRESH_BATCH_SIZE=runtime_settings.token_refresh_batch_size,
        TOKEN_REFRESH_MAX_RUNTIME_SECONDS=(
            runtime_settings.token_refresh_max_runtime_seconds
        ),
        TRUST_PROXY=runtime_settings.trust_proxy,
        TRUSTED_PROXY_CIDRS=runtime_settings.TRUSTED_PROXY_CIDRS,
    )

    if test_config is not None:
        app.config.from_mapping(test_config)

    if app.config["TRUST_PROXY"]:
        app.wsgi_app = _TrustedProxyFix(
            app.wsgi_app,
            app.config["TRUSTED_PROXY_CIDRS"],
        )

    db.init_app(app)
    auth.init_app(app)
    accounts.init_app(app)
    mail.init_app(app)
    rentals.init_app(app)
    pickup.init_app(app)
    operations.init_app(app)

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/pickup")
    def pickup_page():
        return app.send_static_file("pickup.html")

    @app.after_request
    def secure_response(response):
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
            "microphone=(), payment=(), usb=()"
        )
        if request.is_secure:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    return app
