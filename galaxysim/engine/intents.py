"""Submitting orders.

This is the entire write surface of the game. Players, the CLI, the future HTTP
layer and the AI all go through these functions -- the AI has no privileged path
into the simulation, which is what makes a solo game against AI opponents an
honest test of the multiplayer one.

Nothing here changes world state. An order is a row in a queue; it takes effect
when the tick resolves it, alongside everyone else's.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from galaxysim.colony.expedition import Loadout
from galaxysim.colony.labor import normalize
from galaxysim.model.entities import Civ, Colony, Intent, IntentKind, IntentStatus, Universe


def _queue(session: Session, civ: Civ, kind: IntentKind, payload: dict) -> Intent:
    universe = session.get(Universe, civ.universe_id)
    if universe is None:
        raise LookupError(f"civ {civ.id} has no universe")

    intent = Intent(
        universe_id=civ.universe_id,
        civ_id=civ.id,
        kind=kind.value,
        payload=payload,
        status=IntentStatus.QUEUED.value,
        queued_tick=universe.tick_number,
    )
    session.add(intent)
    session.flush()
    return intent


def move_fleet(
    session: Session,
    civ: Civ,
    fleet_id: int,
    x: float,
    y: float,
    z: float,
    *,
    reason: str | None = None,
) -> Intent:
    """Order a fleet to a point in space.

    The target is a coordinate, not a system. Systems do not gate movement --
    a fleet can be sent anywhere, and what it costs is distance.

    ``reason`` is a short tag for *why* the order was issued -- "scout", "raid",
    "withdraw". Nothing in the engine reads it; it exists because the log
    recorded that a fleet was sent somewhere and never what for, so neither a
    player reading their own history nor anyone auditing the AI could tell a
    survey sweep from a war. Optional, because a human clicking a destination
    owes nobody an explanation.
    """
    payload: dict = {"fleet_id": fleet_id, "x": float(x), "y": float(y), "z": float(z)}
    if reason:
        payload["reason"] = reason
    return _queue(session, civ, IntentKind.MOVE_FLEET, payload)


def move_fleet_to_system(
    session: Session, civ: Civ, fleet_id: int, system, *, reason: str | None = None
) -> Intent:
    """Convenience wrapper: send a fleet to a system's coordinates."""
    return move_fleet(session, civ, fleet_id, system.x, system.y, system.z, reason=reason)


def colonize(
    session: Session,
    civ: Civ,
    fleet_id: int,
    world_id: int,
    *,
    loadout: Loadout | None = None,
    name: str | None = None,
) -> Intent:
    """Settle a world using a colony pod from ``fleet_id``.

    The ``loadout`` is what the expedition carries, and it decides both the
    price and what the colony wakes up with. It is charged to the colony nearest
    the fleet when settling begins, not when the order is queued.

    Safe to queue alongside the move order that gets the fleet there: the order
    waits for arrival rather than failing.
    """
    payload: dict = {"fleet_id": fleet_id, "world_id": world_id}
    payload.update((loadout or Loadout()).as_payload())
    if name:
        payload["name"] = name
    return _queue(session, civ, IntentKind.COLONIZE, payload)


def build_fleet(
    session: Session,
    civ: Civ,
    colony_id: int,
    strength: float,
    *,
    colony_pods: int = 0,
    cargo_capacity: float | None = None,
    name: str | None = None,
) -> Intent:
    """Build a fleet at a colony.

    Requires a shipyard there. Resources are charged when work begins, and the
    rest is paid in industry-work, so the colony's industry sector decides how
    fast it finishes.

    Pass ``cargo_capacity`` to build a freighter -- a lot of hold for little
    strength -- rather than the default warship ratio.
    """
    payload: dict = {
        "colony_id": colony_id,
        "strength": float(strength),
        "colony_pods": int(colony_pods),
    }
    if cargo_capacity is not None:
        payload["cargo_capacity"] = float(cargo_capacity)
    if name:
        payload["name"] = name
    return _queue(session, civ, IntentKind.BUILD_FLEET, payload)


def decommission_fleet(session: Session, civ: Civ, fleet_id: int) -> Intent:
    """Break up a fleet, returning part of its materials to the colony it is at.

    The counterpart to :func:`build_fleet`, and until it existed upkeep was a
    one-way ratchet: a colony ship that had landed its pod was a hull with no
    remaining purpose and a permanent hourly bill, and the only way to stop
    paying was to let its crew desert. A fleet is a standing commitment, so
    ending it has to be something you can *decide*.

    The fleet must be parked at one of your own colonies -- scrapping is
    industrial work, not something a crew does in deep space -- and what comes
    back lands in that colony's warehouses, like everything else.
    """
    return _queue(session, civ, IntentKind.DECOMMISSION, {"fleet_id": int(fleet_id)})


def build_structure(session: Session, civ: Civ, colony_id: int, kind: str) -> Intent:
    """Construct a building at a colony.

    Resources are charged when the foundations go in; the rest is paid for in
    industry-work, so a colony with nobody assigned to industry will sit on a
    half-finished structure indefinitely.
    """
    return _queue(
        session, civ, IntentKind.BUILD_STRUCTURE, {"colony_id": colony_id, "kind": str(kind)}
    )


def set_labor(session: Session, colony: Colony, allocation: dict[str, float]) -> None:
    """Reassign a colony's population across labor sectors.

    Applies immediately rather than queueing. Moving your own people between
    jobs is not an action against the world -- it carries no cost, competes with
    nobody, and gives no advantage for being awake when you do it, so there is
    nothing for the tick to arbitrate. The values are normalized, so they are
    read as relative weights.
    """
    colony.labor = normalize(allocation)
    # Setting labor by hand is an implicit statement that you want to run this
    # colony: leaving it governed would have the governor overwrite the
    # assignment on the very next tick.
    colony.management_mode = "manual"


def set_management(
    session: Session, colony: Colony, *, governed: bool, policy: str | None = None
) -> None:
    """Hand a colony to a governor, or take it back.

    Applies immediately, like :func:`set_labor` -- deciding who runs your own
    colony is not an action the tick needs to arbitrate.
    """
    colony.management_mode = "governor" if governed else "manual"
    if policy is not None:
        colony.governor_policy = policy


def transfer_cargo(
    session: Session,
    civ: Civ,
    fleet_id: int,
    colony_id: int,
    manifest: dict[str, float],
    *,
    loading: bool = True,
) -> Intent:
    """Load from or unload to a colony, once.

    Safe to queue before the fleet arrives -- like colonization, it waits.
    Transfers are rate-limited by the colony's cargo throughput, so a large
    manifest takes several ticks and stays in progress until it is done.
    """
    return _queue(
        session,
        civ,
        IntentKind.TRANSFER_CARGO,
        {
            "fleet_id": fleet_id,
            "colony_id": colony_id,
            "manifest": {str(k): float(v) for k, v in manifest.items()},
            "loading": bool(loading),
        },
    )


def supply_route(
    session: Session,
    civ: Civ,
    fleet_id: int,
    origin_colony_id: int,
    dest_colony_id: int,
    manifest: dict[str, float],
) -> Intent:
    """Set up a standing shuttle between two of your colonies.

    Runs forever: load at the origin, fly, unload at the destination, return,
    repeat. This is what makes local stockpiles playable asynchronously -- an
    outpost's need does not pause overnight, so neither does the route.
    """
    if origin_colony_id == dest_colony_id:
        raise ValueError("a supply route needs two different colonies")
    return _queue(
        session,
        civ,
        IntentKind.SUPPLY_ROUTE,
        {
            "fleet_id": fleet_id,
            "origin_colony_id": origin_colony_id,
            "dest_colony_id": dest_colony_id,
            "manifest": {str(k): float(v) for k, v in manifest.items()},
            "leg": "outbound",
        },
    )


def migrate(
    session: Session,
    civ: Civ,
    fleet_id: int,
    origin_colony_id: int,
    dest_colony_id: int,
    people: float,
) -> Intent:
    """Move a population from one of your colonies to another.

    The strategic act the design is built around. Natural growth already carries
    a habitable colony to maturity on its own, so nobody is *forced* to run
    convoys -- but a garden world with room and no people, and a capital with
    people and no room, is a situation only shipping fixes. Doing it is a real
    advantage, which is the point.

    One trip, not a standing route: moving people is a decision about where a
    civilization's weight should sit, and that is not a thing you set up once and
    forget. It also keeps the freighter honest -- people occupy the same hold as
    ore, so a migration run is a cargo run you did not make.
    """
    if origin_colony_id == dest_colony_id:
        raise ValueError("migration needs two different colonies")
    if people <= 0:
        raise ValueError("migration needs people")
    return _queue(
        session,
        civ,
        IntentKind.MIGRATE,
        {
            "fleet_id": fleet_id,
            "origin_colony_id": origin_colony_id,
            "dest_colony_id": dest_colony_id,
            "people": float(people),
            "leg": "boarding",
        },
    )


def terraform(session: Session, civ: Civ, colony_id: int, project: str) -> Intent:
    """Begin reshaping the planet a colony sits on.

    The only order that changes a *world* rather than what is on it, and the
    only sink big enough to absorb a mature civilization's surplus. What it buys
    is the outpost-to-world transition: a dead rock caps at what its habitats
    hold, and lifting habitability past the liveable line makes the ceiling the
    real ``land x density x habitability`` figure instead -- four orders of
    magnitude, from a sequence of projects.

    See :mod:`galaxysim.terraform.projects`.
    """
    from galaxysim.terraform.projects import project as lookup

    lookup(project)  # raises helpfully on a typo, before anything is queued
    return _queue(
        session,
        civ,
        IntentKind.TERRAFORM,
        {"colony_id": int(colony_id), "project": str(project)},
    )


def attack(session: Session, civ: Civ, target_civ_id: int) -> Intent:
    """Declare standing hostility toward another civilization.

    Standing, not per-engagement: it persists until cancelled, so a war does not
    lapse because nobody logged in. Fleets of hostile civs fight wherever they
    meet; without this order they pass each other peacefully.
    """
    if target_civ_id == civ.id:
        raise ValueError("a civilization cannot declare war on itself")
    return _queue(session, civ, IntentKind.ATTACK, {"target_civ_id": int(target_civ_id)})


def research(session: Session, civ: Civ, *, prefer: str | None = None) -> Intent:
    """Begin a standing research programme.

    Runs indefinitely, buying the next step whenever the civ can afford it, so
    an offline player keeps advancing.

    ``prefer`` names a stat from :data:`galaxysim.tech.STATS` and is a standing
    *bias*, not a pick. The frontier regenerates at every depth, so choosing a
    particular candidate would mean stopping to ask on every purchase -- which
    is precisely what a standing order exists to avoid. Naming what the
    civilization is trying to become instead survives regeneration, and matches
    the shape :attr:`Colony.refining` already uses for chains.
    """
    return _queue(
        session, civ, IntentKind.RESEARCH, {"prefer": prefer} if prefer else {}
    )


def cancel(session: Session, intent: Intent) -> None:
    """Cancel a queued or standing order.

    Resources already spent are not refunded -- a build that has started has
    consumed its materials.
    """
    if intent.status in (IntentStatus.COMPLETED.value, IntentStatus.FAILED.value):
        return
    intent.status = IntentStatus.CANCELLED.value


def pending(session: Session, civ: Civ) -> list[Intent]:
    """A civ's unresolved orders, oldest first."""
    return list(
        session.scalars(
            select(Intent)
            .where(
                Intent.civ_id == civ.id,
                Intent.status.in_((IntentStatus.QUEUED.value, IntentStatus.IN_PROGRESS.value)),
            )
            .order_by(Intent.id)
        )
    )
