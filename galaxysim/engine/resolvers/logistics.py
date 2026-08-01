"""Moving goods between colonies.

Stockpiles are local, so this is the only thing that connects them. A colony at
the end of a cut supply line is genuinely alone, which is what gives blockades
and remote outposts their weight.

Two orders, and the difference between them is the whole reason manual logistics
is survivable in an async game:

* :data:`IntentKind.TRANSFER_CARGO` is a one-off load or unload.
* :data:`IntentKind.SUPPLY_ROUTE` is **standing**. It shuttles between two
  colonies indefinitely, re-issuing its own legs, so a player sets it up once
  and it keeps feeding an outpost while they are asleep. Hand-shipping cargo
  every tick would be unplayable for someone checking in once a day.
* :data:`IntentKind.MIGRATE` moves *people* rather than goods, on the same
  legs and out of the same hold. Shipping a million settlers is exactly as
  plausible as shipping a million tonnes, which is what lets natural growth stay
  a believable rate while still giving a player a way to make a world matter
  sooner.

Loading is rate-limited by the colony's cargo throughput, which comes from
spaceports. A colony without one can still trickle goods (there is a small base
rate, so a fresh outpost is not permanently stranded), but bulk supply needs the
building.
"""

from __future__ import annotations

from galaxysim.materials import deposit
from galaxysim.core.space import distance
from galaxysim.core.units import format_count as format_people
from galaxysim.engine.context import TickContext
from galaxysim.engine.resolvers import queries
from galaxysim.engine.resolvers.production import colony_effects
from galaxysim.model.entities import Colony, Fleet, IntentKind, IntentStatus

#: How close a fleet must be to a colony's system to exchange cargo.
DOCKING_TOLERANCE_LY = 0.01

#: Tonnes per hour a colony can handle with no spaceport. Deliberately nonzero:
#: a new outpost has no buildings and must be able to receive the supplies it
#: needs in order to survive long enough to build any.
#:
#: Sized against what people actually drink. A colony burns a kilogram of water
#: per person per hour on a world that supplies none, so this bare rate keeps a
#: quarter of a million people breathing -- comfortably more than a landing
#: party, comfortably less than a real colony. Anything bigger needs the port.
BASE_THROUGHPUT_PER_HOUR = 250.0

#: How much faster a port moves people than it moves bulk. Passengers walk on
#: and off; ore has to be craned, and a port that shifts 250 tonnes of it an hour
#: can process a few thousand settlers in the same time. Without this an
#: evacuation runs at the speed of a gravel barge and migration stops being a
#: thing anyone would order.
PASSENGER_HANDLING_MULTIPLIER = 20.0


def passengers_per_hour(colony: Colony) -> float:
    """People this colony can embark or disembark per real hour."""
    from galaxysim.engine.rates import DEFAULT_RATES

    return (
        throughput_per_hour(colony)
        * PASSENGER_HANDLING_MULTIPLIER
        / max(DEFAULT_RATES.tonnes_per_passenger, 1e-9)
    )


def resolve(ctx: TickContext) -> None:
    """Run after movement, so a freighter that landed this tick works now."""
    _resolve_transfers(ctx)
    _resolve_routes(ctx)
    _resolve_migrations(ctx)


def throughput_per_hour(colony: Colony) -> float:
    """Tonnes per hour this colony can load or unload."""
    return BASE_THROUGHPUT_PER_HOUR + colony_effects(colony).cargo_throughput


# ------------------------------------------------------------- one-off moves


def _resolve_transfers(ctx: TickContext) -> None:
    for intent in queries.active_intents(
        ctx.session, ctx.universe.id, IntentKind.TRANSFER_CARGO.value
    ):
        fleet = ctx.session.get(Fleet, intent.payload.get("fleet_id", -1))
        if fleet is None or fleet.civ_id != intent.civ_id:
            _fail(ctx, intent, "no such fleet")
            continue

        colony = ctx.session.get(Colony, intent.payload.get("colony_id", -1))
        if colony is None or colony.civ_id != intent.civ_id:
            _fail(ctx, intent, "no such colony")
            continue

        if not _docked(fleet, colony):
            # Wait rather than fail: the fleet is probably still on its way
            # under a move order queued at the same time.
            intent.result = "awaiting fleet arrival"
            continue

        manifest = dict(intent.payload.get("manifest") or {})
        loading = bool(intent.payload.get("loading", True))
        allowance = ctx.per_tick(throughput_per_hour(colony))

        moved = (
            _load(fleet, colony, manifest, allowance)
            if loading
            else _unload(fleet, colony, manifest, allowance)
        )

        remaining = {
            resource: amount - moved.get(resource, 0.0)
            for resource, amount in manifest.items()
            if amount - moved.get(resource, 0.0) > 1e-9
        }

        if remaining:
            # Part-loaded. Keep the order alive so the rest follows next tick.
            intent.payload["manifest"] = remaining
            intent.status = IntentStatus.IN_PROGRESS.value
            intent.result = "loading" if loading else "unloading"
            continue

        intent.status = IntentStatus.COMPLETED.value
        intent.resolved_tick = ctx.tick
        verb = "Loaded" if loading else "Delivered"
        ctx.log(
            "cargo_transferred",
            f"{verb} {_describe(moved)} at {colony.name}",
            civ_id=intent.civ_id,
            payload={"fleet_id": fleet.id, "colony_id": colony.id},
        )


# --------------------------------------------------------- standing routes


def _resolve_routes(ctx: TickContext) -> None:
    """Run a standing shuttle between two colonies.

    The route is a small state machine kept in the intent payload: load at the
    origin, fly, unload at the destination, fly back. It never completes on its
    own -- that is the point, since an outpost's need does not stop.
    """
    for intent in queries.active_intents(
        ctx.session, ctx.universe.id, IntentKind.SUPPLY_ROUTE.value
    ):
        fleet = ctx.session.get(Fleet, intent.payload.get("fleet_id", -1))
        if fleet is None or fleet.civ_id != intent.civ_id:
            _fail(ctx, intent, "no such fleet")
            continue

        origin = ctx.session.get(Colony, intent.payload.get("origin_colony_id", -1))
        destination = ctx.session.get(Colony, intent.payload.get("dest_colony_id", -1))
        if origin is None or destination is None:
            _fail(ctx, intent, "route endpoint no longer exists")
            continue
        if origin.civ_id != intent.civ_id or destination.civ_id != intent.civ_id:
            _fail(ctx, intent, "a route must run between your own colonies")
            continue

        intent.status = IntentStatus.IN_PROGRESS.value
        manifest = dict(intent.payload.get("manifest") or {})
        leg = str(intent.payload.get("leg", "outbound"))

        if fleet.in_transit:
            intent.result = f"in transit ({leg})"
            continue

        if leg == "outbound":
            _run_outbound_leg(ctx, intent, fleet, origin, destination, manifest)
        else:
            _run_delivery_leg(ctx, intent, fleet, origin, destination)


def _run_outbound_leg(
    ctx: TickContext, intent, fleet: Fleet, origin: Colony, destination: Colony, manifest: dict
) -> None:
    """At the origin: load. Elsewhere: fly to the origin or on to the drop."""
    if _docked(fleet, origin):
        wanted = {
            resource: max(0.0, amount - fleet.cargo.get(resource, 0.0))
            for resource, amount in manifest.items()
        }
        outstanding = {r: a for r, a in wanted.items() if a > 1e-9}

        if outstanding and origin.stockpile:
            _load(fleet, origin, outstanding, ctx.per_tick(throughput_per_hour(origin)))

        short = {
            resource: amount - fleet.cargo.get(resource, 0.0)
            for resource, amount in manifest.items()
            if fleet.cargo.get(resource, 0.0) + 1e-9 < amount
        }
        # Keep loading while the origin still has what the manifest asks for.
        # Departing on a part load sounds generous to a hungry outpost, and at
        # small numbers it was -- one tick of throughput used to be most of a
        # manifest. In tonnes it is a rounding error, and a route that leaves
        # four percent full delivers less than the destination drinks in transit.
        # So: go when the hold is full, or when the origin cannot fill it.
        can_still_supply = any(
            origin.stockpile.get(resource, 0.0) > 1e-9 for resource in short
        )
        if short and can_still_supply:
            intent.result = f"loading at {origin.name} ({_describe(fleet.cargo)})"
            return
        if fleet.cargo_tonnage <= 0:
            intent.result = f"waiting for cargo at {origin.name}"
            return

        _send(ctx, fleet, destination)
        intent.payload["leg"] = "delivering"
        intent.result = f"carrying {_describe(fleet.cargo)} to {destination.name}"
        return

    if _docked(fleet, destination):
        # Arrived at the drop.
        if fleet.cargo_tonnage > 0:
            delivered = _unload(
                fleet, destination, dict(fleet.cargo), ctx.per_tick(throughput_per_hour(destination))
            )
            if fleet.cargo_tonnage > 1e-9:
                intent.result = f"unloading at {destination.name}"
                return
            ctx.log(
                "supply_delivered",
                f"{fleet.name} delivered {_describe(delivered)} to {destination.name}",
                civ_id=intent.civ_id,
                payload={"colony_id": destination.id},
            )
        _send(ctx, fleet, origin)
        intent.payload["leg"] = "outbound"
        intent.result = f"returning to {origin.name}"
        return

    _send(ctx, fleet, origin)
    intent.result = f"repositioning to {origin.name}"


def _run_delivery_leg(
    ctx: TickContext, intent, fleet: Fleet, origin: Colony, destination: Colony
) -> None:
    """Carrying a load to the destination, then heading home empty."""
    if not _docked(fleet, destination):
        _send(ctx, fleet, destination)
        intent.result = f"carrying {_describe(fleet.cargo)} to {destination.name}"
        return

    delivered = _unload(
        fleet, destination, dict(fleet.cargo), ctx.per_tick(throughput_per_hour(destination))
    )
    if fleet.cargo_tonnage > 1e-9:
        intent.result = f"unloading at {destination.name}"
        return

    if delivered:
        ctx.log(
            "supply_delivered",
            f"{fleet.name} delivered {_describe(delivered)} to {destination.name}",
            civ_id=intent.civ_id,
            payload={"colony_id": destination.id},
        )
    _send(ctx, fleet, origin)
    intent.payload["leg"] = "outbound"
    intent.result = f"returning to {origin.name}"


# ---------------------------------------------------------------- migration


def _resolve_migrations(ctx: TickContext) -> None:
    """Carry people from one colony to another.

    Deliberately the same shape as a supply route -- board, fly, disembark --
    because it *is* the same thing. Shipping a million people at lightspeed is
    exactly as plausible as shipping a million tonnes of ore, on the same ship,
    over the same route.

    That equivalence is what resolves the timescale problem the design ran into:
    natural growth can stay a believable frontier rate, and a player who wants a
    world to matter sooner ships people to it rather than waiting. Nobody is
    forced to; doing it is an advantage.
    """
    for intent in queries.active_intents(ctx.session, ctx.universe.id, IntentKind.MIGRATE.value):
        fleet = ctx.session.get(Fleet, intent.payload.get("fleet_id", -1))
        if fleet is None or fleet.civ_id != intent.civ_id:
            _fail(ctx, intent, "no such fleet")
            continue

        origin = ctx.session.get(Colony, intent.payload.get("origin_colony_id", -1))
        destination = ctx.session.get(Colony, intent.payload.get("dest_colony_id", -1))
        if origin is None or destination is None:
            _fail(ctx, intent, "one end of the crossing no longer exists")
            continue
        if origin.civ_id != intent.civ_id or destination.civ_id != intent.civ_id:
            _fail(ctx, intent, "people can only be moved between your own colonies")
            continue

        intent.status = IntentStatus.IN_PROGRESS.value
        if fleet.in_transit:
            intent.result = f"{format_people(fleet.passengers)} in transit"
            continue

        if str(intent.payload.get("leg")) == "boarding":
            _run_boarding_leg(ctx, intent, fleet, origin, destination)
        else:
            _run_landing_leg(ctx, intent, fleet, origin, destination)


def _run_boarding_leg(
    ctx: TickContext, intent, fleet: Fleet, origin: Colony, destination: Colony
) -> None:
    """At the origin: embark. Elsewhere: fly there first."""
    if not _docked(fleet, origin):
        _send(ctx, fleet, origin)
        intent.result = f"repositioning to {origin.name}"
        return

    wanted = float(intent.payload.get("people", 0.0)) - fleet.passengers
    per_passenger = ctx.rates.tonnes_per_passenger
    room = fleet.cargo_space / per_passenger if per_passenger > 0 else wanted
    # Boarding runs at the port's rate, so a colony without a spaceport
    # evacuates slowly. Moving a population is infrastructure work, not a
    # decision that executes instantly.
    handled = ctx.per_tick(passengers_per_hour(origin))

    boarding = max(0.0, min(wanted, room, handled, origin.population))
    if boarding > 0:
        origin.population -= boarding
        fleet.passengers += boarding

    if fleet.passengers + 1e-9 < float(intent.payload.get("people", 0.0)) and fleet.passengers <= 0:
        intent.result = f"boarding at {origin.name}"
        return
    if boarding > 0 and fleet.passengers + 1e-9 < float(intent.payload.get("people", 0.0)):
        intent.result = f"boarding at {origin.name} ({format_people(fleet.passengers)} aboard)"
        return

    _send(ctx, fleet, destination)
    intent.payload["leg"] = "landing"
    intent.result = f"carrying {format_people(fleet.passengers)} to {destination.name}"


def _run_landing_leg(
    ctx: TickContext, intent, fleet: Fleet, origin: Colony, destination: Colony
) -> None:
    """Carrying settlers, then setting them down."""
    if not _docked(fleet, destination):
        _send(ctx, fleet, destination)
        intent.result = f"carrying {format_people(fleet.passengers)} to {destination.name}"
        return

    landing = min(fleet.passengers, ctx.per_tick(passengers_per_hour(destination)))
    if landing > 0:
        fleet.passengers -= landing
        destination.population += landing

    if fleet.passengers > 1e-9:
        intent.result = f"disembarking at {destination.name}"
        return

    ctx.log(
        "migrants_landed",
        f"{fleet.name} settled {format_people(float(intent.payload.get('people', 0.0)))} "
        f"on {destination.name}",
        civ_id=intent.civ_id,
        payload={"colony_id": destination.id, "people": intent.payload.get("people")},
    )
    intent.status = IntentStatus.COMPLETED.value
    intent.resolved_tick = ctx.tick
    intent.result = f"settled at {destination.name}"


# ------------------------------------------------------------------ helpers


def _docked(fleet: Fleet, colony: Colony) -> bool:
    if fleet.in_transit:
        return False
    return distance(fleet.position, colony.world.system.position) <= DOCKING_TOLERANCE_LY


def _send(ctx: TickContext, fleet: Fleet, colony: Colony) -> None:
    """Put a fleet under way to a colony, if it is not already there.

    Sets the same transit fields the movement resolver reads, so a freighter
    travels by exactly the rules a warship does.
    """
    target = colony.world.system.position
    if _docked(fleet, colony):
        return

    hours = distance(fleet.position, target) / max(fleet.speed_ly_per_hour, 1e-9)
    ticks = ctx.cadence.ticks_for_hours(hours)
    if ticks == 0:
        fleet.x, fleet.y, fleet.z = target.as_tuple()
        return

    fleet.origin_x, fleet.origin_y, fleet.origin_z = fleet.x, fleet.y, fleet.z
    fleet.dest_x, fleet.dest_y, fleet.dest_z = target.as_tuple()
    fleet.departed_tick = ctx.tick
    fleet.arrival_tick = ctx.tick + ticks


def _load(fleet: Fleet, colony: Colony, manifest: dict, allowance: float) -> dict[str, float]:
    """Move goods colony -> fleet, limited by throughput, stock and hold space."""
    moved: dict[str, float] = {}
    for resource, wanted in sorted(manifest.items()):
        if allowance <= 1e-9:
            break
        available = colony.stockpile.get(resource, 0.0)
        amount = min(float(wanted), available, allowance, fleet.cargo_space)
        if amount <= 1e-9:
            continue
        colony.stockpile[resource] = available - amount
        fleet.cargo[resource] = fleet.cargo.get(resource, 0.0) + amount
        allowance -= amount
        moved[resource] = amount
    return moved


def _unload(fleet: Fleet, colony: Colony, manifest: dict, allowance: float) -> dict[str, float]:
    """Move goods fleet -> colony, limited by throughput and what is aboard."""
    moved: dict[str, float] = {}
    for resource, wanted in sorted(manifest.items()):
        if allowance <= 1e-9:
            break
        aboard = fleet.cargo.get(resource, 0.0)
        amount = min(float(wanted), aboard, allowance)
        if amount <= 1e-9:
            continue
        fleet.cargo[resource] = aboard - amount
        if fleet.cargo[resource] <= 1e-9:
            del fleet.cargo[resource]
        deposit(colony.stockpile, {resource: amount})
        allowance -= amount
        moved[resource] = amount
    return moved


def _describe(manifest: dict) -> str:
    if not manifest:
        return "nothing"
    return ", ".join(f"{amount:,.0f} {resource}" for resource, amount in sorted(manifest.items()))


def _fail(ctx: TickContext, intent, reason: str) -> None:
    intent.status = IntentStatus.FAILED.value
    intent.resolved_tick = ctx.tick
    intent.result = reason
    ctx.log("intent_failed", f"Logistics order failed: {reason}", civ_id=intent.civ_id)
