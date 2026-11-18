"""Database pieces shared by zoning_db.py and scraper_db.py: SQLAlchemy Core, SQLite by default,
written to also run on Postgres."""

import re
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import CheckConstraint, Date, DateTime, Text, create_engine, event
from sqlalchemy.types import TypeDecorator

from common import BASE_DIR

RUN_STATUSES = ("running", "succeeded", "partial", "failed")
RESULT_STATUSES = ("succeeded", "partial")  # runs that have results to show or reuse
STRICT = {"sqlite_strict": True}  # enforce column types on SQLite; other databases ignore this


class Timestamp(TypeDecorator):
    """A UTC datetime. Stored as a real timestamp column (impl), except on SQLite: STRICT tables only
    allow INTEGER, REAL, TEXT, BLOB and ANY, so there it's ISO-8601 text, always with microseconds and
    in UTC so that text order is time order."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(Text())
        return super().load_dialect_impl(dialect)  # i.e. impl

    def process_bind_param(self, value, dialect):
        if value is None or dialect.name != "sqlite":
            return value
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    def process_result_value(self, value, dialect):
        return datetime.fromisoformat(value) if value is not None and dialect.name == "sqlite" else value


class Day(TypeDecorator):
    """A calendar date: a real date column, except on SQLite (see Timestamp), where it's YYYY-MM-DD text."""

    impl = Date
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(Text())
        return super().load_dialect_impl(dialect)

    def process_bind_param(self, value, dialect):
        return value.isoformat() if value is not None and dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        return date.fromisoformat(value) if value is not None and dialect.name == "sqlite" else value


def check_in(column, options):
    """A CHECK constraint limiting `column` to fixed values (SQLite has no enum type)."""
    return CheckConstraint(f"{column} IN ({', '.join(repr(o) for o in options)})")


def make_engine(url, metadata):
    """Connect to `url`, creating the SQLite folder, the database and its tables if needed."""
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _enforce_foreign_keys(dbapi_connection, _):  # SQLite leaves them unenforced by default
            dbapi_connection.execute("PRAGMA foreign_keys = ON")
    metadata.create_all(engine)
    return engine


def database_label(engine):
    """Where data is stored, for messages: a file path for SQLite (relative to the project folder
    when inside it), otherwise the URL without its password."""
    if engine.dialect.name == "sqlite":
        path = Path(engine.url.database).resolve()
        return str(path.relative_to(BASE_DIR)) if path.is_relative_to(BASE_DIR) else str(path)
    return engine.url.render_as_string(hide_password=True)


def now_utc():
    return datetime.now(timezone.utc)


def safe_name(text):
    """Make text usable in a file name: characters Windows forbids (< > : " / \\ | ? *) become '-',
    spaces become '_', and trailing dots are dropped (Windows silently strips them)."""
    return re.sub(r'[<>:"/\\|?*]', "-", text).replace(" ", "_").rstrip(".")
