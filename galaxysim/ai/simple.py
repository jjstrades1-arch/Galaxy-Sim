"""A baseline AI opponent.

The rule this file exists to honour: **the AI has no privileged access.** It
submits the same intents through the same :mod:`galaxysim.engine.intents` API a
human uses, reads only what a human could read, and waits for the same ticks.
Nothing here reaches into world state directly.

That is a deliberate constraint rather than a stylistic one. An AI with a back
door makes solo mode a different game from multiplayer, and it also stops the
AI from being usable as load generation for the real thing -- a hundred of these
running against a shared universe is the closest thing to a hundred players we
can get before there are a hundred players.

Its play is intentionally simple: research always, expand when it can, build
when it is rich. It is a sparring partner for testing the loop, not an opponent
worth fearing. Smarter behaviour is a later concern.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from galaxysim.colony.expedition import Loadout
from galaxysim.colony.population import capacity
from galaxysim.materials import (
    FERTILISER,
    FLEET_COST_PER_STRENGTH,
    FOOD,
    FREIGHTER_COST_PER_CAPACITY,
    WATER,
    can_afford,
)
from galaxysim.core.seeds import rng_for
from galaxysim.core.space import distance
from galaxysim.colony.buildings import FLEET_CONSTRUCTION
from galaxysim.engine import intents
from galaxysim.engine.resolvers import governor, queries
from galaxysim.engine.resolvers.production import colony_effects
from galaxysim.worldgen.serialize import has_surface_water
from galaxysim.model.entities import (
    Civ,
    Colony,
    Fleet,
    Intent,
    IntentKind,
    IntentStatus,
    StarSystem,
    Universe,
    World,
)

#: Strength the AI builds in one go, with a colony pod attached so the new fleet
#: can expand rather than only fight.
#: Colonists a landing carries, and the stores that keep them alive while a
#: supply line is arranged. Real people and real tonnes: fifty thousand settlers
#: drink fifty tonnes of water an hour on a world that supplies none, so a month
#: of independence is tens of thousands of tonnes.
SETTLERS = 50_000.0
STORES_FOR_A_MONTH = 40_000.0

#: What a standing route carries to a colony that cannot supply itself. Sized
#: against what the settlers actually consume rather than what the freighter
#: could hold, so the route tops the outpost up rather than burying it.
ROUTE_MANIFEST = {WATER: 12_000.0, FOOD: 1_500.0, FERTILISER: 500.0}

BUILD_STRENGTH = 2.0
BUILD_RESERVE = 2.0  # only build if it can afford this many such fleets

#: A freighter is a hull built with almost no weapons and a great deal of hold.
FREIGHTER_STRENGTH = 0.5
#: Manifests of slack in the hold, so one ship can run a route without the
#: destination drinking the delivery faster than the round trip.
FREIGHTER_TRIPS_OF_SLACK = 2.0

#: Settlers the AI moves in one convoy, and the people it will not draw its
#: capital below. The capital is a reservoir, not a resource to be emptied.
MIGRATION_BATCH = 500_000.0
MIGRATION_RESERVE = 1e8
TONNES_PER_SETTLER = 0.5

#: Fleet strength the AI supports per *billion* people it governs. Fleets cost
#: upkeep every hour, so an unbounded navy bankrupts its own economy and then
#: deserts -- and a fixed cap per colony stopped meaning anything once a colony
#: held billions rather than a handful. Tying it to population keeps the AI's
#: military proportional to the economy paying for it at any scale.
MAX_STRENGTH_PER_BILLION_POP = 4.0

#: Warship strength the AI garrisons per colony it holds. Deliberately modest:
#: this is the *reason* to build a warship, where
#: :data:`MAX_STRENGTH_PER_BILLION_POP` is only the ceiling on what the economy
#: can pay upkeep for. Wanting a navy and being able to afford one are different
#: questions and the AI used to conflate them, with the result that it bought
#: hulls until it could not afford to expand.
DEFENSIVE_STRENGTH_PER_COLONY = 2.0

#: The most of the affordable ceiling the AI will let *newly built* warships
#: occupy, leaving the rest as headroom for settlers and freighters.
#:
#: This does not stop the empire settling into a steady state, and it is worth
#: being clear about why, because the reason is the design rather than a bug.
#: Settling dead worlds adds almost no *people* -- a civ can go from six
#: colonies to thirty and its population barely moves, because an outpost holds
#: tens of thousands against a homeworld's eighteen billion. So the upkeep
#: budget, which scales with population, is very nearly a constant, and an
#: expanding civilization eventually spends all of it. Where it stops is
#: therefore set by how many people it has, and the only thing in the game that
#: changes that is terraforming a world into somewhere billions can live.
#:
#: What this constant does is stop *deliberate* warship purchases from eating
#: the expansion budget first.
NAVY_SHARE_OF_UPKEEP_BUDGET = 0.55


def take_all_turns(session: Session, universe: Universe) -> int:
    """Let every AI civ in ``universe`` queue its orders. Returns how many acted."""
    ai_civs = session.scalars(
        select(Civ)
        .where(Civ.universe_id == universe.id, Civ.is_ai.is_(True))
        .order_by(Civ.id)
    ).all()

    for civ in ai_civs:
        take_turn(session, universe, civ)
    return len(ai_civs)


def take_turn(session: Session, universe: Universe, civ: Civ) -> None:
    """Queue whatever this AI wants to do next.

    Called once per tick. Every decision is seeded from the civ and the tick, so
    a solo game replays identically -- the AI must not be the thing that breaks
    determinism.
    """
    rng = rng_for(civ.seed, "ai", universe.tick_number)
    pending = _pending_by_kind(session, civ)

    if not pending.get(IntentKind.RESEARCH.value):
        intents.research(session, civ)

    _set_policies(session, civ)
    _maybe_expand(session, universe, civ, pending)
    _maybe_supply(session, universe, civ, pending)
    _maybe_migrate(session, universe, civ, pending)
    _maybe_build(session, universe, civ, pending, rng)


def _set_policies(session: Session, civ: Civ) -> None:
    """Leave colonies governed, and pick a sensible policy for each.

    The AI runs its empire the way a player with many colonies would: it does
    not micromanage labor or building queues, it delegates and chooses a
    posture. Everything that used to be duplicated here now lives in
    :mod:`galaxysim.engine.resolvers.governor`, so the AI and a player's
    governed colonies behave identically -- which is what makes solo play an
    honest rehearsal for the real thing.
    """
    colonies = queries.colonies_of(session, civ.id)
    for index, colony in enumerate(colonies):
        if not colony.is_governed:
            continue
        if colony.world.habitability < 0.4:
            policy = governor.SURVIVAL
        elif index == 0:
            # The capital carries the war effort and the shipyard.
            policy = governor.INDUSTRY_POLICY
        elif index % 3 == 2:
            policy = governor.RESEARCH_POLICY
        else:
            policy = governor.EXTRACTION_POLICY
        colony.governor_policy = policy


def _pending_by_kind(session: Session, civ: Civ) -> dict[str, list[Intent]]:
    grouped: dict[str, list[Intent]] = {}
    for intent in intents.pending(session, civ):
        grouped.setdefault(intent.kind, []).append(intent)
    return grouped


def _maybe_expand(
    session: Session, universe: Universe, civ: Civ, pending: dict[str, list[Intent]]
) -> None:
    """Send every idle colony ship at the nearest unclaimed world it can afford.

    Every, not one. A civ that has built fourteen colony ships has fourteen
    expeditions' worth of intent, and settling them one at a time means the
    other thirteen sit in orbit paying upkeep for a month. The bound on how fast
    it expands should be what its warehouses can outfit, which is a real
    constraint, rather than a queue of one, which is not.
    """
    ordered = pending.get(IntentKind.COLONIZE.value, [])
    busy_fleets = {i.payload.get("fleet_id") for i in ordered}
    claimed = {i.payload.get("world_id") for i in ordered}

    for fleet in _idle_colony_fleets(session, civ):
        if fleet.id in busy_fleets:
            continue

        target = _nearest_settleable_world(session, universe, fleet, claimed)
        if target is None:
            return  # nothing left for this fleet is nothing left for any of them
        world, system = target

        # The expedition is loaded before it leaves, so affordability is a
        # question about the warehouse it is standing next to now.
        outfitter = queries.nearest_colony(session, civ.id, fleet.position)
        if outfitter is None:
            return

        loadout = _loadout_for(world)
        if not can_afford(outfitter.stockpile, loadout.cost()):
            return  # the next ship would ask the same warehouse the same thing

        if distance(fleet.position, system.position) > 0.01:
            intents.move_fleet_to_system(session, civ, fleet.id, system)
        intents.colonize(session, civ, fleet.id, world.id, loadout=loadout)
        claimed.add(world.id)


def _loadout_for(world) -> Loadout:
    """Size an expedition to the world it is going to.

    The AI reads hostility the way the pricing model intends: it does not pay a
    surcharge for a hard world, it packs more stores. A garden world gets a light
    landing; a bare rock gets a month of independence while a supply line is
    arranged.
    """
    hostility = 1.0 - world.habitability
    if hostility <= 0.2:
        return Loadout(colonists=SETTLERS, equipment=4.0, stores=STORES_FOR_A_MONTH * 0.25)
    if hostility <= 0.6:
        return Loadout(colonists=SETTLERS, equipment=4.0, stores=STORES_FOR_A_MONTH * 0.75)
    return Loadout(colonists=SETTLERS, equipment=5.0, stores=STORES_FOR_A_MONTH * 2.0)


def _maybe_supply(
    session: Session, universe: Universe, civ: Civ, pending: dict[str, list[Intent]]
) -> None:
    """Keep a standing route running to every colony that cannot feed itself.

    This is the piece that makes AI expansion mean anything. Almost every world
    worth settling is a dead one, and a dead one lives or dies by its supply
    line -- so an AI that founds colonies without routing to them is not
    expanding, it is running a slow way to kill fifty thousand colonists at a
    time, which is exactly what it used to do.

    Ordinary orders through the ordinary API: same route intent a player issues,
    same throughput limits, no privileged access.
    """
    colonies = queries.colonies_of(session, civ.id)
    if len(colonies) < 2:
        return

    # The capital -- biggest population -- is the only place with the surplus to
    # supply anybody.
    source = max(colonies, key=lambda c: c.population)
    routed = {
        intent.payload.get("dest_colony_id")
        for intent in _all_routes(session, universe, civ)
    }

    for colony in colonies:
        if colony.id == source.id or colony.id in routed:
            continue
        if has_surface_water(colony.world.survey or {}):
            continue  # it draws its own water; it can wait
        fleet = _idle_freighter(session, civ)
        if fleet is None:
            _order_freighter(session, civ, source, pending)
            return
        intents.supply_route(session, civ, fleet.id, source.id, colony.id, dict(ROUTE_MANIFEST))
        return


def _order_freighter(
    session: Session, civ: Civ, yard: Colony, pending: dict[str, list[Intent]]
) -> None:
    """Build a hull that is mostly hold.

    A warship's incidental hold is forty tonnes; an outpost drinks that in an
    hour. Supplying anything at real scale needs a ship built for it, and hold
    is priced per tonne, so this is a real purchase rather than a free one.
    """
    if any(
        intent.payload.get("cargo_capacity") for intent in pending.get(IntentKind.BUILD_FLEET.value, [])
    ):
        return
    if FLEET_CONSTRUCTION not in colony_effects(yard).grants:
        return

    hold = sum(ROUTE_MANIFEST.values()) * FREIGHTER_TRIPS_OF_SLACK
    cost = {r: a * FREIGHTER_STRENGTH for r, a in FLEET_COST_PER_STRENGTH.items()}
    for resource, per_tonne in FREIGHTER_COST_PER_CAPACITY.items():
        cost[resource] = cost.get(resource, 0.0) + per_tonne * hold
    if not can_afford(yard.stockpile, cost):
        return

    intents.build_fleet(
        session,
        civ,
        yard.id,
        FREIGHTER_STRENGTH,
        cargo_capacity=hold,
        name=f"{civ.name} Freighter",
    )


def _maybe_migrate(
    session: Session, universe: Universe, civ: Civ, pending: dict[str, list[Intent]]
) -> None:
    """Move settlers from a full world to one with room.

    The capital opens near capacity and barely grows, so its people are a
    *reservoir* rather than a surplus that keeps accumulating. A young colony
    with headroom is where they are worth more, and shipping them there is the
    difference between a colony that matures in weeks and one that takes months
    compounding from a landing party.

    Only one convoy at a time, and only where there is real room to fill --
    dumping people faster than a colony can build for them just spreads its
    industry thinner.
    """
    if pending.get(IntentKind.MIGRATE.value):
        return
    if any(
        intent.kind == IntentKind.MIGRATE.value
        for intent in queries.active_intents(session, universe.id, IntentKind.MIGRATE.value)
        if intent.civ_id == civ.id
    ):
        return

    colonies = queries.colonies_of(session, civ.id)
    if len(colonies) < 2:
        return

    source = max(colonies, key=lambda c: c.population)
    if source.population < MIGRATION_RESERVE:
        return

    # The colony with the most unused room, as a fraction of what it could hold.
    def headroom(colony: Colony) -> float:
        ceiling = capacity(colony.world, colony.infrastructure, colony.world.habitability)
        return max(0.0, ceiling - colony.population)

    target = max((c for c in colonies if c.id != source.id), key=headroom, default=None)
    if target is None or headroom(target) < MIGRATION_BATCH:
        return

    fleet = _idle_freighter(session, civ, hold=MIGRATION_BATCH * TONNES_PER_SETTLER)
    if fleet is None:
        return

    people = min(MIGRATION_BATCH, headroom(target), source.population - MIGRATION_RESERVE)
    if people <= 0:
        return
    intents.migrate(session, civ, fleet.id, source.id, target.id, people)


def _all_routes(session: Session, universe: Universe, civ: Civ) -> list[Intent]:
    return [
        intent
        for intent in queries.active_intents(
            session, universe.id, IntentKind.SUPPLY_ROUTE.value
        )
        if intent.civ_id == civ.id
    ]


def _idle_freighter(session: Session, civ: Civ, hold: float | None = None) -> Fleet | None:
    """A ship with a *useful* hold and nothing better to do.

    "Has any hold at all" is not the test: every warship carries forty tonnes
    incidentally, which an outpost drinks in an hour. Accepting one of those as
    a freighter is how the AI ended up running supply routes that delivered less
    than the destination consumed in transit -- routes that looked busy in the
    log and starved the colony anyway.
    """
    busy = {intent.payload.get("fleet_id") for intent in intents.pending(session, civ)}
    busy |= {
        intent.payload.get("fleet_id")
        for intent in session.scalars(
            select(Intent).where(
                Intent.civ_id == civ.id,
                Intent.kind == IntentKind.SUPPLY_ROUTE.value,
                Intent.status == IntentStatus.IN_PROGRESS.value,
            )
        )
    }
    wanted = sum(ROUTE_MANIFEST.values()) if hold is None else hold
    for fleet in session.scalars(
        select(Fleet).where(Fleet.civ_id == civ.id).order_by(Fleet.id)
    ):
        if fleet.cargo_capacity >= wanted and not fleet.in_transit and fleet.id not in busy:
            return fleet
    return None


def _idle_colony_fleets(session: Session, civ: Civ) -> list[Fleet]:
    return [
        fleet
        for fleet in session.scalars(
            select(Fleet).where(Fleet.civ_id == civ.id).order_by(Fleet.id)
        )
        if fleet.colony_pods > 0 and not fleet.in_transit
    ]


def _nearest_settleable_world(
    session: Session,
    universe: Universe,
    fleet: Fleet,
    claimed: set[int] | None = None,
) -> tuple[World, StarSystem] | None:
    """Closest unclaimed world, skipping any another expedition is already after.

    Only looks at systems that already have rows -- that is, ones somebody has
    visited. That is exactly a player's star charts, so the AI is working from
    the same information a player would have and no more.
    """
    claimed = claimed or set()
    best: tuple[float, World, StarSystem] | None = None

    for system in session.scalars(
        select(StarSystem).where(StarSystem.universe_id == universe.id).order_by(StarSystem.id)
    ):
        span = distance(fleet.position, system.position)
        if best is not None and span >= best[0]:
            continue
        for world in sorted(system.worlds, key=lambda w: w.id):
            if world.colony is not None or world.id in claimed:
                continue
            # Any unclaimed world, including dead ones. That is not recklessness:
            # genuinely habitable worlds are about one in six thousand and every
            # civ's homeworld is one of them, so in any ordinary neighbourhood
            # *every* remaining world is a rock. Expanding at all means settling
            # rocks and keeping them supplied -- see :func:`_maybe_supply`.
            best = (span, world, system)
            break

    return (best[1], best[2]) if best else None


def _maybe_build(
    session: Session, universe: Universe, civ: Civ, pending: dict[str, list[Intent]], rng
) -> None:
    """Build a fleet when there is a reason to and it can afford to keep it.

    What it builds is decided, not rolled. The version of this that flipped a
    coin between a warship and a settler filled the whole standing-navy budget
    with idle warships inside two days -- and since the budget scales with
    population, and population only grows by expanding, the AI then could not
    afford the colony ship that would have let it grow. Fifty hulls in orbit,
    three hundred empty worlds in range, and a civilization that never moved
    again.

    So: expansion first, and a warship only up to what the empire it actually
    holds would want to defend.
    """
    if pending.get(IntentKind.BUILD_FLEET.value):
        return

    cost = {
        resource: amount * BUILD_STRENGTH * BUILD_RESERVE
        for resource, amount in FLEET_COST_PER_STRENGTH.items()
    }

    colonies = session.scalars(
        select(Colony).where(Colony.civ_id == civ.id).order_by(Colony.id)
    ).all()
    if not colonies:
        return

    # Build wherever the materials actually are, rather than always at the
    # capital -- with local stockpiles the capital is often not the richest.
    # A yard is required, so most colonies are not candidates at all.
    colony = next(
        (
            c
            for c in colonies
            if FLEET_CONSTRUCTION in colony_effects(c).grants and can_afford(c.stockpile, cost)
        ),
        None,
    )
    if colony is None:
        return

    fleets = session.scalars(select(Fleet).where(Fleet.civ_id == civ.id).order_by(Fleet.id)).all()

    # Affording the purchase is not the same as affording the standing bill.
    # Upkeep is charged per strength every hour, so this is the ceiling on what
    # the economy can carry rather than a rule about how big a navy may be.
    people = sum(c.population for c in colonies)
    budget = (people / 1e9) * MAX_STRENGTH_PER_BILLION_POP
    strength = sum(f.strength for f in fleets)
    if strength + BUILD_STRENGTH > budget:
        return

    # A settler if there is somewhere to send one and nothing to send.
    wants_settler = not _idle_colony_fleets(session, civ) and any(
        _nearest_settleable_world(session, universe, fleet) for fleet in fleets[:1]
    )
    if wants_settler:
        intents.build_fleet(
            session,
            civ,
            colony.id,
            BUILD_STRENGTH,
            colony_pods=1,
            name=f"{civ.name} Settler {rng.randrange(100, 999)}",
        )
        return

    # Otherwise a warship, and only up to what this many colonies is worth
    # garrisoning *and* what leaves room to keep expanding. Hulls with nothing
    # to do still cost upkeep every hour.
    wanted = min(
        len(colonies) * DEFENSIVE_STRENGTH_PER_COLONY,
        budget * NAVY_SHARE_OF_UPKEEP_BUDGET,
    )
    warships = sum(f.strength for f in fleets if f.colony_pods <= 0 and f.cargo_capacity <= 100.0)
    if warships + BUILD_STRENGTH > wanted:
        return

    intents.build_fleet(
        session,
        civ,
        colony.id,
        BUILD_STRENGTH,
        colony_pods=0,
        name=f"{civ.name} Fleet {rng.randrange(100, 999)}",
    )


def cancel_all(session: Session, civ: Civ) -> None:
    """Clear a civ's standing orders. Used when handing an AI civ to a player."""
    for intent in intents.pending(session, civ):
        intent.status = IntentStatus.CANCELLED.value
