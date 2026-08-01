"""Ordered queries shared by the resolvers.

Every query here carries an explicit ``ORDER BY``. Without one, SQLite and
Postgres are free to return rows in different orders, and since resolution order
determines both RNG stream consumption and insertion order of new rows, an
unordered query would quietly break tick replay. Treat the ordering as part of
the game rules, not a formatting detail.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from galaxysim.model.entities import Civ, Colony, Fleet, Intent, IntentStatus, World

#: Statuses an intent can be in and still need work this tick.
ACTIVE_STATUSES = (IntentStatus.QUEUED.value, IntentStatus.IN_PROGRESS.value)


def civs(session: Session, universe_id: int) -> list[Civ]:
    return list(
        session.scalars(select(Civ).where(Civ.universe_id == universe_id).order_by(Civ.id))
    )


def fleets(session: Session, universe_id: int) -> list[Fleet]:
    return list(
        session.scalars(select(Fleet).where(Fleet.universe_id == universe_id).order_by(Fleet.id))
    )


def colonies_of(session: Session, civ_id: int) -> list[Colony]:
    return list(session.scalars(select(Colony).where(Colony.civ_id == civ_id).order_by(Colony.id)))


def active_intents(session: Session, universe_id: int, kind: str) -> list[Intent]:
    """Every unresolved intent of one kind, oldest first.

    Ordering by id means a civ's own orders resolve in the sequence they were
    queued, and across civs it means whoever queued first goes first -- the one
    place submission time matters, and only as a tiebreak.
    """
    return list(
        session.scalars(
            select(Intent)
            .where(
                Intent.universe_id == universe_id,
                Intent.kind == kind,
                Intent.status.in_(ACTIVE_STATUSES),
            )
            .order_by(Intent.id)
        )
    )


def world_by_id(session: Session, world_id: int) -> World | None:
    return session.get(World, world_id)
