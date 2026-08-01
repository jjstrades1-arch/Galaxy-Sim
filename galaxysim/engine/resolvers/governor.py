"""Governors: colony depth is opt-in.

Everything the colony layer added -- labor sectors, building slots, local
stockpiles, supply routes -- is genuine depth, and genuine depth does not scale.
Running twelve colonies by hand is interesting; running sixty is data entry. So
every colony can be handed to a governor, and **new colonies are governed by
default**. A player opts *into* detail on the colonies they care about rather
than opting out of it everywhere else.

Two rules keep this honest:

* **A governor gets no hidden bonus.** It sets the same labor allocations and
  queues the same intents a player could, through the same API. Delegating is a
  convenience, never an advantage, and a player who micromanages well should
  beat a governor.
* **Life support is not the governor's job.** It runs for every colony in every
  mode, in :mod:`~galaxysim.engine.resolvers.production`. A manually managed
  colony still breathes -- forgetting to assign life-support labor must never
  silently kill a colony while its owner is asleep.

The governor also decides labor *before* production runs, so its judgement
applies on the tick it is made rather than the next one.
"""

from __future__ import annotations

from galaxysim.colony.buildings import BUILDING_TYPES_BY_KIND
from galaxysim.colony.industry import cost_of_level, levels_in_use, max_total_levels
from galaxysim.colony.labor import (
    AGRICULTURE,
    EXTRACTION,
    INDUSTRY,
    LIFE_SUPPORT,
    RESEARCH,
    normalize,
)
from galaxysim.materials import can_afford
from galaxysim.engine import intents
from galaxysim.engine.context import TickContext
from galaxysim.engine.resolvers import queries
from galaxysim.engine.resolvers.production import (
    agricultural_quality,
    effective_habitability,
    effects_for,
    productivity_of,
)
from galaxysim.model.entities import Colony, IntentKind

BALANCED = "balanced"
EXTRACTION_POLICY = "extraction"
INDUSTRY_POLICY = "industry"
RESEARCH_POLICY = "research"
SURVIVAL = "survival"

POLICIES: tuple[str, ...] = (
    BALANCED,
    EXTRACTION_POLICY,
    INDUSTRY_POLICY,
    RESEARCH_POLICY,
    SURVIVAL,
)

#: Weights each policy wants across the productive sectors, before life support
#: is carved out of the total. Life support is not listed: it is not a
#: preference, it is a bill, and :func:`_labor_for` computes it from the world.
_POLICY_WEIGHTS: dict[str, dict[str, float]] = {
    BALANCED: {EXTRACTION: 0.45, INDUSTRY: 0.3, RESEARCH: 0.25},
    EXTRACTION_POLICY: {EXTRACTION: 0.75, INDUSTRY: 0.2, RESEARCH: 0.05},
    INDUSTRY_POLICY: {EXTRACTION: 0.35, INDUSTRY: 0.6, RESEARCH: 0.05},
    RESEARCH_POLICY: {EXTRACTION: 0.3, INDUSTRY: 0.15, RESEARCH: 0.55},
    # Survival keeps a wide margin on life support and puts the rest into
    # building its way out of the problem.
    SURVIVAL: {EXTRACTION: 0.3, INDUSTRY: 0.65, RESEARCH: 0.05},
}

#: Safety factor on the farmers a governor keeps. Wider than the life-support
#: margin because a harvest shortfall is slower to notice and slower to fix: by
#: the time a colony is visibly hungry it has already stopped growing.
FOOD_MARGIN = 1.5

#: What each policy tries to build, in order of preference. Domes and
#: hydroponics come first everywhere they are needed -- a colony that cannot
#: breathe has no use for a laboratory.
_BUILD_ORDER: dict[str, tuple[str, ...]] = {
    BALANCED: ("mine", "factory", "laboratory", "spaceport", "collector", "refinery", "granary"),
    EXTRACTION_POLICY: ("mine", "refinery", "collector", "factory", "spaceport", "granary"),
    INDUSTRY_POLICY: ("factory", "mine", "shipyard", "spaceport", "collector"),
    RESEARCH_POLICY: ("laboratory", "factory", "mine", "spaceport", "collector"),
    SURVIVAL: ("hydroponics", "dome", "granary", "mine", "spaceport"),
}

#: Below this effective habitability a governor prioritises staying alive
#: regardless of the policy it was given.
#:
#: Calibrated against *derived* habitability, which runs lower and means more
#: than the rolled number it replaced: a breathable, watered, shielded world
#: scores around 0.6, and a genuinely marginal one well under 0.3. Leaving this
#: at 0.5 put ordinary homeworlds permanently into survival mode.
HOSTILE_THRESHOLD = 0.3

#: Safety factor on the life-support labor a governor reserves. Above 1.0 so a
#: governed colony keeps a margin rather than running exactly at the line, where
#: a small population change would tip it into starving.
LIFE_SUPPORT_MARGIN = 1.4


def resolve(ctx: TickContext) -> None:
    """Let every governed colony manage itself for this tick."""
    for civ in queries.civs(ctx.session, ctx.universe.id):
        pending_structures = {
            intent.payload.get("colony_id")
            for intent in queries.active_intents(
                ctx.session, ctx.universe.id, IntentKind.BUILD_STRUCTURE.value
            )
            if intent.civ_id == civ.id
        }

        for colony in queries.colonies_of(ctx.session, civ.id):
            if not colony.is_governed or colony.population <= 0:
                continue
            colony.labor = _labor_for(ctx, colony)
            if colony.id not in pending_structures:
                _maybe_build(ctx, civ, colony)


def _labor_for(ctx: TickContext, colony: Colony) -> dict[str, float]:
    """Decide this colony's labor split.

    **The two bills come off the top**, sized to what the world actually
    demands, and the policy divides whatever is left. That ordering is the whole
    idea: a governor on a hostile world is not choosing to under-produce, it is
    paying its bills first.

    Which bills those are depends entirely on the world. A garden world with an
    edible biosphere hands nearly its whole population back; a bare rock spends
    a fifth of its people breathing and most of the rest growing food under
    glass, and has almost nobody left to mine with. Two worlds with identical
    ore can therefore be completely different propositions -- which is the point
    of having both sectors rather than one abstract "overhead".
    """
    effects = effects_for(ctx, colony)
    habitability = effective_habitability(colony, effects)
    policy = colony.governor_policy if colony.governor_policy in POLICIES else BALANCED

    # Fraction of the population needed to cover life support, derived from the
    # same constants production uses rather than guessed at.
    per_worker = ctx.rates.life_support_per_worker_per_hour * effects.sector(LIFE_SUPPORT)
    if per_worker <= 0:
        life_support_share = 0.0
    else:
        need_per_person = ctx.rates.life_support_per_pop_per_hour * (1.0 - habitability)
        life_support_share = min(0.9, (need_per_person / per_worker) * LIFE_SUPPORT_MARGIN)

    # And the farmers, from the same constants the harvest uses.
    per_farmer = (
        ctx.rates.food_per_farmer_per_hour
        * agricultural_quality(ctx, colony)
        * productivity_of(ctx, colony)
        * effects.sector(AGRICULTURE)
    )
    if per_farmer <= 0:
        agriculture_share = 0.9
    else:
        agriculture_share = min(
            0.9, (ctx.rates.food_per_person_per_hour / per_farmer) * FOOD_MARGIN
        )

    if habitability < HOSTILE_THRESHOLD and policy != SURVIVAL:
        # A hostile world overrides the assignment it was given. A governor told
        # to prioritise research on a rock that cannot breathe should keep the
        # colony alive first and say nothing clever about it.
        policy = SURVIVAL

    weights = _POLICY_WEIGHTS[policy]
    bills = min(0.95, life_support_share + agriculture_share)
    productive = max(0.0, 1.0 - bills)

    allocation = {sector: share * productive for sector, share in weights.items()}
    allocation[LIFE_SUPPORT] = life_support_share
    allocation[AGRICULTURE] = agriculture_share
    return normalize(allocation)


def _maybe_build(ctx: TickContext, civ, colony: Colony) -> None:
    """Queue the next thing this colony's policy wants, if it can pay.

    Industries are deepened as readily as they are started: a governor walks its
    build order and takes the first entry it can afford the *next level* of. On a
    young colony that means founding new industries; on a developed one it means
    growing the ones that matter to its policy, which is the same decision a
    player makes and the same one the economy prices.

    One at a time: industry capacity is split across active projects, so queueing
    everything at once would leave a colony with five half-built things and no
    finished ones.
    """
    if levels_in_use(colony.buildings) >= max_total_levels(
        colony.population, colony.world.land_area_km2
    ):
        # Out of people or out of ground. Either way the colony has to grow
        # before it can develop further, and there is nothing to queue.
        return

    policy = colony.governor_policy if colony.governor_policy in POLICIES else BALANCED
    existing = {building.kind: building.level for building in colony.buildings}

    # A hostile world builds its way out first, whatever policy it was given.
    order = _BUILD_ORDER[policy]
    if effective_habitability(colony, effects_for(ctx, colony)) < HOSTILE_THRESHOLD:
        order = _BUILD_ORDER[SURVIVAL] + order

    for kind in order:
        spec = BUILDING_TYPES_BY_KIND[kind]
        level = existing.get(kind, 0) + 1
        if not can_afford(colony.stockpile, cost_of_level(spec.cost, level)):
            # Cannot pay for this one yet. Stop rather than skipping ahead --
            # saving up for the thing the policy wants most beats always
            # building the cheapest thing available.
            return
        intents.build_structure(ctx.session, civ, colony.id, kind)
        ctx.log(
            "governor_building",
            f"Governor at {colony.name} began "
            + (f"a {spec.name}" if level == 1 else f"{spec.name} level {level}"),
            civ_id=civ.id,
            payload={"colony_id": colony.id, "kind": kind, "level": level},
        )
        return
