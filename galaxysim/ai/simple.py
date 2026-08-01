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
from galaxysim.core.resources import FLEET_COST_PER_STRENGTH, can_afford
from galaxysim.core.seeds import rng_for
from galaxysim.core.space import distance
from galaxysim.colony.buildings import FLEET_CONSTRUCTION
from galaxysim.engine import intents
from galaxysim.engine.resolvers import governor, queries
from galaxysim.engine.resolvers.production import colony_effects
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
BUILD_STRENGTH = 2.0
BUILD_RESERVE = 2.0  # only build if it can afford this many such fleets

#: Fleet strength the AI is willing to support per colony. Fleets cost upkeep
#: every hour, so an unbounded navy bankrupts its own economy and then deserts.
#: The first pacing run had the AI sitting on 39 fleets it had no use for; this
#: keeps its military tied to the economy actually paying for it.
MAX_STRENGTH_PER_COLONY = 4.0


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
    _maybe_build(session, civ, pending, rng)


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
        if colony.world.habitability < 0.5:
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
    """Send an idle colony ship at the nearest unclaimed habitable world."""
    if pending.get(IntentKind.COLONIZE.value):
        return  # already settling something

    fleet = _idle_colony_fleet(session, civ)
    if fleet is None:
        return

    target = _nearest_settleable_world(session, universe, fleet)
    if target is None:
        return

    world, system = target

    # The expedition is outfitted wherever the ship is, so affordability is a
    # question about that colony's stockpile, not about the civ as a whole.
    outfitter = queries.nearest_colony(session, civ.id, fleet.position)
    if outfitter is None:
        return

    loadout = _loadout_for(world)
    if not can_afford(outfitter.stockpile, loadout.cost()):
        return
    if distance(fleet.position, system.position) > 0.01:
        intents.move_fleet_to_system(session, civ, fleet.id, system)
    intents.colonize(session, civ, fleet.id, world.id, loadout=loadout)


def _loadout_for(world) -> Loadout:
    """Size an expedition to the world it is going to.

    The AI reads hostility the way the pricing model intends: it does not pay a
    surcharge for a hard world, it packs more stores. A garden world gets a
    light landing; a bare rock gets enough air to last while a supply line is
    arranged.
    """
    hostility = 1.0 - world.habitability
    if hostility <= 0.2:
        return Loadout(colonists=3.0, equipment=4.0, stores=20.0)
    if hostility <= 0.6:
        return Loadout(colonists=3.0, equipment=4.0, stores=60.0)
    return Loadout(colonists=2.0, equipment=3.0, stores=150.0)


def _idle_colony_fleet(session: Session, civ: Civ) -> Fleet | None:
    for fleet in session.scalars(
        select(Fleet).where(Fleet.civ_id == civ.id).order_by(Fleet.id)
    ):
        if fleet.colony_pods > 0 and not fleet.in_transit:
            return fleet
    return None


def _nearest_settleable_world(
    session: Session, universe: Universe, fleet: Fleet
) -> tuple[World, StarSystem] | None:
    """Closest habitable, unclaimed world.

    Only looks at systems that already have rows -- that is, ones somebody has
    visited. Once lazy generation lands (step 4) this is exactly the AI's
    equivalent of a player's star charts, so it stays honest.
    """
    best: tuple[float, World, StarSystem] | None = None

    for system in session.scalars(
        select(StarSystem).where(StarSystem.universe_id == universe.id).order_by(StarSystem.id)
    ):
        span = distance(fleet.position, system.position)
        if best is not None and span >= best[0]:
            continue
        for world in sorted(system.worlds, key=lambda w: w.id):
            # Habitable worlds only. Uninhabitable ones are settleable now, but
            # they survive on a supply route, and this AI does not yet run any --
            # it would simply be founding colonies to watch them suffocate.
            if world.habitability > 0 and world.colony is None:
                best = (span, world, system)
                break

    return (best[1], best[2]) if best else None


def _maybe_build(session: Session, civ: Civ, pending: dict[str, list[Intent]], rng) -> None:
    """Build a fleet when comfortably able to afford one, and to keep it."""
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

    # Affording the purchase is not the same as affording the standing bill.
    strength = sum(
        f.strength
        for f in session.scalars(select(Fleet).where(Fleet.civ_id == civ.id))
    )
    if strength + BUILD_STRENGTH > len(colonies) * MAX_STRENGTH_PER_COLONY:
        return

    intents.build_fleet(
        session,
        civ,
        colony.id,
        BUILD_STRENGTH,
        # Sometimes a warship, sometimes a settler. Enough variation that the
        # AI does not lock into one shape of play.
        colony_pods=1 if rng.random() < 0.5 else 0,
        name=f"{civ.name} Fleet {rng.randrange(100, 999)}",
    )


def cancel_all(session: Session, civ: Civ) -> None:
    """Clear a civ's standing orders. Used when handing an AI civ to a player."""
    for intent in intents.pending(session, civ):
        intent.status = IntentStatus.CANCELLED.value
