"""Blockade, siege and capture: what makes a war end in something.

Combat before this was fleets grinding each other down and nothing else. Two
civilizations could destroy each other's navies for a month and finish holding
exactly the worlds they started with -- so there was no reason to fight, and no
way to win. This is the part that touches the map.

Three ideas, and each one is meant to fall out of what is already there rather
than add a rule:

**A blockade is just presence.** Hold more strength over somebody's colony than
they do, while at war with them, and cargo stops moving. That is not a new
penalty; it is the design's existing dependence on supply lines finally being
worth attacking. An outpost lives on its route, so cutting the route kills it,
and the ``life_support_failing`` event already tells its owner exactly what is
happening.

**A siege wears down what is actually there.** The measure of a colony's ability
to hold out is its people and what they have built -- nothing invented, nothing
assigned. That single choice does all the balancing: a fifty-thousand-person
outpost falls in hours, and a homeworld of eighteen billion cannot be taken from
orbit in any practical time by any fleet a civilization could keep flying. War
takes a rival's frontier, never their heart. It is also what protects a player
who is asleep, which an asynchronous game has to care about more than most.

**Taking a world needs a colony pod.** Grinding the resistance to nothing leaves
a colony besieged and suffering; somebody still has to land an administration.
That makes annexation a real investment rather than a side effect of winning a
battle, makes accidental capture impossible, and means blockade-without-annex --
starving a rival rather than absorbing them -- is a strategy that exists for free.
"""

from __future__ import annotations

from galaxysim.core.space import distance
from galaxysim.engine.context import TickContext
from galaxysim.engine.resolvers import queries
from galaxysim.model.entities import Colony, IntentKind

#: How far from a colony's star a fleet still counts as being over it. Wider
#: than docking tolerance on purpose: a blockade is a cordon around a system,
#: not a ship parked on the tarmac, and requiring exact coincidence would let a
#: freighter slip past a battle fleet by rounding error.
BLOCKADE_RANGE_LY = 0.5

#: Resistance a colony has per person and per point of infrastructure.
#:
#: The ratio between them is what says a siege is fought against a *population*
#: rather than against buildings: a homeworld's eighteen billion people are four
#: orders of magnitude more of a problem than anything standing on it.
RESISTANCE_PER_PERSON = 1.0
RESISTANCE_PER_INFRASTRUCTURE = 5_000.0

#: Resistance one point of besieging strength removes per real hour.
#:
#: Set so an outpost of fifty thousand falls to a small squadron inside a day,
#: which is the frontier-raid timescale the design wants, while a homeworld is
#: untouchable by anything short of an absurd and permanently-supplied fleet.
#: Neither end is a rule; both are this number meeting a real population.
SIEGE_PER_STRENGTH_HOUR = 3_000.0

#: Fraction of its standing a colony recovers per hour once a siege lifts.
#: Recovery is slow enough that a fought-over world stays vulnerable for a while
#: -- a second push should not have to start from nothing.
RECOVERY_PER_HOUR = 0.02

#: Share of a captured colony's population lost in the taking.
CAPTURE_POPULATION_LOSS = 0.25


def standing_resistance(colony: Colony) -> float:
    """What this colony can bring to bear against being taken.

    Its people and what they have built, which is the whole point: nothing here
    is a defence stat somebody chose, so a world's ability to hold out rises
    exactly as it becomes worth taking.
    """
    return max(
        0.0,
        colony.population * RESISTANCE_PER_PERSON
        + max(0.0, colony.infrastructure) * RESISTANCE_PER_INFRASTRUCTURE,
    )


def resolve(ctx: TickContext) -> None:
    """Run blockades, sieges and captures for the tick."""
    besiegers = blockades(ctx)
    colonies = queries.colonies_by_id(ctx)

    for colony_id, (civ_id, strength) in sorted(besiegers.items()):
        colony = colonies.get(colony_id)
        if colony is None:
            continue
        _press(ctx, colony, civ_id, strength)

    _recover(ctx, colonies, besiegers)


def blockades(ctx: TickContext) -> dict[int, tuple[int, float]]:
    """Colony id -> (besieging civ, its strength), computed once per tick.

    A colony is blockaded when one hostile civilization holds more strength over
    it than its owner does. "Hostile" is the standing attack order and nothing
    else, so a fleet passing through a neutral's space is not an act of war.

    Memoized, and built from two universe-wide reads rather than a query per
    colony: a tick's cost must not scale with how much war is going on, which is
    the property ``tests/test_tick_cost.py`` exists to defend.
    """
    return ctx.cached_effects("blockades", lambda: _compute_blockades(ctx))


def _compute_blockades(ctx: TickContext) -> dict[int, tuple[int, float]]:
    hostile = _hostilities(ctx)
    if not hostile:
        return {}

    fleets = [
        fleet
        for fleet in queries.fleets(ctx.session, ctx.universe.id)
        if not fleet.in_transit and fleet.strength > 0
    ]
    if not fleets:
        return {}

    blockaded: dict[int, tuple[int, float]] = {}
    for colony in sorted(queries.colonies_by_id(ctx).values(), key=lambda c: c.id):
        position = colony.world.system.position
        present: dict[int, float] = {}
        for fleet in fleets:
            if distance(fleet.position, position) <= BLOCKADE_RANGE_LY:
                present[fleet.civ_id] = present.get(fleet.civ_id, 0.0) + fleet.strength
        if not present:
            continue

        defending = present.get(colony.civ_id, 0.0)
        # Strongest hostile present, ties broken by civ id so the outcome of a
        # three-way standoff is determined rather than dict-order.
        contenders = sorted(
            (
                (strength, -civ_id)
                for civ_id, strength in present.items()
                if civ_id != colony.civ_id
                and (civ_id, colony.civ_id) in hostile
            ),
            reverse=True,
        )
        if not contenders:
            continue
        strength, negative_id = contenders[0]
        if strength > defending:
            blockaded[colony.id] = (-negative_id, strength - defending)
    return blockaded


def _hostilities(ctx: TickContext) -> set[tuple[int, int]]:
    """(aggressor, target) pairs with a standing attack order."""
    pairs: set[tuple[int, int]] = set()
    for intent in queries.active_intents(
        ctx.session, ctx.universe.id, IntentKind.ATTACK.value
    ):
        target = intent.payload.get("target_civ_id")
        if isinstance(target, int) and target != intent.civ_id:
            pairs.add((intent.civ_id, target))
    return pairs


def _press(ctx: TickContext, colony: Colony, civ_id: int, strength: float) -> None:
    """One tick of siege against a blockaded colony."""
    if colony.resistance < 0:
        colony.resistance = standing_resistance(colony)
    if not colony.blockaded:
        colony.blockaded = True
        ctx.log(
            "blockade_established",
            f"{colony.name} is blockaded; nothing is moving on or off it",
            civ_id=colony.civ_id,
            payload={"colony_id": colony.id, "by_civ_id": civ_id},
        )

    if colony.resistance > 0:
        colony.resistance = max(
            0.0, colony.resistance - ctx.per_tick(strength * SIEGE_PER_STRENGTH_HOUR)
        )
        if colony.resistance > 0:
            return
        ctx.log(
            "colony_subdued",
            f"{colony.name} can no longer resist; it falls to whoever lands first",
            civ_id=colony.civ_id,
            payload={"colony_id": colony.id, "by_civ_id": civ_id},
        )

    _try_capture(ctx, colony, civ_id)


def _try_capture(ctx: TickContext, colony: Colony, civ_id: int) -> None:
    """Take the colony, if the besieger brought somebody to run it.

    Without a pod the colony simply stays subdued and blockaded -- which is a
    perfectly good outcome to want, and the reason starving a rival is a
    strategy rather than an oversight.
    """
    lander = next(
        (
            fleet
            for fleet in sorted(
                queries.fleets_by_civ(ctx.session, ctx.universe.id).get(civ_id, []),
                key=lambda f: f.id,
            )
            if fleet.colony_pods > 0
            and not fleet.in_transit
            and distance(fleet.position, colony.world.system.position)
            <= BLOCKADE_RANGE_LY
        ),
        None,
    )
    if lander is None:
        return

    new_owner = next(
        (
            civ
            for civ in queries.civs(ctx.session, ctx.universe.id)
            if civ.id == civ_id
        ),
        None,
    )
    if new_owner is None:
        return

    lander.colony_pods -= 1
    former = colony.civ_id
    lost = colony.population * CAPTURE_POPULATION_LOSS
    colony.population = max(0.0, colony.population - lost)
    # Through the relationship, not the raw column. Writing ``civ_id`` alone
    # produces a correct row and a wrong session: ``colony.civ`` would still
    # answer with the civilization that just lost it, and every resolver after
    # this one in the tick reads objects rather than rows.
    colony.civ = new_owner
    # A captured world is run by its new owner from scratch: whatever posture
    # the last one had is not this one's plan.
    colony.governor_policy = "balanced"
    # It is not under siege any more; it is home. Clearing both stops the next
    # tick announcing a lifted blockade to the civilization that imposed it.
    colony.resistance = -1.0
    colony.blockaded = False
    _regroup(ctx, colony)

    for observer, message in (
        (former, f"{colony.name} has been captured; {lost:,.0f} lost in the fighting"),
        (civ_id, f"{colony.name} taken, with its industry and its warehouses"),
    ):
        ctx.log(
            "colony_captured",
            message,
            civ_id=observer,
            payload={
                "colony_id": colony.id,
                "from_civ_id": former,
                "to_civ_id": civ_id,
                "population_lost": round(lost, 1),
            },
        )


def _regroup(ctx: TickContext, colony: Colony) -> None:
    """Move a captured colony between the tick's memoized per-civ groups.

    ``queries.colonies_grouped`` is computed once a tick on the promise that
    nothing changes ownership mid-tick, which was true until this module. The
    governor and production both run after the siege and both read that memo, so
    without this a world would produce for its former owner for one more hour --
    a small wrongness that would be invisible and permanent.
    """
    grouped = queries.colonies_grouped(ctx)
    for group in grouped.values():
        if colony in group:
            group.remove(colony)
    owned = grouped.setdefault(colony.civ_id, [])
    owned.append(colony)
    owned.sort(key=lambda c: c.id)


def _recover(
    ctx: TickContext, colonies: dict[int, Colony], besieged: dict[int, tuple[int, float]]
) -> None:
    """Let a colony nobody is sitting on rebuild its ability to hold out."""
    for colony_id, colony in sorted(colonies.items()):
        if colony_id in besieged:
            continue

        # The lift is announced the moment the hostile fleet is no longer there,
        # not when the colony has finished recovering: what the player needs to
        # know is that their routes run again, and they do so immediately.
        if colony.blockaded:
            colony.blockaded = False
            ctx.log(
                "blockade_lifted",
                f"{colony.name} is clear; supply lines are open again",
                civ_id=colony.civ_id,
                payload={"colony_id": colony.id},
            )

        if colony.resistance < 0:
            continue
        standing = standing_resistance(colony)
        if colony.resistance >= standing:
            # Fully recovered, and a colony that has grown since is not still
            # carrying the wear. Back to "never besieged" so the next siege
            # starts from what is actually there now.
            colony.resistance = -1.0
            continue
        colony.resistance = min(
            standing, colony.resistance + ctx.per_tick(standing * RECOVERY_PER_HOUR)
        )


def is_blockaded(ctx: TickContext, colony: Colony) -> bool:
    """Whether cargo may move at this colony. Read by logistics."""
    return colony.id in blockades(ctx)


def blockader_of(ctx: TickContext, colony: Colony) -> int | None:
    entry = blockades(ctx).get(colony.id)
    return entry[0] if entry else None


__all__ = [
    "BLOCKADE_RANGE_LY",
    "blockader_of",
    "blockades",
    "is_blockaded",
    "resolve",
    "standing_resistance",
]
