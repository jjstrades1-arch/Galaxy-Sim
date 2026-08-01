"""Colonization.

A colonize order waits. If the fleet has not reached the target world yet the
order stays queued rather than failing, so a player can queue "move there, then
settle it" in one sitting and log off -- which is the whole point of an async
game.

Settling takes wall-clock hours (:attr:`Rates.colonization_hours`), charged and
timed the same way construction is.
"""

from __future__ import annotations

from galaxysim.colony.expedition import Loadout, assess
from galaxysim.colony.labor import balanced_allocation
from galaxysim.materials import can_afford, spend
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

        # Note what is *not* checked here: habitability. A world with none can
        # be settled, on wholly artificial life support, and lives or dies by
        # its supply line. Since barren worlds and gas giants carry the richest
        # yields in the table, that is where the interesting decisions are.

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

        loadout = Loadout.from_payload(intent.payload)

        if intent.status == IntentStatus.QUEUED.value:
            # An expedition is outfitted somewhere specific, and what it carries
            # is what it costs. There is no flat fee and no hostility surcharge:
            # a hard world is expensive because surviving it takes more cargo.
            outfitter = queries.nearest_colony(ctx.session, civ.id, fleet.position)
            if outfitter is None:
                _fail(ctx, intent, "no colony available to outfit the expedition")
                continue

            cost = loadout.cost()
            if not can_afford(outfitter.stockpile, cost):
                intent.result = f"insufficient resources at {outfitter.name} to outfit"
                continue

            spend(outfitter.stockpile, cost)
            intent.status = IntentStatus.IN_PROGRESS.value
            intent.result = ""
            intent.payload["completes_tick"] = ctx.tick + ctx.cadence.ticks_for_hours(
                ctx.rates.colonization_hours
            )

            assessment = assess(loadout, world, ctx.rates)
            ctx.log(
                "colonization_started",
                f"Landing parties began settling {world.name}. {assessment.summary()}",
                civ_id=civ.id,
                payload={
                    "world_id": world.id,
                    "survival_hours": assessment.survival_hours,
                    "self_sufficient": assessment.self_sufficient,
                },
            )
            continue

        if ctx.tick >= int(intent.payload.get("completes_tick", ctx.tick)):
            fleet.colony_pods -= 1
            # The expedition becomes the colony: colonists are its population,
            # equipment its infrastructure, stores its opening stockpile.
            colony = Colony(
                world_id=world.id,
                civ_id=civ.id,
                name=str(intent.payload.get("name") or world.name),
                population=loadout.colonists,
                infrastructure=loadout.starting_infrastructure(),
                founded_tick=ctx.tick,
                stockpile=loadout.starting_stockpile(),
                labor=balanced_allocation(),
            )
            ctx.session.add(colony)
            intent.status = IntentStatus.COMPLETED.value
            intent.resolved_tick = ctx.tick

            assessment = assess(loadout, world, ctx.rates)
            ctx.log(
                "colony_founded",
                f"Founded {colony.name} on {world.name} ({world.world_type}) "
                f"with {loadout.colonists:.0f} colonists. {assessment.summary()}",
                civ_id=civ.id,
                payload={"world_id": world.id, "colony_id": colony.id},
            )


def _fail(ctx: TickContext, intent, reason: str) -> None:
    intent.status = IntentStatus.FAILED.value
    intent.resolved_tick = ctx.tick
    intent.result = reason
    ctx.log("intent_failed", f"Colonization failed: {reason}", civ_id=intent.civ_id)
