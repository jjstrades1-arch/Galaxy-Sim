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

from galaxysim.core.space import Vec3, distance
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


def total_stockpile(session: Session, civ_id: int) -> dict[str, float]:
    """Everything a civ owns, summed across its colonies.

    A reporting view only. Nothing may *spend* from this -- goods are spendable
    where they physically sit, and summing them would quietly reinstate the
    civ-wide treasury this design removed.
    """
    totals: dict[str, float] = {}
    for colony in colonies_of(session, civ_id):
        for resource, amount in sorted(colony.stockpile.items()):
            totals[resource] = totals.get(resource, 0.0) + amount
    return totals


def nearest_colony(session: Session, civ_id: int, position: Vec3) -> Colony | None:
    """The civ's colony closest to ``position``, or None if it holds none.

    Used to decide which stockpile pays for a fleet in the field. Ties break on
    colony id so two equidistant colonies always resolve the same way -- an
    arbitrary-but-fixed rule, which is what determinism needs.
    """
    colonies = colonies_of(session, civ_id)
    if not colonies:
        return None
    return min(colonies, key=lambda c: (distance(position, c.world.system.position), c.id))
