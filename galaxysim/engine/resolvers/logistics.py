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

Loading is rate-limited by the colony's cargo throughput, which comes from
spaceports. A colony without one can still trickle goods (there is a small base
rate, so a fresh outpost is not permanently stranded), but bulk supply needs the
building.
"""

from __future__ import annotations

from galaxysim.materials import deposit
from galaxysim.core.space import distance
from galaxysim.engine.context import TickContext
from galaxysim.engine.resolvers import queries
from galaxysim.engine.resolvers.production import colony_effects
from galaxysim.model.entities import Colony, Fleet, IntentKind, IntentStatus

#: How close a fleet must be to a colony's system to exchange cargo.
DOCKING_TOLERANCE_LY = 0.01

#: Tonnes per hour a colony can handle with no spaceport. Deliberately nonzero:
#: a new outpost has no buildings and must be able to receive the supplies it
#: needs in order to survive long enough to build any.
BASE_THROUGHPUT_PER_HOUR = 4.0


def resolve(ctx: TickContext) -> None:
    """Run after movement, so a freighter that landed this tick works now."""
    _resolve_transfers(ctx)
    _resolve_routes(ctx)


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

        still_short = any(
            fleet.cargo.get(resource, 0.0) + 1e-9 < amount
            for resource, amount in manifest.items()
        )
        # Depart with a part load rather than waiting for a full one. A hungry
        # outpost would rather have some air now than all of it eventually.
        if still_short and fleet.cargo_tonnage <= 0:
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
