"""Colonization.

A colonize order waits. If the fleet has not reached the target world yet the
order stays queued rather than failing, so a player can queue "move there, then
settle it" in one sitting and log off -- which is the whole point of an async
game.

**An expedition is loaded before it leaves.** That is the physical reading and
it is also the only one that works: outfitting on arrival means shopping for a
year of water and fertiliser at whatever colony happens to be nearest the
*destination*, which on a frontier run is the outpost you founded last week and
which has nothing. An order in that position never fails and never completes --
it sits forever reporting that the warehouse at the edge of your territory is
empty, because it always will be.

So the cost is charged at the origin, while the ship is still somewhere with a
warehouse, and what it carries is what it paid for. If the expedition is called
off after loading, the cargo goes back where it came from.

Settling then takes wall-clock hours (:attr:`Rates.colonization_hours`), timed
the same way construction is.
"""

from __future__ import annotations

from galaxysim.colony.expedition import Loadout, assess
from galaxysim.colony.labor import balanced_allocation
from galaxysim.materials import gather
from galaxysim.core.space import distance
from galaxysim.engine.context import TickContext
from galaxysim.engine.resolvers import queries
from galaxysim.model.entities import Civ, Colony, Fleet, IntentKind, IntentStatus

#: How close a fleet must be to a system to settle a world in it, in light-years.
#: Not zero: positions are floats and a fleet parked "at" a system is only ever
#: approximately there.
ARRIVAL_TOLERANCE_LY = 0.01

#: How long an order will wait for its origin to be able to afford the loadout
#: before giving up, in hours. Waiting is right -- a capital mid-way through a
#: shipyard run will have the materials next week -- but waiting *forever* is
#: how one unaffordable order silently ends a civilization's expansion, so the
#: patience is finite and the fleet is released when it runs out.
OUTFITTING_PATIENCE_HOURS = 24.0 * 14.0


def resolve(ctx: TickContext) -> None:
    for intent in queries.pending(ctx, IntentKind.COLONIZE.value):
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

        loadout = Loadout.from_payload(intent.payload)

        # Note what is *not* checked here: habitability. A world with none can
        # be settled, on wholly artificial life support, and lives or dies by
        # its supply line. Since barren worlds and gas giants carry the richest
        # yields in the table, that is where the interesting decisions are.

        fleet = queries.fleets_by_id(ctx).get(intent.payload.get("fleet_id", -1))
        if fleet is None or fleet.civ_id != civ.id:
            _fail(ctx, intent, "no such fleet")
            continue

        if fleet.colony_pods <= 0:
            _fail(ctx, intent, f"{fleet.name} carries no colony pods")
            continue

        # Load first, wherever the ship currently is. On a normal order that is
        # the colony it was built at, before it has gone anywhere.
        if not intent.payload.get("outfitted_colony_id"):
            if not _outfit(ctx, intent, civ, fleet, loadout):
                continue
            # Outfitting may have cut the manifest down to what the warehouse
            # could actually cover, so read it back rather than trusting the
            # local. Everything below -- the assessment the player is shown, the
            # infrastructure the colony lands with -- has to describe the
            # expedition that sailed and not the one that was ordered.
            loadout = Loadout.from_payload(intent.payload)

        if fleet.in_transit or distance(fleet.position, world.system.position) > ARRIVAL_TOLERANCE_LY:
            # Not there yet. Wait rather than fail -- the fleet is probably on
            # its way under a move order queued at the same time.
            intent.result = "expedition loaded, awaiting fleet arrival"
            continue

        if intent.status == IntentStatus.QUEUED.value:
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
                # The relationship, not the raw foreign key. Assigning
                # ``world_id`` leaves ``World.colony`` unset in memory, so
                # anything asking "is this world taken?" through the ORM gets
                # the answer from before the landing -- which is how two
                # expeditions came to settle the same rock the moment the tick
                # loop stopped re-reading every row every hour.
                world=world,
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


def _outfit(ctx: TickContext, intent, civ: Civ, fleet: Fleet, loadout: Loadout) -> bool:
    """Load the expedition out of the nearest colony's warehouse.

    Returns whether the ship is now loaded. A ``False`` leaves the order queued:
    either it is still waiting for the materials to exist, or its patience has
    run out and it has failed outright.

    What it carries is what it costs. There is no flat fee and no hostility
    surcharge -- a hard world is expensive because surviving it takes more
    cargo, which is a fact about the manifest rather than a price list.
    """
    outfitter = queries.nearest_colony(ctx.session, civ.id, fleet.position)
    if outfitter is None:
        _fail(ctx, intent, "no colony available to outfit the expedition")
        return False

    # Loaded over time, not in one instant.
    #
    # A quartermaster takes delivery of what the warehouse can spare each hour
    # and holds it against the manifest. Demanding the whole expedition at once
    # is what stopped eight AI civilizations dead at thirty-odd colonies for two
    # simulated months: each had a colony ship with a pod aboard and a world
    # picked out, and each was refused because its capital's governor had spent
    # the steel on a mine an hour earlier. See :func:`galaxysim.materials.gather`
    # -- an all-or-nothing bill cannot be saved for at any income.
    cost = loadout.cost()
    stock = dict(outfitter.stockpile)
    banked, short = gather(stock, cost, intent.payload.get("loaded") or {})
    outfitter.stockpile = stock

    if short:
        intent.payload["loaded"] = banked
        # Patience measures being *stuck*, not being slow. An expedition that
        # took on stores this hour is making progress and should not be
        # abandoned for having taken a fortnight to fill its holds.
        if banked != (intent.payload.get("loaded_last") or {}):
            intent.payload["loaded_last"] = dict(banked)
            intent.payload["stalled_since"] = ctx.tick
        stalled = int(intent.payload.get("stalled_since", intent.queued_tick))
        waited = (ctx.tick - stalled) * ctx.cadence.hours_per_tick
        if waited >= OUTFITTING_PATIENCE_HOURS:
            return _sail_short(ctx, intent, civ, fleet, loadout, outfitter, banked)
        intent.result = (
            f"loading at {outfitter.name} ("
            + ", ".join(
                f"{amount:,.0f} {resource} short"
                for resource, amount in sorted(short.items())
            )
            + ")"
        )
        return False

    intent.payload.pop("loaded", None)
    intent.payload.pop("loaded_last", None)
    intent.payload["outfitted_colony_id"] = outfitter.id
    intent.result = "expedition loaded"
    ctx.log(
        "expedition_outfitted",
        f"{outfitter.name} loaded {fleet.name} for the settlement of "
        f"{loadout.colonists:,.0f} colonists",
        civ_id=civ.id,
        payload={"colony_id": outfitter.id, "fleet_id": fleet.id},
    )
    return True


def _sail_short(
    ctx: TickContext,
    intent,
    civ: Civ,
    fleet: Fleet,
    loadout: Loadout,
    outfitter: Colony,
    banked: dict[str, float],
) -> bool:
    """Send the expedition with what the warehouse could actually cover.

    The patience above used to end in :func:`_fail`, and for most of a
    civilization's life that is harmless -- a capital mid-way through a shipyard
    run has the materials next week and the order is simply re-issued. It is not
    harmless when the shortage is *permanent*. A homeworld with no copper and no
    rare earths makes no electronics ever, a unit of colony equipment wants three
    thousand tonnes of it, and the order therefore fails on a fortnight's timer
    for as long as the game runs. Measured: one opponent in eight, on a
    habitability-1.0 world with eleven billion people, stuck at two colonies for
    a hundred and twenty days with two settlers holding pods they would never
    land.

    So the fortnight now buys a *smaller* expedition rather than no expedition.
    See :meth:`Loadout.largest_within` for what gets cut and why. The colony that
    results is a real one and a poor one -- same people, same world, a third of
    the infrastructure -- which is the honest cost of a crust that cannot supply
    what settling takes, and leaves geology a reason to go somewhere better
    rather than a reason to stop playing.
    """
    reduced = loadout.largest_within(banked)
    if reduced is None:
        _fail(
            ctx,
            intent,
            f"{outfitter.name} could not supply even the colonists in "
            f"{OUTFITTING_PATIENCE_HOURS / 24:.0f} days; order abandoned",
        )
        return False

    # Whatever the smaller manifest does not need goes back on the shelf. The
    # escrow was never burned -- it is stores on a dock.
    returned = dict(outfitter.stockpile)
    needed = reduced.cost()
    for material, amount in sorted(banked.items()):
        spare = amount - needed.get(material, 0.0)
        if spare > 1e-9:
            returned[material] = returned.get(material, 0.0) + spare
    outfitter.stockpile = returned

    intent.payload.update(reduced.as_payload())
    intent.payload.pop("loaded", None)
    intent.payload.pop("loaded_last", None)
    intent.payload.pop("stalled_since", None)
    intent.payload["outfitted_colony_id"] = outfitter.id
    intent.payload["sailed_short"] = True

    shortfall = (
        f"{reduced.equipment:,.1f} of {loadout.equipment:,.1f} equipment"
        if reduced.equipment < loadout.equipment
        else f"{reduced.stores:,.0f} of {loadout.stores:,.0f} tonnes of stores"
    )
    intent.result = f"sailed short from {outfitter.name} ({shortfall})"
    ctx.log(
        "expedition_sailed_short",
        f"{outfitter.name} could not fill {fleet.name}'s manifest in "
        f"{OUTFITTING_PATIENCE_HOURS / 24:.0f} days and sent it anyway with "
        f"{shortfall}. The colony will land able to do less for itself.",
        civ_id=civ.id,
        payload={
            "colony_id": outfitter.id,
            "fleet_id": fleet.id,
            "equipment": reduced.equipment,
            "stores": reduced.stores,
        },
    )
    return True


def _fail(ctx: TickContext, intent, reason: str) -> None:
    """Abandon an order, returning anything already loaded to where it came from.

    An expedition that is called off is cargo sitting in a hold, not cargo that
    was burned -- so it goes back on the shelf. Without this, a world settled by
    a rival while your convoy was in transit would silently destroy a year of a
    colony's production.
    """
    colony_id = intent.payload.get("outfitted_colony_id") if intent.payload else None
    if colony_id:
        origin = queries.colonies_by_id(ctx).get(colony_id)
        if origin is not None:
            returned = dict(origin.stockpile)
            for material, amount in Loadout.from_payload(intent.payload).cost().items():
                returned[material] = returned.get(material, 0.0) + amount
            origin.stockpile = returned
            reason = f"{reason}; the expedition was returned to {origin.name}"
        intent.payload["outfitted_colony_id"] = None
    elif intent.payload and intent.payload.get("loaded"):
        # Abandoned part-way through loading. What is already in the escrow was
        # never burned either -- it is stores on a dock -- so it goes back to the
        # colony that supplied it, and to the same place it would have gone had
        # the manifest completed.
        ship = queries.fleets_by_id(ctx).get(intent.payload.get("fleet_id", -1))
        origin = (
            queries.nearest_colony(ctx.session, intent.civ_id, ship.position)
            if ship is not None
            else None
        )
        if origin is not None:
            returned = dict(origin.stockpile)
            for material, amount in dict(intent.payload["loaded"]).items():
                returned[material] = returned.get(material, 0.0) + amount
            origin.stockpile = returned
            reason = f"{reason}; part-loaded stores returned to {origin.name}"
        intent.payload.pop("loaded", None)
        intent.payload.pop("loaded_last", None)

    intent.status = IntentStatus.FAILED.value
    intent.resolved_tick = ctx.tick
    intent.result = reason
    ctx.log("intent_failed", f"Colonization failed: {reason}", civ_id=intent.civ_id)
