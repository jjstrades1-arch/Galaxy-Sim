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

**What a civilization receives is now a technology rather than a number.** This
file used to increment ``Civ.techs_known`` and stop, and nothing anywhere read
that integer -- so every empire in every soak spent millions of tonnes of
electronics and ceramics on a counter with no effect on the simulation at all.
It now takes one candidate off the civ's frontier (:mod:`galaxysim.tech`), keeps
it as a :class:`~galaxysim.model.entities.Tech` row, and the effect reaches the
resolver that owns the stat it moves.
"""

from __future__ import annotations

from sqlalchemy import select

from galaxysim.engine.context import TickContext
from galaxysim.engine.resolvers import queries
from galaxysim.model.entities import Civ, IntentKind, IntentStatus, Tech
from galaxysim.tech import (
    EMPTY,
    ROOT_CONCEPTS,
    ROOT_DOMAINS,
    ROOT_NAME,
    Effect,
    Known,
    TechEffects,
    frontier_for,
)


def techs_of(ctx: TickContext, civ_id: int) -> list[Tech]:
    """Every tech a civ holds, oldest first. Memoised for the tick."""
    return ctx.cached_effects(
        ("techs", civ_id),
        lambda: list(
            ctx.session.scalars(
                select(Tech).where(Tech.civ_id == civ_id).order_by(Tech.id)
            ).all()
        ),
    )


def effects_for(ctx: TickContext, civ_id: int) -> TechEffects:
    """What this civ's techs do, collapsed to one multiplier a stat.

    Memoised per civ per tick, the same shape
    :func:`galaxysim.engine.resolvers.production.effects_for` uses for a colony's
    buildings, and for the same reason: several resolvers ask, and it is pure
    over rows that only research changes.
    """
    return ctx.cached_effects(
        ("tech_effects", civ_id),
        lambda: TechEffects.from_effects(
            [
                Effect(stat=tech.effect_stat, magnitude=tech.effect_magnitude)
                for tech in techs_of(ctx, civ_id)
            ]
        )
        or EMPTY,
    )


def grant_root(session, civ: Civ) -> Tech:
    """Give a new civilization the one tech every lineage descends from.

    ``Rates.base_speed_ly_per_hour`` has always described the galaxy as though
    this existed -- "every civ begins holding Lightspeed Travel" -- so this makes
    that claim true rather than adding a new one. Depth 0, and it moves nothing:
    the root is where lineages start, not a head start.
    """
    root = Tech(
        civ_id=civ.id,
        name=ROOT_NAME,
        domains=list(ROOT_DOMAINS),
        concepts=list(ROOT_CONCEPTS),
        effect_stat="drive",
        effect_magnitude=0.0,
        depth=0,
        parents=[],
    )
    session.add(root)
    return root


def frontier(ctx: TickContext, civ: Civ):
    """The candidates ``civ`` can research next."""
    known = [
        Known(id=tech.id, domains=tuple(tech.domains), concepts=tuple(tech.concepts))
        for tech in techs_of(ctx, civ.id)
    ]
    return frontier_for(
        civ.seed,
        known,
        civ.techs_known,
        effect_base=ctx.rates.tech_effect_base,
        effect_exponent=ctx.rates.tech_effect_exponent,
    )


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
        bought = False
        while True:
            cost = ctx.rates.research_cost(civ.techs_known)
            if civ.research_progress < cost:
                break

            candidates = frontier(ctx, civ)
            if not candidates:
                break  # no root yet; nothing to derive from

            # Which candidate is taken is a decision the player will make. Until
            # there is an interface for it, take the first -- the frontier is
            # already seeded per civ and per depth, so this is a determined
            # choice rather than an arbitrary one, and every candidate is worth
            # exactly the same magnitude anyway. Only its shape differs.
            chosen = candidates[0]

            civ.research_progress -= cost
            civ.research_invested += cost
            civ.techs_known += 1
            ctx.session.add(
                Tech(
                    civ_id=civ.id,
                    name=chosen.name,
                    domains=list(chosen.domains),
                    concepts=list(chosen.concepts),
                    effect_stat=chosen.effect.stat,
                    effect_magnitude=chosen.effect.magnitude,
                    depth=chosen.depth,
                    parents=list(chosen.parents),
                )
            )
            bought = True
            ctx.log(
                "research_completed",
                f"{chosen.name} ({chosen.effect.stat} "
                f"+{chosen.effect.magnitude * 100:.1f}%, cost {cost:.1f})",
                civ_id=civ.id,
                payload={
                    "depth": civ.techs_known,
                    "cost": round(cost, 4),
                    "name": chosen.name,
                    "stat": chosen.effect.stat,
                    "magnitude": round(chosen.effect.magnitude, 6),
                },
            )

        if bought:
            # The new row has to be visible to the next reader this tick, and
            # what it does has to be recomputed rather than served from the
            # cache built before it existed.
            ctx.session.flush()
            ctx.invalidate(("techs", civ.id))
            ctx.invalidate(("tech_effects", civ.id))
