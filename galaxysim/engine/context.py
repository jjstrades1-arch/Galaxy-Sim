"""Per-tick context passed to every resolver.

Resolvers never touch the global RNG and never invent their own seeds. They ask
the context for a stream, scoped to what they are rolling for, so that adding an
entity or reordering a resolver cannot shift another resolver's draws.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from galaxysim.core.seeds import rng_for, tick_seed
from galaxysim.engine.rates import Cadence, Rates
from galaxysim.model.entities import Event, Universe


@dataclass(slots=True)
class TickContext:
    """State and services for resolving a single tick."""

    session: Session
    universe: Universe
    cadence: Cadence
    rates: Rates
    #: The tick being resolved -- that is, ``universe.tick_number + 1``.
    tick: int
    seed: int
    events: list[Event] = field(default_factory=list)
    #: Per-tick memo of derived colony state, keyed by colony id. Buildings do
    #: not change mid-tick except when one completes, and construction clears
    #: the entry when that happens. Without this, three resolvers each rebuild
    #: the same aggregate for every colony on every tick.
    _colony_cache: dict[int, object] = field(default_factory=dict)

    def cached_effects(self, colony_id: int, build):
        """Return memoized derived state for a colony, computing it once."""
        cached = self._colony_cache.get(colony_id)
        if cached is None:
            cached = build()
            self._colony_cache[colony_id] = cached
        return cached

    def invalidate_colony(self, colony_id: int) -> None:
        """Drop a colony's memo, after something changed what it derives from."""
        self._colony_cache.pop(colony_id, None)

    @classmethod
    def build(
        cls, session: Session, universe: Universe, cadence: Cadence, rates: Rates
    ) -> "TickContext":
        tick = universe.tick_number + 1
        return cls(
            session=session,
            universe=universe,
            cadence=cadence,
            rates=rates,
            tick=tick,
            seed=tick_seed(universe.seed, tick),
        )

    def rng(self, *scope: Any) -> random.Random:
        """A private RNG stream for ``scope`` within this tick.

        Pass enough to identify the roll uniquely, e.g.
        ``ctx.rng("combat", attacker_id, defender_id)``.
        """
        return rng_for(self.seed, *scope)

    def per_tick(self, per_hour: float) -> float:
        """This tick's share of a per-real-hour rate."""
        return self.cadence.per_tick(per_hour)

    def log(
        self,
        kind: str,
        message: str,
        *,
        civ_id: int | None = None,
        payload: dict | None = None,
    ) -> Event:
        """Record one line in the event log.

        Buffered and flushed with the rest of the tick, so a tick that fails
        part way leaves no orphaned events behind.
        """
        event = Event(
            universe_id=self.universe.id,
            civ_id=civ_id,
            tick=self.tick,
            kind=kind,
            message=message,
            payload=payload or {},
        )
        self.events.append(event)
        return event
