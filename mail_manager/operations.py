from datetime import UTC, datetime, timedelta

import click
from flask import Flask, current_app

from mail_manager import db


@click.command("cleanup-expired")
def cleanup_expired_command():
    now = datetime.now(UTC)
    with db.transaction(db.get_db()) as connection:
        sessions = connection.execute(
            "DELETE FROM auth_sessions WHERE expires_at <= %s RETURNING session_hash",
            (now,),
        ).fetchall()
        pickup_sessions = connection.execute(
            "DELETE FROM pickup_sessions WHERE expires_at <= %s RETURNING session_hash",
            (now,),
        ).fetchall()
        login_failures = connection.execute(
            """
            DELETE FROM login_failures
            WHERE updated_at < %s
              AND (locked_until IS NULL OR locked_until <= %s)
            RETURNING client_key
            """,
            (now - timedelta(days=1), now),
        ).fetchall()
        pickup_failures = connection.execute(
            """
            DELETE FROM pickup_failures
            WHERE updated_at < %s
              AND (locked_until IS NULL OR locked_until <= %s)
            RETURNING client_key
            """,
            (now - timedelta(days=1), now),
        ).fetchall()
        leases = connection.execute(
            """
            UPDATE accounts
            SET refresh_lease_token = NULL, refresh_lease_expires_at = NULL
            WHERE refresh_lease_token IS NOT NULL
              AND refresh_lease_expires_at <= %s
            RETURNING id
            """,
            (now,),
        ).fetchall()
    click.echo(
        "Expired cleanup completed: "
        f"admin_sessions={len(sessions)} pickup_sessions={len(pickup_sessions)} "
        f"login_failures={len(login_failures)} "
        f"pickup_failures={len(pickup_failures)} leases={len(leases)}"
    )


def init_app(app: Flask):
    app.cli.add_command(cleanup_expired_command)

    @app.get("/api/admin/settings")
    def admin_settings():
        return {
            "rentalDefaultMinutes": current_app.config["RENTAL_DEFAULT_MINUTES"],
            "rentalMinMinutes": current_app.config["RENTAL_MIN_MINUTES"],
            "rentalMaxMinutes": current_app.config["RENTAL_MAX_MINUTES"],
            "importLimit": current_app.config["IMPORT_LIMIT"],
            "mailLimit": current_app.config["MAIL_LIMIT"],
        }
