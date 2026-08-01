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


def move_fleet(session: Session, civ: Civ, fleet_id: int, x: float, y: float, z: float) -> Intent:
    """Order a fleet to a point in space.

    The target is a coordinate, not a system. Systems do not gate movement --
    a fleet can be sent anywhere, and what it costs is distance.
    """
    return _queue(
        session,
        civ,
        IntentKind.MOVE_FLEET,
        {"fleet_id": fleet_id, "x": float(x), "y": float(y), "z": float(z)},
    )


def move_fleet_to_system(session: Session, civ: Civ, fleet_id: int, system) -> Intent:
    """Convenience wrapper: send a fleet to a system's coordinates."""
    return move_fleet(session, civ, fleet_id, system.x, system.y, system.z)


def colonize(
    session: Session, civ: Civ, fleet_id: int, world_id: int, *, name: str | None = None
) -> Intent:
    """Settle a world using a colony pod from ``fleet_id``.

    Safe to queue alongside the move order that gets the fleet there: the order
    waits for arrival rather than failing.
    """
    payload: dict = {"fleet_id": fleet_id, "world_id": world_id}
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
    name: str | None = None,
) -> Intent:
    """Build a fleet at a colony. Resources are charged when work begins."""
    payload: dict = {
        "colony_id": colony_id,
        "strength": float(strength),
        "colony_pods": int(colony_pods),
    }
    if name:
        payload["name"] = name
    return _queue(session, civ, IntentKind.BUILD_FLEET, payload)


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


def attack(session: Session, civ: Civ, target_civ_id: int) -> Intent:
    """Declare standing hostility toward another civilization.

    Standing, not per-engagement: it persists until cancelled, so a war does not
    lapse because nobody logged in. Fleets of hostile civs fight wherever they
    meet; without this order they pass each other peacefully.
    """
    if target_civ_id == civ.id:
        raise ValueError("a civilization cannot declare war on itself")
    return _queue(session, civ, IntentKind.ATTACK, {"target_civ_id": int(target_civ_id)})


def research(session: Session, civ: Civ) -> Intent:
    """Begin a standing research programme.

    Runs indefinitely, buying the next step whenever the civ can afford it, so
    an offline player keeps advancing.
    """
    return _queue(session, civ, IntentKind.RESEARCH, {})


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
