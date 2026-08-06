"""Research.

A research order is *standing*: it stays active across ticks and keeps buying
the next step the moment the civ can afford it. That is what keeps an offline
player advancing -- research should not stall because nobody was awake to click
it.

**Everything spent here was paid for in materials.** This resolver only spends
:attr:`Civ.research_progress`, and every point in that pool was bought out of
some colony's stockpile in
:func:`galaxysim.engine.resolvers.production._research_output` -- electronics,
polymers, ceramics and fuel, consumed where the laboratories stand. There is no
abstract currency here that appears out of assigning people to a sector.

Cost is superlinear in depth (:meth:`Rates.research_cost`), so each step along a
lineage costs meaningfully more than the last. That curve is the whole
anti-runaway story for research: there is no depth at which progress becomes
cheap, and a civ that pours everything into one direction buys fewer and fewer
techs for it.

Build-order step 7 replaces the depth counter here with real generated tech --
genomes, an effect grammar, and a frontier of candidates. The economics of
paying for the next step do not change when it lands; only what you receive
does.
"""

from __future__ import annotations

from galaxysim.engine.context import TickContext
from galaxysim.engine.resolvers import queries
from galaxysim.model.entities import Civ, IntentKind, IntentStatus


def resolve(ctx: TickContext) -> None:
    for intent in queries.pending(ctx, IntentKind.RESEARCH.value):
        civ = ctx.session.get(Civ, intent.civ_id)
        if civ is None:
            continue

        # Mark standing orders as in progress on first sight so the player can
        # tell an active research programme from an unstarted one.
        intent.status = IntentStatus.IN_PROGRESS.value

        # Loop: at a coarse cadence a civ may bank enough for several steps in
        # one tick, and it would be wrong for hourly ticks to waste that
        # overflow when five-minute ticks would not.
        while True:
            cost = ctx.rates.research_cost(civ.techs_known)
            if civ.research_progress < cost:
                break

            civ.research_progress -= cost
            civ.research_invested += cost
            civ.techs_known += 1
            ctx.log(
                "research_completed",
                f"Completed research step {civ.techs_known} (cost {cost:.1f})",
                civ_id=civ.id,
                payload={"depth": civ.techs_known, "cost": round(cost, 4)},
            )
