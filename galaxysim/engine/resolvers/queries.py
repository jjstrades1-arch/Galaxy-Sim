"""Ordered queries shared by the resolvers.

Every query here carries an explicit ``ORDER BY``. Without one, SQLite and
Postgres are free to return rows in different orders, and since resolution order
determines both RNG stream consumption and insertion order of new rows, an
unordered query would quietly break tick replay. Treat the ordering as part of
the game rules, not a formatting detail.

**Everything is eager-loaded.** A resolver that touches ``colony.world.system``
or ``colony.buildings`` is doing so for every colony it looks at, so leaving
those to lazy loading means one round trip per colony per attribute. That was
most of the cost of a tick: at a hundred and twenty colonies the engine was
spending its time on SELECT statements rather than on the simulation, and it
scaled with the size of the game rather than with anything interesting.

The rule for adding a query here: if a resolver will walk the result and touch a
relationship, load it up front.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, defer, selectinload

from galaxysim.core.space import Vec3, distance
from galaxysim.model.entities import (
    Civ,
    Colony,
    Fleet,
    Intent,
    IntentStatus,
    StarSystem,
    World,
)

#: Statuses an intent can be in and still need work this tick.
ACTIVE_STATUSES = (IntentStatus.QUEUED.value, IntentStatus.IN_PROGRESS.value)

#: What a colony is almost always asked about: the world under it, the system
#: that world is in, and what has been built on it.
#:
#: The world's ``survey`` is explicitly *not* fetched. It is a large JSON
#: document, the session is per tick, and every load decodes it again -- for a
#: hundred colonies that was the largest single cost in the engine. Everything
#: the tick loop needs from it has been promoted to columns beside it
#: (:func:`galaxysim.worldgen.serialize.promoted_fields`); the document itself
#: loads on demand for the things that genuinely read it, which are the survey
#: readout and terraforming.
_COLONY_LOADS = (
    selectinload(Colony.world).options(
        defer(World.survey), selectinload(World.system)
    ),
    selectinload(Colony.buildings),
)


def civs(session: Session, universe_id: int) -> list[Civ]:
    return list(
        session.scalars(select(Civ).where(Civ.universe_id == universe_id).order_by(Civ.id))
    )


def fleets(session: Session, universe_id: int) -> list[Fleet]:
    return list(
        session.scalars(select(Fleet).where(Fleet.universe_id == universe_id).order_by(Fleet.id))
    )


def fleets_by_civ(session: Session, universe_id: int) -> dict[int, list[Fleet]]:
    """Every fleet in the universe, grouped by owner, in one query.

    For resolvers that walk civs and want each one's ships. Asking per civ meant
    re-reading and re-filtering the whole table once per civilization.
    """
    grouped: dict[int, list[Fleet]] = {}
    for fleet in fleets(session, universe_id):
        grouped.setdefault(fleet.civ_id, []).append(fleet)
    return grouped


def colonies_of(session: Session, civ_id: int) -> list[Colony]:
    return list(
        session.scalars(
            select(Colony)
            .where(Colony.civ_id == civ_id)
            .options(*_COLONY_LOADS)
            .order_by(Colony.id)
        )
    )


def colonies_by_civ(session: Session, universe_id: int) -> dict[int, list[Colony]]:
    """Every colony in the universe, grouped by owner, in one query."""
    grouped: dict[int, list[Colony]] = {}
    rows = session.scalars(
        select(Colony)
        .join(Civ, Colony.civ_id == Civ.id)
        .where(Civ.universe_id == universe_id)
        .options(*_COLONY_LOADS)
        .order_by(Colony.id)
    )
    for colony in rows:
        grouped.setdefault(colony.civ_id, []).append(colony)
    return grouped


def colonies_by_id(ctx) -> dict[int, Colony]:
    """Every colony in the universe, indexed, computed once per tick.

    For the resolvers that hold an *id* rather than an object: routes, orders,
    projects. Each of those used to reach for ``session.get`` inside its loop,
    which reads like a cheap lookup and is a database round trip -- a route
    alone took three, so a civilization supplying thirty outposts paid ninety
    an hour to move nothing new.

    Memoized on the tick, so the first resolver to ask pays one query and every
    resolver after it pays nothing.
    """
    return ctx.cached_effects(
        "colonies_by_id",
        lambda: {
            colony.id: colony
            for group in colonies_by_civ(ctx.session, ctx.universe.id).values()
            for colony in group
        },
    )


def fleets_by_id(ctx) -> dict[int, Fleet]:
    """Every fleet in the universe, indexed, computed once per tick."""
    return ctx.cached_effects(
        "fleets_by_id",
        lambda: {fleet.id: fleet for fleet in fleets(ctx.session, ctx.universe.id)},
    )


def charted_keys(ctx) -> set[tuple[int, int, int, int]]:
    """Generation keys of every system anybody has visited, in one query.

    The galaxy is a pure function, so "does this star exist" is free -- but "has
    anyone been there" is a row, and asking it one star at a time is what made
    arrival and scouting expensive. A fleet arriving asked once; the AI looking
    for somewhere to explore asked once per candidate system per colony per
    turn, which at forty candidates and thirty colonies is twelve hundred round
    trips to decide one move.

    Four integers per system, so the whole set is small even when the charted
    galaxy is not.
    """
    return ctx.cached_effects(
        "charted_keys", lambda: charted_key_set(ctx.session, ctx.universe.id)
    )


def systems_by_key(ctx) -> dict[tuple[int, int, int, int], StarSystem]:
    """Charted systems indexed by generation key, computed once per tick.

    Arrival needs the *row* for wherever a fleet just docked, and asking for it
    one fleet at a time is a round trip per arrival -- which on a civilization
    running thirty supply routes is thirty an hour to look up places it has been
    docking at all week.
    """
    return ctx.cached_effects(
        "systems_by_key",
        lambda: {
            (s.sector_i, s.sector_j, s.sector_k, s.index_in_sector): s
            for s in ctx.session.scalars(
                select(StarSystem)
                .where(StarSystem.universe_id == ctx.universe.id)
                .order_by(StarSystem.id)
            )
        },
    )


def charted_key_set(session: Session, universe_id: int) -> set[tuple[int, int, int, int]]:
    """:func:`charted_keys` without a tick, for the AI taking its turn."""
    return {
        tuple(row)
        for row in session.execute(
            select(
                StarSystem.sector_i,
                StarSystem.sector_j,
                StarSystem.sector_k,
                StarSystem.index_in_sector,
            ).where(StarSystem.universe_id == universe_id)
        )
    }


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
    """One world, without dragging its survey document along.

    Colonization asks this per pending order per tick. ``session.get`` loads
    every column, which for a world means the largest JSON object in the game --
    so a civ with a few expeditions in flight was decoding whole planets to read
    a name and a position.
    """
    return session.get(World, world_id, options=(defer(World.survey),))


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

    Ties break on colony id so two equidistant colonies always resolve the same
    way -- an arbitrary-but-fixed rule, which is what determinism needs.
    """
    return nearest_of(colonies_of(session, civ_id), position)


def nearest_of(colonies: list[Colony], position: Vec3) -> Colony | None:
    """Closest of an already-loaded list. Same rule, no query."""
    if not colonies:
        return None
    return min(colonies, key=lambda c: (distance(position, c.world.system.position), c.id))


def colonies_by_distance(
    session: Session, civ_id: int, position: Vec3, within_ly: float | None = None
) -> list[Colony]:
    """The civ's colonies, nearest first, optionally inside a radius."""
    return sorted_by_distance(colonies_of(session, civ_id), position, within_ly)


def sorted_by_distance(
    colonies: list[Colony], position: Vec3, within_ly: float | None = None
) -> list[Colony]:
    """Nearest first out of an already-loaded list.

    Used to decide which stockpiles supply a fleet in the field. Nearest *that
    can actually pay* is the right rule rather than nearest outright: a fleet
    parked over a two-week-old outpost is not unsupplied because that outpost
    has no fuel, it is supplied from the world one jump behind it, which is how
    logistics works.
    """
    ordered = sorted(
        colonies, key=lambda c: (distance(position, c.world.system.position), c.id)
    )
    if within_ly is None:
        return ordered
    return [c for c in ordered if distance(position, c.world.system.position) <= within_ly]
