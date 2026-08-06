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
from galaxysim.materials.recipes import RECIPES
from galaxysim.engine import intents
from galaxysim.engine.context import TickContext
from galaxysim.engine.resolvers import queries
from galaxysim.engine.resolvers.production import (
    agricultural_quality,
    effective_habitability,
    effects_for,
    power_satisfaction,
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
#:
#: **The industry order used to have no refinery**, which made it the only
#: policy in the game that could not turn its own ore into anything. Factories,
#: shipyards and spaceports are all priced in *steel*, steel is refined from
#: iron, and a refinery is what puts a colony's industry behind that conversion
#: -- so the one policy dedicated to being an empire's workshop was the one
#: guaranteed to run out of the material every entry on its list needs. Measured
#: at day 60: a world holding 10.9 million tonnes of iron and 777 thousand
#: tonnes of steel, wanting a factory priced at 1.62 million.
_BUILD_ORDER: dict[str, tuple[str, ...]] = {
    BALANCED: ("mine", "factory", "laboratory", "spaceport", "collector", "refinery", "granary"),
    EXTRACTION_POLICY: ("mine", "refinery", "collector", "factory", "spaceport", "granary"),
    INDUSTRY_POLICY: ("factory", "refinery", "mine", "shipyard", "spaceport", "collector"),
    RESEARCH_POLICY: ("laboratory", "factory", "mine", "spaceport", "collector"),
    SURVIVAL: ("hydroponics", "dome", "granary", "mine", "spaceport"),
}

#: Generating industries, in the order a governor tries them. Fuelled plants
#: first because they work anywhere; the free routes only pay off where the star
#: is close or the interior is live, and the governor finds that out by looking
#: at what the plant would actually produce rather than by being told.
_POWER_ORDER: tuple[str, ...] = (
    "fusion_plant",
    "fission_plant",
    "geothermal_plant",
    "solar_array",
)

#: Power satisfaction below which a governor stops whatever its policy wanted
#: and builds a power station instead.
#:
#: Not a preference. Power multiplies *everything* -- mining, refining,
#: construction, shipbuilding, terraforming all come out of pools it scales --
#: so a colony at seventy percent power is losing thirty percent of every other
#: thing it does, and no amount of the mine its policy asked for is worth as
#: much as the reactor that would un-throttle the mine it already has.
POWER_THRESHOLD = 0.95

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
    # Both reads are universe-wide and happen once. Asking per civilization
    # meant re-reading the whole intent table and the whole colony table once
    # per civ, which is the same mistake production was making and costs the
    # same thing: a tick that gets slower as the game gets bigger.
    pending_structures: dict[int, set[str]] = {}
    for intent in queries.pending(ctx, IntentKind.BUILD_STRUCTURE.value):
        colony_id = intent.payload.get("colony_id")
        if colony_id is not None:
            pending_structures.setdefault(colony_id, set()).add(
                str(intent.payload.get("kind", ""))
            )
    colonies_by_civ = queries.colonies_grouped(ctx)

    for civ in queries.civs(ctx.session, ctx.universe.id):
        for colony in colonies_by_civ.get(civ.id, []):
            if not colony.is_governed or colony.population <= 0:
                continue
            colony.labor = _labor_for(ctx, colony)
            if _replans_refining(ctx, colony):
                colony.refining = _refining_for(colony)
            _maybe_build(ctx, civ, colony, pending_structures.get(colony.id, set()))


#: How often a governor revisits which chains its colony should be running.
#:
#: In **hours**, never in ticks, so a five-minute universe re-plans exactly as
#: often in simulated time as an hourly one and pace invariance holds. That is
#: the same rule every other rate in the game is authored under.
#:
#: Why not every tick: rewriting the plan is a *decision*, not simulation.
#: Nothing about a warehouse changes enough in five minutes to justify
#: re-deciding, and the write is not free -- ``Colony.refining`` is a JSON column
#: on a mutable-tracked attribute, so assigning it re-encodes the document and
#: fires change tracking for every governed colony in the universe. The same
#: lesson ``Colony.stockpile`` already taught, in the same file that taught it.
REFINING_REPLAN_HOURS = 6.0


def _replans_refining(ctx: TickContext, colony: Colony) -> bool:
    """Whether this colony revisits its chains on this tick.

    Offset by colony id so the universe's governors do not all re-plan on the
    same tick -- the cost is spread across the interval rather than spiking.
    A colony that has never had a plan gets one immediately.
    """
    if not colony.refining:
        return True
    every = max(1, ctx.cadence.ticks_for_hours(REFINING_REPLAN_HOURS))
    return (ctx.tick + (colony.id or 0)) % every == 0


def _refining_for(colony: Colony) -> dict[str, float]:
    """Weight this colony's chains toward whatever it is short of.

    **Nothing has ever set this**, and the cost of that was invisible until a
    soak was read properly. ``Colony.refining`` has existed since refining did;
    with it empty every colony in the game fell back on the deliberately
    mediocre even plan, spreading its industry across thirteen chains in a fixed
    ratio and never once noticing a shortage.

    What that produced: an empire holding seven hundred million tonnes of alloys
    and making *no fuel at all*. Carbon is mined at about a seventh the rate of
    iron and smelting takes one carbon per ten iron, so smelting quietly
    consumed nearly the whole carbon supply and fuel synthesis -- which wants two
    -- ran on the remainder, which was nothing. The fleet then flew for
    twenty-six days on the fuel its homeworld started with, and when that bank
    finally emptied a third of the navy deserted inside two days. The collapse
    looked like an upkeep problem and was a refining problem.

    The rule is the one a competent player uses: **make what you have least of**.
    Each chain is weighted inversely to how much of its output the colony
    already holds, measured against its own average holding so the rule is
    scale-free and needs no constant. A warehouse full of alloys and empty of
    fuel puts its industry into fuel; when fuel is stocked and steel is short it
    swings back. Chains it cannot run at all are left out.

    It is still beatable, which is the standing bargain with automation: a player
    who stockpiles inputs ahead of a build will out-plan a governor that only
    ever looks at today. It just no longer starves.
    """
    stock = colony.stockpile
    runnable = {
        key: recipe
        for key, recipe in sorted(RECIPES.items())
        if all(stock.get(material, 0.0) > 0.0 for material in recipe.inputs)
    }
    if not runnable:
        return {}

    held = {
        key: sum(stock.get(material, 0.0) for material in recipe.outputs)
        for key, recipe in runnable.items()
    }
    # The colony's own scale, so "a lot" means a lot *for this colony*: a capital
    # holding a million tonnes of steel is well stocked, an outpost holding a
    # thousand may be equally so.
    typical = sum(held.values()) / len(held)
    if typical <= 0:
        return {key: 1.0 for key in runnable}
    return {key: 1.0 / (1.0 + amount / typical) for key, amount in held.items()}


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


def _worthwhile_power(colony: Colony) -> tuple[str, ...]:
    """Power stations that would actually generate something on this world.

    A solar array on a rock in the outer system of a red dwarf is a very large
    and very expensive nothing, and a geothermal tap on a dead world is a hole.
    Rather than encode which worlds get which, ask what the plant would produce
    here and drop the ones whose answer is approximately zero -- so the choice
    falls out of the planet, and a governor on a volcanic world reaches for the
    ground heat without anyone telling it that volcanic worlds are hot.
    """
    usable = []
    for kind in _POWER_ORDER:
        if kind == "solar_array" and colony.world.stellar_flux < 0.15:
            continue
        if kind == "geothermal_plant" and colony.world.tectonic_activity < 0.15:
            continue
        usable.append(kind)
    return tuple(usable)


def _maybe_build(
    ctx: TickContext, civ, colony: Colony, pending_kinds: set[str]
) -> None:
    """Queue the best thing this colony's policy wants that it can actually pay for.

    Industries are deepened as readily as they are started: a governor walks its
    build order and takes the highest-priority entry it can afford the *next
    level* of. On a young colony that means founding new industries; on a
    developed one it means growing the ones that matter to its policy, which is
    the same decision a player makes and the same one the economy prices.

    **"That it can afford" used to mean "stop here".** The walk returned at the
    first entry the colony could not pay for, on the reasoning that saving up for
    the thing the policy wants most beats always building the cheapest shed
    available. That is sound when a colony is *accumulating*, and a deadlock when
    it is not -- and nothing checked which it was. Measured at day 60: the most
    populous world in the game, a terraformed garden holding **24.8 billion
    people with room for 7,122 industry levels, had six**. It wanted a factory
    priced at 1.62 million tonnes of steel, held 777 thousand, and was earning
    almost none because a refinery is what puts industry behind that conversion
    and it had never built one. A mine and a refinery were both affordable that
    afternoon, at thirty thousand tonnes each. It built neither, every tick, for
    two months.

    So an entry it cannot afford is skipped rather than fatal, and the same rule
    now covers the policy list and the power list -- there is one rule here, not
    two. The quadratic cost curve is what keeps this from being a licence to
    build tat: anything a developed colony can still afford is something it has
    *neglected*, and the order still decides priority among whatever is actually
    open.

    One at a time: industry capacity is split across active projects, so queueing
    everything at once would leave a colony with five half-built things and no
    finished ones.

    **Power is the one exception**, and it has to be. A developed capital's next
    mine is months of work, and while that order stands the colony can queue
    nothing else -- so a governor that hit a brownout mid-project would sit at
    half output until the project it was already throttled on finished. Since
    power multiplies the very thing it is waiting for, a shortfall gets a
    concurrent slot. One slot: two reactors at once would split the industry
    that is short in the first place.
    """
    if levels_in_use(colony.buildings) >= max_total_levels(
        colony.population, colony.world.land_area_km2
    ):
        # Out of people or out of ground. Either way the colony has to grow
        # before it can develop further, and there is nothing to queue.
        return

    policy = colony.governor_policy if colony.governor_policy in POLICIES else BALANCED
    existing = {building.kind: building.level for building in colony.buildings}
    browning_out = power_satisfaction(ctx, colony) < POWER_THRESHOLD

    if browning_out and not any(
        BUILDING_TYPES_BY_KIND[kind].generation
        for kind in pending_kinds
        if kind in BUILDING_TYPES_BY_KIND
    ):
        # Nothing else this colony could queue is worth as much as ending the
        # brownout, so this jumps the queue rather than joining it.
        order = _worthwhile_power(colony)
    elif pending_kinds:
        return  # already building something, and the lights are on
    else:
        # A hostile world builds its way out first, whatever policy it was given.
        order = _BUILD_ORDER[policy]
        if effective_habitability(colony, effects_for(ctx, colony)) < HOSTILE_THRESHOLD:
            order = _BUILD_ORDER[SURVIVAL] + order

    for kind in order:
        spec = BUILDING_TYPES_BY_KIND[kind]
        level = existing.get(kind, 0) + 1
        if not can_afford(colony.stockpile, cost_of_level(spec.cost, level)):
            continue  # take the best thing it *can* buy -- see the docstring

        intents.build_structure(ctx.session, civ, colony.id, kind)
        # Production reads the queue in the very next stage and has to see this.
        # The tick's order queue is loaded once (queries.pending); adding to it
        # is the one thing that memo cannot notice for itself.
        ctx.invalidate(queries.INTENT_QUEUE)
        ctx.log(
            "governor_building",
            f"Governor at {colony.name} began "
            + (f"a {spec.name}" if level == 1 else f"{spec.name} level {level}"),
            civ_id=civ.id,
            payload={"colony_id": colony.id, "kind": kind, "level": level},
        )
        return
