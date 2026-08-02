"""The tick: everyone's queued intents resolve together, or none of them do.

Resolution order is fixed and meaningful:

1. **Movement** -- fleets arrive before anything can act on where they are.
2. **Logistics** -- a freighter that landed this tick unloads now, so supplies
   reach a starving colony before life support is computed against them.
3. **Combat** -- a fleet that arrived this tick is immediately at risk, and one
   that left is out of reach.
4. **Siege** -- what is left in orbit after the shooting decides whose supply
   lines run, and whether a colony changes hands.
5. **Governor** -- governed colonies decide their labor and building orders
   before production reads them, so a decision applies on the tick it is made.
6. **Production** -- survivors produce; casualties do not.
7. **Research** -- spends what production just banked.
8. **Colonization** -- last, so a world contested this tick is resolved before
   anyone settles it.

Everything random descends from ``tick_seed(universe_seed, tick_number)``, and
every query is explicitly ordered, so re-running a tick against the same
starting state reproduces it exactly. That is not a debugging nicety: players
are asleep for most of the ticks that affect them, and they need to be able to
trust an outcome they did not watch.

The whole tick commits as one transaction. A half-applied tick would leave a
universe that can never be replayed.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from galaxysim.engine.context import TickContext
from galaxysim.engine.rates import DEFAULT_RATES, Cadence, Rates
from galaxysim.engine.resolvers import (
    colonization,
    combat,
    governor,
    logistics,
    movement,
    production,
    research,
    siege,
    terraform,
)
from galaxysim.model.base import new_session, transaction
from galaxysim.model.entities import Universe

#: Run in this order. Changing it changes the game.
PIPELINE = (
    ("movement", movement.resolve),
    ("logistics", logistics.resolve),
    ("combat", combat.resolve),
    # After combat, so a blockade is judged on who is still standing once the
    # shooting stops rather than on who turned up. Before logistics would be
    # wrong: a colony must get one tick's warning in the log before its supply
    # lines are cut.
    ("siege", siege.resolve),
    ("governor", governor.resolve),
    ("production", production.resolve),
    ("research", research.resolve),
    # After production, so a project draws on the industry produced this tick;
    # before colonization, so a world finished this tick is settled as the world
    # it has become rather than the one it was.
    ("terraform", terraform.resolve),
    ("colonization", colonization.resolve),
)


@dataclass(frozen=True, slots=True)
class TickResult:
    tick: int
    seed: int
    events: int


def resolve_tick(
    session: Session, universe: Universe, *, rates: Rates = DEFAULT_RATES
) -> TickResult:
    """Advance ``universe`` by one tick within the caller's transaction.

    The caller owns the commit, which is what lets the CLI run a batch of ticks
    in one transaction and lets tests roll back.
    """
    cadence = Cadence(universe.seconds_per_tick)
    ctx = TickContext.build(session, universe, cadence, rates)

    for _name, resolve in PIPELINE:
        resolve(ctx)
        # Flush between stages so the next resolver sees rows the previous one
        # created (a fleet finished in production must exist before
        # colonization looks for it) without ending the transaction.
        session.flush()

    session.add_all(ctx.events)
    universe.tick_number = ctx.tick
    session.flush()

    return TickResult(tick=ctx.tick, seed=ctx.seed, events=len(ctx.events))


def run_ticks(
    engine: Engine, universe_id: int, count: int = 1, *, rates: Rates = DEFAULT_RATES
) -> list[TickResult]:
    """Resolve ``count`` ticks, each in its own transaction.

    One transaction per tick rather than one for the batch: a tick is the unit
    of atomicity, and a batch that failed on its last tick should not roll back
    the ones that already succeeded.

    **One session for the batch, though.** Those are different questions and
    conflating them was the single most expensive thing in the engine. A session
    per tick means a cold identity map every hour, so every colony, world,
    system and fleet is rebuilt as a fresh Python object and every JSON column
    is decoded again -- at two hundred colonies that measured 9,670 ORM
    instances and 10,766 JSON decodes *per tick*, none of which is simulation.
    Keeping the session across the batch keeps what it already loaded, and
    ``expire_on_commit=False`` keeps it after each tick commits.
    """
    results: list[TickResult] = []
    session = new_session(engine)
    try:
        universe = session.get(Universe, universe_id)
        if universe is None:
            raise LookupError(f"no universe with id {universe_id}")
        for _ in range(count):
            with transaction(session):
                results.append(resolve_tick(session, universe, rates=rates))
    finally:
        session.close()
    return results
