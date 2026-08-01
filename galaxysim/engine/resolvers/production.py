"""Economy and construction.

This resolver runs for every civilization on every tick regardless of whether
anyone is logged in. Offline players must keep producing, or an async game
punishes people for sleeping.

The pacing rules live here more than anywhere else:

* Population grows **logistically** against the world's habitability, so a
  colony plateaus instead of compounding forever. Exponential growth is the
  fastest route to a get-big-quick game.
* Every civ pays **superlinear administrative drag** on its colony count
  (:meth:`Rates.colony_overhead`), applied as a divisor on output and research.
  A tenth colony makes the other nine slightly worse, so wide expansion only
  pays with matching infrastructure investment behind it.
"""

from __future__ import annotations

import math

from galaxysim.core.resources import (
    FLEET_COST_PER_STRENGTH,
    FLEET_UPKEEP_PER_STRENGTH,
    can_afford,
    deposit,
    spend,
)
from galaxysim.engine.context import TickContext
from galaxysim.engine.resolvers import queries
from galaxysim.model.entities import Civ, Colony, Fleet, IntentKind, IntentStatus


def resolve(ctx: TickContext) -> None:
    for civ in queries.civs(ctx.session, ctx.universe.id):
        _produce(ctx, civ)
    _build_fleets(ctx)


def _produce(ctx: TickContext, civ: Civ) -> None:
    colonies = queries.colonies_of(ctx.session, civ.id)
    if not colonies:
        return

    # Superlinear in colony count -- the anti-wide-expansion brake.
    drag = 1.0 / (1.0 + ctx.rates.colony_overhead(len(colonies)))

    gains: dict[str, float] = {}
    research_gain = 0.0

    for colony in colonies:
        _grow_population(ctx, colony)

        world = colony.world
        for resource, yield_factor in sorted(world.resource_yield.items()):
            per_hour = (
                ctx.rates.colony_output_per_hour
                * colony.infrastructure
                * colony.population
                * float(yield_factor)
            )
            gains[resource] = gains.get(resource, 0.0) + ctx.per_tick(per_hour) * drag

        # Research tracks population, but sublinearly: a colony twice the size
        # does not think twice as fast. Linear would make population the only
        # thing that matters, and combined with the superlinear cost curve it
        # would leave research income and cost growing at the same rate, so
        # progress would never actually slow down.
        research_gain += (
            ctx.per_tick(
                ctx.rates.research_per_colony_per_hour
                * colony.infrastructure
                * math.sqrt(max(colony.population, 0.0))
            )
            * drag
        )

    deposit(civ.resources, gains)
    civ.research_points += research_gain
    _charge_fleet_upkeep(ctx, civ)


def _charge_fleet_upkeep(ctx: TickContext, civ: Civ) -> None:
    """Bill a civ for the navy it is keeping in the field.

    A fleet you cannot pay for does not simply persist for free: unpaid ships
    desert, scaled to how badly short the treasury is. That is what makes fleet
    size a standing economic commitment rather than a one-time purchase, and it
    is why a civ cannot bank an unbounded navy the moment its economy outgrows
    its ambitions.
    """
    fleets = [f for f in queries.fleets(ctx.session, ctx.universe.id) if f.civ_id == civ.id]
    total_strength = sum(f.strength for f in fleets)
    if total_strength <= 0:
        return

    shortfall = 0.0
    for resource, per_strength in sorted(FLEET_UPKEEP_PER_STRENGTH.items()):
        owed = ctx.per_tick(per_strength * total_strength)
        available = civ.resources.get(resource, 0.0)
        paid = min(owed, available)
        civ.resources[resource] = available - paid
        if owed > 0:
            shortfall = max(shortfall, (owed - paid) / owed)

    if shortfall <= 0:
        return

    # Desertion is proportional to how much of the bill went unpaid, so a civ
    # that is slightly short bleeds slowly rather than losing its navy at once.
    attrition = shortfall * ctx.per_tick(ctx.rates.unpaid_fleet_attrition_per_hour)
    for fleet in fleets:
        fleet.strength = max(0.0, fleet.strength - fleet.strength * attrition)

    ctx.log(
        "upkeep_shortfall",
        f"Could not pay {shortfall * 100:.0f}% of fleet upkeep; ships are deserting",
        civ_id=civ.id,
        payload={"shortfall": round(shortfall, 4)},
    )


def _grow_population(ctx: TickContext, colony: Colony) -> None:
    """Logistic growth toward the world's carrying capacity."""
    world = colony.world
    capacity = world.habitability * ctx.rates.population_capacity_factor
    if capacity <= 0:
        return

    headroom = 1.0 - (colony.population / capacity)
    if headroom <= 0:
        # Over capacity (habitability can drop). Let it decay back down rather
        # than pinning it, so losing habitability actually costs something.
        colony.population = max(
            0.0,
            colony.population
            + ctx.per_tick(colony.population * ctx.rates.population_growth_per_hour * headroom),
        )
        return

    growth_per_hour = (
        colony.population
        * ctx.rates.population_growth_per_hour
        * headroom
        * (1.0 - world.hazard)
    )
    colony.population += ctx.per_tick(growth_per_hour)


def _build_fleets(ctx: TickContext) -> None:
    """Charge for and complete fleet construction.

    Resources are taken when the order starts, not when the ship lands, so a
    queued build cannot be paid for twice and cannot be cancelled for a refund
    after the fact.
    """
    for intent in queries.active_intents(
        ctx.session, ctx.universe.id, IntentKind.BUILD_FLEET.value
    ):
        civ = ctx.session.get(Civ, intent.civ_id)
        if civ is None:
            continue

        colony = ctx.session.get(Colony, intent.payload.get("colony_id", -1))
        if colony is None or colony.civ_id != civ.id:
            _fail(ctx, intent, "no such colony")
            continue

        strength = float(intent.payload.get("strength", 1.0))
        if strength <= 0:
            _fail(ctx, intent, "strength must be positive")
            continue

        if intent.status == IntentStatus.QUEUED.value:
            cost = {r: amount * strength for r, amount in FLEET_COST_PER_STRENGTH.items()}
            if not can_afford(civ.resources, cost):
                # Stay queued: the civ may be able to afford it in a later tick.
                intent.result = "insufficient resources"
                continue

            spend(civ.resources, cost)
            intent.status = IntentStatus.IN_PROGRESS.value
            intent.result = ""
            intent.payload["completes_tick"] = ctx.tick + ctx.cadence.ticks_for_hours(
                ctx.rates.fleet_build_hours * strength
            )
            ctx.log(
                "build_started",
                f"Began construction of a {strength:.1f}-strength fleet at {colony.name}",
                civ_id=civ.id,
                payload={"colony_id": colony.id, "strength": strength},
            )
            continue

        if ctx.tick >= int(intent.payload.get("completes_tick", ctx.tick)):
            position = colony.world.system.position
            fleet = Fleet(
                universe_id=ctx.universe.id,
                civ_id=civ.id,
                name=str(intent.payload.get("name") or f"{civ.name} Fleet"),
                strength=strength,
                colony_pods=int(intent.payload.get("colony_pods", 0)),
                speed_ly_per_hour=ctx.rates.base_speed_ly_per_hour,
                x=position.x,
                y=position.y,
                z=position.z,
            )
            ctx.session.add(fleet)
            intent.status = IntentStatus.COMPLETED.value
            intent.resolved_tick = ctx.tick
            ctx.log(
                "build_completed",
                f"{fleet.name} commissioned at {colony.name}",
                civ_id=civ.id,
                payload={"strength": strength},
            )


def _fail(ctx: TickContext, intent, reason: str) -> None:
    intent.status = IntentStatus.FAILED.value
    intent.resolved_tick = ctx.tick
    intent.result = reason
    ctx.log("intent_failed", f"Build order failed: {reason}", civ_id=intent.civ_id)
