"""Declarative base, engine construction and session helpers.

Solo games run against SQLite and the shared multiplayer universe runs against
Postgres, from the same models. Nothing here may use a dialect-specific type:
resource bags and intent payloads are plain JSON columns, which both backends
handle.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Declarative base for every persisted entity."""


def create_engine_for(url: str, *, echo: bool = False) -> Engine:
    """Build an engine for ``url``.

    SQLite needs two adjustments to behave like the Postgres deployment target:
    foreign keys are off by default, and its default isolation handling will not
    hold a tick's writes in one atomic transaction.
    """
    engine = create_engine(url, echo=echo, future=True)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _connection_record):  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    """Create any missing tables.

    Fine for the prototype. Once a shared universe is live this gives way to
    real migrations -- a running universe cannot be dropped and recreated.
    """
    Base.metadata.create_all(engine)


def new_session(engine: Engine) -> Session:
    """A session that does not expire its objects when a transaction commits.

    ``expire_on_commit=False`` is load-bearing rather than a convenience: it is
    what lets a session outlive a single tick, which is what
    :func:`galaxysim.engine.tick.run_ticks` relies on to stop rebuilding the
    universe from rows every hour.
    """
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)()


@contextmanager
def open_session(engine: Engine) -> Iterator[Session]:
    """Session scope that commits on success and rolls back on failure.

    A tick either lands whole or not at all: partially applied state would leave
    the universe unreplayable.
    """
    session = new_session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def transaction(session: Session) -> Iterator[Session]:
    """One atomic unit of work on a session that outlives it.

    The same commit-or-roll-back guarantee :func:`open_session` gives, without
    discarding the session afterwards. That distinction is the difference
    between a tick loop that re-reads the whole universe every hour and one that
    keeps what it already has: a session's identity map is what stops a row
    becoming a fresh Python object and a JSON column being decoded again, and
    throwing the session away throws that away too.
    """
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise


def snapshot(engine: Engine, path: str) -> None:
    """Copy a SQLite universe to a file, using SQLite's own backup.

    For long soaks, which is how this project finds out whether its economy
    works. A soak runs in memory because a file-backed engine pays disk on every
    tick and the tick loop is the whole run -- but an in-memory universe dies
    with its process, and a measurement that has to survive a process is a
    measurement that can be resumed rather than restarted.

    Safe to call mid-run: the backup API copies a consistent view without
    stopping the writer.
    """
    _backup(engine, path, restoring=False)


def restore(engine: Engine, path: str) -> None:
    """Load a universe saved by :func:`snapshot` back into ``engine``."""
    _backup(engine, path, restoring=True)


def _backup(engine: Engine, path: str, *, restoring: bool) -> None:
    import sqlite3

    if engine.dialect.name != "sqlite":
        raise ValueError("snapshot and restore are SQLite-only")

    raw = engine.raw_connection()
    try:
        # SQLAlchemy 2.0 exposes the DBAPI connection as `driver_connection`;
        # older releases only have `connection`.
        live = getattr(raw, "driver_connection", None) or raw.connection
        other = sqlite3.connect(path)
        try:
            if restoring:
                other.backup(live)
            else:
                live.backup(other)
        finally:
            other.close()
    finally:
        raw.close()
