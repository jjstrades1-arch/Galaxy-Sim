"""Fleet movement through continuous space.

A fleet does not step across a grid. It is given an origin, a destination and an
arrival tick, and its position is *interpolated* from those three each tick.
That choice matters for the pacing rule in :mod:`galaxysim.engine.rates`: an
integrated position would accumulate per-tick error and a fleet's real speed
would depend on the cadence. Interpolation keeps a journey exactly as long in
wall-clock time whether the universe ticks every five minutes or every hour.
"""

from __future__ import annotations

from galaxysim.core.space import Vec3, travel_time_hours
from galaxysim.engine.context import TickContext
from galaxysim.engine.resolvers import queries
from galaxysim.worldgen.galaxy import systems_near
from galaxysim.worldgen.materialize import ARRIVAL_TOLERANCE_LY, materialize
from galaxysim.model.entities import Fleet, IntentKind, IntentStatus


def resolve(ctx: TickContext) -> None:
    """Launch newly ordered journeys, then advance everything in transit."""
    _launch_ordered_moves(ctx)
    _advance_in_transit(ctx)


def _launch_ordered_moves(ctx: TickContext) -> None:
    for intent in queries.active_intents(ctx.session, ctx.universe.id, IntentKind.MOVE_FLEET.value):
        fleet = ctx.session.get(Fleet, intent.payload.get("fleet_id", -1))

        if fleet is None or fleet.civ_id != intent.civ_id:
            _fail(intent, ctx, "no such fleet")
            continue

        destination = Vec3(
            float(intent.payload.get("x", fleet.x)),
            float(intent.payload.get("y", fleet.y)),
            float(intent.payload.get("z", fleet.z)),
        ).quantized()

        hours = travel_time_hours(fleet.position, destination, fleet.speed_ly_per_hour)
        ticks = ctx.cadence.ticks_for_hours(hours)

        if ticks == 0:
            # Already there, or close enough that the journey rounds to nothing.
            _arrive(ctx, fleet, destination)
            intent.status = IntentStatus.COMPLETED.value
            intent.resolved_tick = ctx.tick
            continue

        fleet.origin_x, fleet.origin_y, fleet.origin_z = fleet.x, fleet.y, fleet.z
        fleet.dest_x, fleet.dest_y, fleet.dest_z = destination.as_tuple()
        fleet.departed_tick = ctx.tick
        fleet.arrival_tick = ctx.tick + ticks

        intent.status = IntentStatus.COMPLETED.value
        intent.resolved_tick = ctx.tick
        ctx.log(
            "fleet_departed",
            f"{fleet.name} set course for "
            f"({destination.x:.2f}, {destination.y:.2f}, {destination.z:.2f}); "
            f"arrives tick {fleet.arrival_tick}",
            civ_id=fleet.civ_id,
            payload={"fleet_id": fleet.id, "arrival_tick": fleet.arrival_tick},
        )


def _advance_in_transit(ctx: TickContext) -> None:
    for fleet in queries.fleets(ctx.session, ctx.universe.id):
        if not fleet.in_transit:
            continue

        assert fleet.arrival_tick is not None and fleet.departed_tick is not None
        destination = Vec3(float(fleet.dest_x), float(fleet.dest_y), float(fleet.dest_z))

        if ctx.tick >= fleet.arrival_tick:
            _arrive(ctx, fleet, destination)
            continue

        origin = Vec3(float(fleet.origin_x), float(fleet.origin_y), float(fleet.origin_z))
        span = fleet.arrival_tick - fleet.departed_tick
        progress = (ctx.tick - fleet.departed_tick) / span
        position = (origin + (destination - origin) * progress).quantized()
        fleet.x, fleet.y, fleet.z = position.as_tuple()


def _arrive(ctx: TickContext, fleet: Fleet, destination: Vec3) -> None:
    """Place a fleet at its destination and clear its transit state.

    **Arrival is what makes a system real.** Until somebody gets there a system
    is a pure function of the universe seed and a position -- computable by
    anyone, stored by nobody. Reaching it is the moment it acquires state that
    generation cannot derive, so that is the moment it becomes a row.

    A course can also end in empty space, which is legal and common: the galaxy
    is mostly nothing.
    """
    already_there = (fleet.x, fleet.y, fleet.z) == destination.as_tuple()

    fleet.x, fleet.y, fleet.z = destination.as_tuple()
    fleet.origin_x = fleet.origin_y = fleet.origin_z = None
    fleet.dest_x = fleet.dest_y = fleet.dest_z = None
    fleet.departed_tick = None
    fleet.arrival_tick = None

    # Asked *before* materializing, because "was this system already a row" is
    # the only reliable way to know whether this fleet is the first here. The
    # version that compared ``discovered_tick`` to ``ctx.tick`` never once fired:
    # a system is stamped with ``universe.tick_number``, and the tick being
    # resolved is that plus one, so every discovery in the game went unlogged.
    #
    # Answered against the tick's charted-key set rather than with a query, so a
    # hundred freighters docking costs one lookup between them rather than a
    # hundred.
    arriving_at = systems_near(ctx.universe.seed, destination, ARRIVAL_TOLERANCE_LY, limit=1)
    if not arriving_at:
        system, unvisited = None, False  # empty space, which is most of it
    else:
        known = queries.systems_by_key(ctx)
        system = known.get(arriving_at[0].key)
        unvisited = system is None
        if unvisited:
            # Only a genuinely new place costs a write. Everywhere a freighter
            # docks twice a day is already in the tick's index.
            system = materialize(ctx.session, ctx.universe, arriving_at[0])
            known[arriving_at[0].key] = system

    if system is not None and unvisited:
        ctx.log(
            "system_discovered",
            f"{fleet.name} is the first to reach {system.name} "
            f"({system.star_class}); {len(system.worlds)} worlds surveyed",
            civ_id=fleet.civ_id,
            payload={"system_id": system.id, "fleet_id": fleet.id},
        )

    if not already_there:
        ctx.log(
            "fleet_arrived",
            f"{fleet.name} arrived at "
            f"({destination.x:.2f}, {destination.y:.2f}, {destination.z:.2f})",
            civ_id=fleet.civ_id,
            payload={"fleet_id": fleet.id},
        )


def _fail(intent, ctx: TickContext, reason: str) -> None:
    intent.status = IntentStatus.FAILED.value
    intent.resolved_tick = ctx.tick
    intent.result = reason
    ctx.log("intent_failed", f"Move order failed: {reason}", civ_id=intent.civ_id)
