from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib import resources

import click
import psycopg
from flask import Flask, current_app, g
from flask.cli import with_appcontext
from psycopg import Connection


def connect_database(url: str) -> Connection:
    return psycopg.connect(url)


def get_db() -> Connection:
    connection = g.get("db")
    if connection is None:
        connection = connect_database(current_app.config["DATABASE_URL"])
        g.db = connection
    return connection


def close_db(error: BaseException | None = None) -> None:
    connection = g.pop("db", None)
    if connection is None:
        return

    if error is not None:
        _run_cleanup(connection.rollback, "database rollback", error)
    _run_cleanup(connection.close, "database close", error)


def _run_cleanup(
    operation: Callable[[], object],
    description: str,
    primary_error: BaseException | None,
) -> None:
    try:
        operation()
    except BaseException as cleanup_error:
        if primary_error is None:
            raise
        primary_error.add_note(f"{description} failed: {cleanup_error!r}")


@contextmanager
def transaction(connection: Connection | None = None) -> Iterator[Connection]:
    active_connection = connection if connection is not None else get_db()
    try:
        yield active_connection
    except BaseException as error:
        _run_cleanup(active_connection.rollback, "database rollback", error)
        raise

    try:
        active_connection.commit()
    except BaseException as error:
        _run_cleanup(active_connection.rollback, "database rollback", error)
        raise


def read_schema() -> str:
    return (
        resources.files("mail_manager")
        .joinpath("schema.sql")
        .read_text(encoding="utf-8")
    )


def execute_schema(connection: Connection, sql: str) -> None:
    connection.execute(sql)


@click.command("init-db")
@with_appcontext
def init_db_command() -> None:
    connection = connect_database(current_app.config["DATABASE_URL"])
    primary_error = None
    try:
        with transaction(connection):
            execute_schema(connection, read_schema())
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _run_cleanup(connection.close, "database close", primary_error)
    click.echo("Initialized the database.")


def init_app(app: Flask) -> None:
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
