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


@contextmanager
def open_session(engine: Engine) -> Iterator[Session]:
    """Session scope that commits on success and rolls back on failure.

    A tick either lands whole or not at all: partially applied state would leave
    the universe unreplayable.
    """
    factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
