"""Colonization.

A colonize order waits. If the fleet has not reached the target world yet the
order stays queued rather than failing, so a player can queue "move there, then
settle it" in one sitting and log off -- which is the whole point of an async
game.

Settling takes wall-clock hours (:attr:`Rates.colonization_hours`), charged and
timed the same way construction is.
"""

from __future__ import annotations

from galaxysim.core.resources import COLONY_COST, can_afford, spend
from galaxysim.core.space import distance
from galaxysim.engine.context import TickContext
from galaxysim.engine.resolvers import queries
from galaxysim.model.entities import Civ, Colony, Fleet, IntentKind, IntentStatus

#: How close a fleet must be to a system to settle a world in it, in light-years.
#: Not zero: positions are floats and a fleet parked "at" a system is only ever
#: approximately there.
ARRIVAL_TOLERANCE_LY = 0.01


def resolve(ctx: TickContext) -> None:
    for intent in queries.active_intents(ctx.session, ctx.universe.id, IntentKind.COLONIZE.value):
        civ = ctx.session.get(Civ, intent.civ_id)
        if civ is None:
            continue

        world = queries.world_by_id(ctx.session, intent.payload.get("world_id", -1))
        if world is None:
            _fail(ctx, intent, "no such world")
            continue

        if world.colony is not None:
            _fail(ctx, intent, f"{world.name} is already settled")
            continue

        if world.habitability <= 0:
            _fail(ctx, intent, f"{world.name} cannot support a colony")
            continue

        fleet = ctx.session.get(Fleet, intent.payload.get("fleet_id", -1))
        if fleet is None or fleet.civ_id != civ.id:
            _fail(ctx, intent, "no such fleet")
            continue

        if fleet.colony_pods <= 0:
            _fail(ctx, intent, f"{fleet.name} carries no colony pods")
            continue

        if fleet.in_transit or distance(fleet.position, world.system.position) > ARRIVAL_TOLERANCE_LY:
            # Not there yet. Wait rather than fail -- the fleet is probably on
            # its way under a move order queued at the same time.
            intent.result = "awaiting fleet arrival"
            continue

        if intent.status == IntentStatus.QUEUED.value:
            # Each world already held makes the next one dearer. Overhead taxes
            # what a sprawling civ produces; this is what slows how fast it can
            # claim in the first place.
            held = len(queries.colonies_of(ctx.session, civ.id))
            multiplier = ctx.rates.colony_cost_multiplier(held)
            cost = {resource: amount * multiplier for resource, amount in COLONY_COST.items()}

            if not can_afford(civ.resources, cost):
                intent.result = (
                    f"insufficient resources (settling world {held + 1} costs "
                    f"{multiplier:.1f}x the base)"
                )
                continue

            spend(civ.resources, cost)
            intent.status = IntentStatus.IN_PROGRESS.value
            intent.result = ""
            intent.payload["completes_tick"] = ctx.tick + ctx.cadence.ticks_for_hours(
                ctx.rates.colonization_hours
            )
            ctx.log(
                "colonization_started",
                f"Landing parties began settling {world.name}",
                civ_id=civ.id,
                payload={"world_id": world.id},
            )
            continue

        if ctx.tick >= int(intent.payload.get("completes_tick", ctx.tick)):
            fleet.colony_pods -= 1
            colony = Colony(
                world_id=world.id,
                civ_id=civ.id,
                name=str(intent.payload.get("name") or world.name),
                population=1.0,
                infrastructure=1.0,
                founded_tick=ctx.tick,
            )
            ctx.session.add(colony)
            intent.status = IntentStatus.COMPLETED.value
            intent.resolved_tick = ctx.tick
            ctx.log(
                "colony_founded",
                f"Founded {colony.name} on {world.name} ({world.world_type})",
                civ_id=civ.id,
                payload={"world_id": world.id},
            )


def _fail(ctx: TickContext, intent, reason: str) -> None:
    intent.status = IntentStatus.FAILED.value
    intent.resolved_tick = ctx.tick
    intent.result = reason
    ctx.log("intent_failed", f"Colonization failed: {reason}", civ_id=intent.civ_id)
