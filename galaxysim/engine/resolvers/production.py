"""Economy, life support and construction.

This resolver runs for every civilization on every tick regardless of whether
anyone is logged in. Offline players must keep producing, or an async game
punishes people for sleeping.

A colony's tick runs in a deliberate order:

1. **Life support first.** People have to breathe before they work. A hostile
   world consumes labor, then stockpiled water, and then population.
2. **Extraction** pulls real elements out of the world's real deposits -- so a
   colony's output list *is* its crust, and a world with no uranium yields none
   at any labour allocation, infrastructure level or tech.
3. **Refining** turns some of that ore into the steel, alloys, electronics and
   fuel that everything is actually priced in. This is where a world stops
   being a pile of rock and becomes an economy.
4. **Agriculture and consumption.** Food is grown at whatever rate the world
   allows and then eaten, and what fraction of the requirement was met becomes
   the colony's **standard of living** -- which is what decides whether the
   population grows, stalls or falls.
5. **Research** buys progress with materials, out of the stockpile where the
   laboratories stand.
6. **Construction** spends whatever industry-work refining left.

Steps 3 and 6 draw on the same industry-work pool, split by
:attr:`Rates.refining_share_of_industry`. That competition is the point: a
colony cannot both process everything it digs and build at full speed.

The pacing rules live here more than anywhere else:

* Population grows **logistically** against effective habitability, so a colony
  plateaus instead of compounding forever.
* **Nothing artificial slows a wide empire.** A civ holding twelve colonies gets
  the output of twelve colonies. What slows it down is real -- distance, supply
  lines that have to be defended, and worlds that cost more to hold than they
  yield.
* A hostile world **taxes the workforce**. Habitability is not just a population
  cap: at low habitability a large share of the colony is occupied simply
  staying alive, and is therefore not mining, building or researching.
"""

from __future__ import annotations

import math

from galaxysim.colony.agriculture import HYDROPONICS, quality, regime
from galaxysim.colony.buildings import FLEET_CONSTRUCTION, building_type
from galaxysim.colony.industry import (
    cost_of_level,
    development,
    effect_scale,
    levels_in_use,
    max_total_levels,
    productivity,
    work_of_level,
)
from galaxysim.colony.labor import (
    AGRICULTURE,
    EXTRACTION,
    INDUSTRY,
    LIFE_SUPPORT,
    RESEARCH,
    normalize,
    workers_in,
)
from galaxysim.colony.population import capacity, growth_per_hour
from galaxysim.materials import (
    FERTILISER,
    FLEET_COST_PER_STRENGTH,
    FLEET_UPKEEP_PER_STRENGTH,
    FOOD,
    FREIGHTER_COST_PER_CAPACITY,
    RESEARCH_COST_PER_PROGRESS,
    WATER,
    accelerant_multiplier,
    can_afford,
    deposit,
    draw,
    extraction_rates,
    refine,
    spend,
)
from galaxysim.engine.context import TickContext
from galaxysim.engine.resolvers import queries
from galaxysim.model.entities import Building, Civ, Colony, Fleet, IntentKind, IntentStatus
from galaxysim.worldgen.serialize import (
    deposits_from_json,
    has_surface_water,
    survey_from_json,
)


def resolve(ctx: TickContext) -> None:
    for civ in queries.civs(ctx.session, ctx.universe.id):
        _produce(ctx, civ)
    _start_structures(ctx)
    _start_fleets(ctx)
    _advance_construction(ctx)


# ---------------------------------------------------------------- production


def _produce(ctx: TickContext, civ: Civ) -> None:
    colonies = queries.colonies_of(ctx.session, civ.id)
    if not colonies:
        return

    research_gain = 0.0

    for colony in colonies:
        effects = effects_for(ctx, colony)
        allocation = normalize(colony.labor)

        _run_life_support(ctx, colony, allocation, effects)
        if colony.population <= 0:
            continue

        # Farm, then eat. A colony's standard of living is how much of what its
        # people need it actually met this hour, and that is what decides
        # whether the population grows, stalls or falls.
        _farm(ctx, colony, allocation, effects)
        colony.standard_of_living = _feed(ctx, colony)
        _grow_population(ctx, colony, effects)
        _extract(ctx, colony, allocation, effects)
        _refine(ctx, colony)
        research_gain += _research_output(ctx, colony, allocation, effects)

    # Knowledge is the one thing that is civ-wide: a discovery is known
    # everywhere the moment it is made. Note what is *not* civ-wide: the
    # materials that bought it, which came out of specific warehouses on
    # specific worlds.
    civ.research_progress += research_gain
    _charge_fleet_upkeep(ctx, civ)


def effects_for(ctx: TickContext, colony: Colony) -> "ColonyEffects":
    """Memoized :func:`colony_effects` for the duration of one tick.

    Three resolvers ask for this per colony per tick and it is pure over the
    colony's finished buildings, so recomputing it each time is wasted work.
    Construction invalidates the entry when a building completes.
    """
    return ctx.cached_effects(colony.id, lambda: colony_effects(colony))


def colony_effects(colony: Colony) -> "ColonyEffects":
    """Aggregate what a colony's finished buildings do for it."""
    sector_bonus: dict[str, float] = {}
    resource_bonus: dict[str, float] = {}
    habitability_offset = 0.0
    refining_bonus = 0.0
    recycling = 0.0
    grants: set[str] = set()
    throughput = 0.0

    # Sorted by id: bonuses are additive so order does not change the sum, but
    # keeping iteration deterministic is a standing rule here.
    for building in sorted(colony.buildings, key=lambda b: b.id or 0):
        if not building.is_complete:
            continue
        spec = building_type(building.kind)
        # Everything an industry does scales with how far it has been developed,
        # on a square-root curve -- so a level-100 mine is worth ten level-1
        # mines rather than a hundred.
        scale = effect_scale(building.level)
        for sector, bonus in sorted(spec.sector_bonus.items()):
            sector_bonus[sector] = sector_bonus.get(sector, 0.0) + bonus * scale
        for resource, bonus in sorted(spec.resource_bonus.items()):
            resource_bonus[resource] = resource_bonus.get(resource, 0.0) + bonus * scale
        habitability_offset += spec.habitability_offset * scale
        refining_bonus += spec.refining_bonus * scale
        recycling += spec.life_support_recycling * scale
        grants.update(spec.grants)
        throughput += spec.cargo_throughput * scale

    return ColonyEffects(
        sector_bonus=sector_bonus,
        resource_bonus=resource_bonus,
        habitability_offset=habitability_offset,
        refining_bonus=refining_bonus,
        # Capped below 1.0: perfect recycling would make a colony need no
        # supplies at all, which would undo the whole point of supply lines.
        life_support_recycling=min(0.9, recycling),
        grants=frozenset(grants),
        cargo_throughput=throughput,
    )


class ColonyEffects:
    """What a colony's buildings contribute. Plain container, no behaviour."""

    __slots__ = (
        "sector_bonus",
        "resource_bonus",
        "habitability_offset",
        "refining_bonus",
        "life_support_recycling",
        "grants",
        "cargo_throughput",
    )

    def __init__(
        self,
        sector_bonus: dict[str, float],
        resource_bonus: dict[str, float],
        habitability_offset: float,
        refining_bonus: float,
        life_support_recycling: float,
        grants: frozenset[str],
        cargo_throughput: float,
    ) -> None:
        self.sector_bonus = sector_bonus
        self.resource_bonus = resource_bonus
        self.habitability_offset = habitability_offset
        self.refining_bonus = refining_bonus
        self.life_support_recycling = life_support_recycling
        self.grants = grants
        self.cargo_throughput = cargo_throughput

    def sector(self, name: str) -> float:
        """Multiplier for a labor sector, 1.0 with no buildings."""
        return 1.0 + self.sector_bonus.get(name, 0.0)

    def resource(self, name: str) -> float:
        """Multiplier for extracting one resource, 1.0 with no buildings."""
        return 1.0 + self.resource_bonus.get(name, 0.0)


def effective_habitability(colony: Colony, effects: ColonyEffects) -> float:
    """The world's habitability as the colony actually experiences it.

    Domes are the only thing that moves this, and they are what turn a hostile
    world from permanently expensive into merely awkward.
    """
    return min(1.0, colony.world.habitability + effects.habitability_offset)


def _run_life_support(
    ctx: TickContext, colony: Colony, allocation: dict[str, float], effects: ColonyEffects
) -> None:
    """Keep the colony breathing, or start killing it.

    Three layers, in order: assigned workers, then stockpiled water, then
    population. A colony at the end of a cut supply line works through its
    stores and only then starts dying -- so the failure is visible in the log
    for a long while before it is fatal.

    Water rather than an abstract supply number, and water a colony can only
    make by refining ice it has dug up itself or had shipped in. Water ice
    occurs on roughly a tenth of worlds, so most colonies breathe at the end of
    a supply line -- and the richest mining worlds, dry by definition, are the
    most dependent of all.

    Runs in every management mode. A manually run colony still breathes;
    forgetting to assign life-support labor must not silently kill a colony
    while its owner is asleep.
    """
    if colony.population <= 0:
        return

    habitability = effective_habitability(colony, effects)
    need = ctx.per_tick(
        colony.population * ctx.rates.life_support_per_pop_per_hour * (1.0 - habitability)
    )
    if need <= 0:
        return

    # Life support takes both people and materials, and is capped by whichever
    # runs out first. Workers alone cannot make air on a bare rock -- that is
    # what makes a hostile world depend on its supply line rather than merely
    # cost it labor.
    labor_capacity = ctx.per_tick(
        workers_in(colony.population, allocation, LIFE_SUPPORT)
        * ctx.rates.life_support_per_worker_per_hour
        * effects.sector(LIFE_SUPPORT)
    )

    # A world with oceans supplies its own. Life support there is a labour cost
    # and nothing more, which is why a marginally habitable but *wet* world is a
    # far better place to be than a rich dry one.
    per_unit = (
        0.0
        if has_surface_water(colony.world.survey or {})
        else ctx.rates.water_per_life_support * (1.0 - effects.life_support_recycling)
    )
    available = colony.stockpile.get(WATER, 0.0)
    supply_capacity = (available / per_unit) if per_unit > 0 else need

    delivered = min(need, labor_capacity, supply_capacity)
    if delivered > 0 and per_unit > 0:
        colony.stockpile[WATER] = max(0.0, available - delivered * per_unit)

    deficit = need - delivered
    if deficit <= 1e-12:
        return

    # Short. People start dying, in proportion to how much of the requirement
    # went unmet.
    unmet = min(1.0, deficit / need)
    lost = colony.population * unmet * ctx.per_tick(ctx.rates.starvation_per_hour)
    colony.population = max(0.0, colony.population - lost)

    starved_of = "water" if supply_capacity < labor_capacity else "life-support workers"
    ctx.log(
        "life_support_failing",
        f"{colony.name} cannot sustain its population -- out of {starved_of} "
        f"({unmet * 100:.0f}% of life support unmet); {lost:.2f} lost"
        + (" -- the colony has died" if colony.population <= 0 else ""),
        civ_id=colony.civ_id,
        payload={
            "colony_id": colony.id,
            "unmet": round(unmet, 4),
            "population": round(colony.population, 4),
        },
    )


def _farm(
    ctx: TickContext, colony: Colony, allocation: dict[str, float], effects: ColonyEffects
) -> None:
    """Grow this tick's food, at whatever rate the world allows.

    The world is most of the answer. Open farmland on a compatible biosphere
    feeds a colony with a tenth of its people; the same colony on a bare rock
    puts most of them into hydroponics and still imports fertiliser. That gap is
    the concrete payoff for finding an edible biosphere -- a thing habitability
    alone does not capture, since a world can be perfectly breathable and still
    a terrible farm.
    """
    workers = workers_in(colony.population, allocation, AGRICULTURE)
    if workers <= 0:
        return

    grown = ctx.per_tick(
        workers
        * ctx.rates.food_per_farmer_per_hour
        * agricultural_quality(ctx, colony)
        * productivity_of(ctx, colony)
        * effects.sector(AGRICULTURE)
    )
    if grown <= 0:
        return

    # Where there is no native ecology, the nutrients have to come from
    # somewhere. A shortfall does not stop the harvest, it shrinks it -- so a
    # colony cut off from fertiliser gets hungrier rather than instantly starving.
    if _needs_fertiliser(ctx, colony):
        wanted = grown * ctx.rates.fertiliser_per_food
        available = colony.stockpile.get(FERTILISER, 0.0)
        if wanted > 0:
            supplied = min(wanted, available)
            draw(colony.stockpile, {FERTILISER: supplied})
            grown *= max(0.2, supplied / wanted)

    deposit(colony.stockpile, {FOOD: grown})


def _needs_fertiliser(ctx: TickContext, colony: Colony) -> bool:
    """True where nothing native is doing the soil chemistry for free."""
    return ctx.cached_effects(
        ("farm-inputs", colony.world_id),
        lambda: regime(survey_from_json(colony.world.survey)) != "open farmland"
        if colony.world.survey
        else True,
    )


def agricultural_quality(ctx: TickContext, colony: Colony) -> float:
    """How productive a farmer is on this world. Memoized for the tick."""
    return ctx.cached_effects(
        ("farm", colony.world_id),
        lambda: quality(survey_from_json(colony.world.survey)) if colony.world.survey else HYDROPONICS,
    )


def _feed(ctx: TickContext, colony: Colony) -> float:
    """Eat, and return how well fed the colony ended up: 0 to 1.

    Standard of living is the single number that carries "is this place working
    for the people living in it" into the growth curve. Meet the requirement and
    the colony grows normally; fall short and growth scales down, then reverses.
    Nobody dies instantly of a bad harvest -- that is what the stores are for --
    but a colony that stays hungry shrinks.
    """
    need = ctx.per_tick(colony.population * ctx.rates.food_per_person_per_hour)
    if need <= 0:
        return 1.0

    available = colony.stockpile.get(FOOD, 0.0)
    eaten = min(need, available)
    draw(colony.stockpile, {FOOD: eaten})
    return min(1.0, eaten / need)


def _grow_population(ctx: TickContext, colony: Colony, effects: ColonyEffects) -> None:
    """Logistic growth toward whichever ceiling actually binds.

    On a living world that is the land: real surface area at a real density,
    scaled by habitability. On a dead one it is the habitats, which hold three
    orders of magnitude fewer people -- so a barren world plateaus as an outpost
    however long it is left alone, and only terraforming changes that.

    See :mod:`galaxysim.colony.population`.
    """
    ceiling = capacity(
        colony.world,
        colony.infrastructure,
        habitability=effective_habitability(colony, effects),
    )
    # Hungry people do not have children, and stay hungry long enough and there
    # are fewer of them. Below subsistence the multiplier goes negative, so the
    # same logistic formula runs the population back down without a special case.
    living = colony.standard_of_living
    threshold = ctx.rates.subsistence_threshold
    wellbeing = (
        (living - threshold) / (1.0 - threshold) if living >= threshold
        else (living - threshold) / threshold
    )

    colony.population = max(
        0.0,
        colony.population
        + ctx.per_tick(
            growth_per_hour(
                colony.population,
                ceiling,
                base_rate=ctx.rates.population_growth_per_hour,
                standard_of_living=wellbeing,
                hazard=colony.world.hazard,
            )
        ),
    )


def _extract(
    ctx: TickContext,
    colony: Colony,
    allocation: dict[str, float],
    effects: ColonyEffects,
) -> None:
    """Mine the world with whoever is assigned to it.

    What comes out is what the crust holds. There is no yield table between the
    geology and the stockpile any more: :func:`extraction_rates` reads the
    world's actual deposits, so a colony's output list is a statement about the
    planet rather than about its owner.

    Output lands where it was produced. There is no treasury to sweep it into --
    moving it anywhere else is a shipping problem.
    """
    workers = workers_in(colony.population, allocation, EXTRACTION)
    if workers <= 0:
        return

    rates = mining_rates(ctx, colony)
    if not rates:
        # A world with nothing worth digging. Perfectly legal -- and a reason to
        # settle it only for where it is rather than what it holds.
        return

    worker_hours = ctx.per_tick(
        ctx.rates.extraction_per_worker_per_hour
        * workers
        * productivity_of(ctx, colony)
        * effects.sector(EXTRACTION)
    )

    deposit(
        colony.stockpile,
        {
            material: rate * worker_hours * effects.resource(material)
            for material, rate in rates.items()
        },
    )


def mining_rates(ctx: TickContext, colony: Colony) -> dict[str, float]:
    """Tonnes per worker-hour this colony can pull, by material.

    Memoized for the tick: the deposits live inside the world's survey document
    and rebuilding them per colony per tick is the one hot read in the whole
    generation layer.
    """
    return ctx.cached_effects(
        ("mining", colony.world_id),
        lambda: extraction_rates(deposits_from_json(colony.world.survey or {})),
    )


def _refine(ctx: TickContext, colony: Colony) -> None:
    """Run this colony's processing chains on part of its industry output.

    Ore is nearly useless: buildings are priced in steel and construction
    materials, ships in alloys and electronics, life support in water. All of
    those come out of a recipe. So a colony that mines and never refines
    accumulates a growing pile of rock it cannot spend -- which is exactly what
    should happen to one whose owner never assigned anybody to industry.
    """
    effects = effects_for(ctx, colony)
    budget = (
        industry_output(ctx, colony)
        * ctx.rates.refining_share_of_industry
        * (1.0 + effects.refining_bonus)
    )
    if budget <= 0:
        return

    # No efficiency multiplier here: industry_output already carries the
    # colony's factory bonus, and applying it twice would let one building
    # compound against itself.
    refine(
        colony.stockpile,
        budget,
        ctx.cadence.hours_per_tick,
        priorities=colony.refining or None,
    )


def _research_output(
    ctx: TickContext, colony: Colony, allocation: dict[str, float], effects: ColonyEffects
) -> float:
    """Research this colony contributes this tick, and what it cost to get it.

    Research is **bought, not banked**. Laboratories consume instruments,
    optics, reactor parts and reagents at a real rate, and those are made of
    electronics, polymers, ceramics and fuel out of the warehouse next door. A
    colony with the workers, the buildings and an empty stockpile discovers
    nothing -- which is what makes an industrial base a prerequisite for a
    scientific one rather than a parallel track.

    What that buys, deliberately, is that a rival's research programme is
    something you can *reach*: cut the supply line feeding their laboratory
    world and their tech rate falls, without anyone needing a rule that says so.

    Capacity is sublinear in headcount: a colony twice the size does not think
    twice as fast. Linear would make population the only thing that matters, and
    combined with the superlinear cost curve it would leave research income and
    cost growing at the same rate, so progress would never actually slow down.
    """
    workers = workers_in(colony.population, allocation, RESEARCH)
    if workers <= 0:
        return 0.0

    capacity = ctx.per_tick(
        ctx.rates.research_per_colony_per_hour
        * math.sqrt(workers)
        * productivity_of(ctx, colony)
        * effects.sector(RESEARCH)
    )
    if capacity <= 0:
        return 0.0

    # How much of that capacity the stockpile can actually supply.
    #
    # **Averaged across the basket, not limited by the scarcest item.** That is
    # deliberate and it is the rule the whole research economy rests on: a civ
    # whose crust lacks one element must work harder, and is never locked out.
    # Taking the minimum instead -- which this did at first -- meant a homeworld
    # with no calcium made no ceramics, and therefore did no research at all,
    # forever. A civilization does not stop having ideas because one warehouse
    # is empty; it substitutes, badly, and goes slower.
    covered = 0.0
    for material, per_progress in sorted(RESEARCH_COST_PER_PROGRESS.items()):
        if per_progress <= 0:
            continue
        want = capacity * per_progress
        covered += min(1.0, colony.stockpile.get(material, 0.0) / want) if want > 0 else 1.0
    coverage = covered / len(RESEARCH_COST_PER_PROGRESS)

    progress = max(0.0, capacity * coverage)
    if progress <= 0:
        return 0.0

    # Spend what is actually there, up to the share this much progress wanted.
    for material, per_progress in sorted(RESEARCH_COST_PER_PROGRESS.items()):
        wanted = capacity * per_progress * coverage
        draw(colony.stockpile, {material: min(wanted, colony.stockpile.get(material, 0.0))})

    # Rare materials never gate research -- they only speed it up, out of
    # whatever this colony happens to be sitting on. A civ that draws a
    # metal-poor start researches slower, never not at all.
    multiplier, consumed = accelerant_multiplier(colony.stockpile, progress)
    if consumed:
        draw(colony.stockpile, consumed)

    return progress * multiplier


def development_of(colony: Colony) -> float:
    """How built-out this colony is, 0 to 1.

    Levels standing against levels the world and the population could support.
    A capital sits high; a fresh landing sits near zero and climbs as it builds.
    """
    return development(
        sum(b.level for b in colony.buildings if b.is_complete),
        max_total_levels(colony.population, colony.world.land_area_km2),
    )


def productivity_of(ctx: TickContext, colony: Colony) -> float:
    """How much a worker here gets done, from how well equipped the place is.

    This is what replaced raw ``infrastructure`` as an output multiplier. It is
    bounded, and it has to be: the industries that raise it *also* raise their
    own sector bonuses, and letting both grow without limit made the two
    compound into a hundredfold runaway on a developed capital.
    """
    return ctx.cached_effects(
        ("productivity", colony.id), lambda: productivity(development_of(colony))
    )


def construction_output(ctx: TickContext, colony: Colony) -> float:
    """Industry-work left for building things after refining has taken its cut."""
    return industry_output(ctx, colony) * (1.0 - ctx.rates.refining_share_of_industry)


def industry_output(ctx: TickContext, colony: Colony) -> float:
    """Total industry-work this colony produces this tick.

    The currency of everything industrial: refining, buildings and ships are all
    paid for out of it, so a colony with nobody in industry neither processes
    nor finishes anything regardless of how rich the ground under it is.
    """
    allocation = normalize(colony.labor)
    workers = workers_in(colony.population, allocation, INDUSTRY)
    if workers <= 0:
        return 0.0
    return ctx.per_tick(
        ctx.rates.industry_per_worker_per_hour
        * workers
        * productivity_of(ctx, colony)
        * effects_for(ctx, colony).sector(INDUSTRY)
    )


# ------------------------------------------------------------------- upkeep


def _charge_fleet_upkeep(ctx: TickContext, civ: Civ) -> None:
    """Bill each fleet to the nearest colony that could plausibly supply it.

    A fleet you cannot pay for does not simply persist for free: unpaid ships
    desert, scaled to how badly short the supplying colony is. That is what makes
    fleet size a standing economic commitment rather than a one-time purchase.

    Billing the *nearest* colony rather than a civ-wide pot is what gives this
    teeth now that matter is local. A fleet parked over a rich core world is
    cheap to keep; the same fleet at the far end of the frontier draws on
    whatever that outpost happens to have, so projecting force far from home
    means feeding it out there.
    """
    fleets = [f for f in queries.fleets(ctx.session, ctx.universe.id) if f.civ_id == civ.id]
    if not fleets:
        return

    for fleet in fleets:
        if fleet.strength <= 0:
            continue

        supplier = queries.nearest_colony(ctx.session, civ.id, fleet.position)
        if supplier is None:
            shortfall = 1.0  # no colonies at all: nothing can sustain it
        else:
            shortfall = 0.0
            for resource, per_strength in sorted(FLEET_UPKEEP_PER_STRENGTH.items()):
                owed = ctx.per_tick(per_strength * fleet.strength)
                available = supplier.stockpile.get(resource, 0.0)
                paid = min(owed, available)
                supplier.stockpile[resource] = available - paid
                if owed > 0:
                    shortfall = max(shortfall, (owed - paid) / owed)

        if shortfall <= 0:
            continue

        attrition = shortfall * ctx.per_tick(ctx.rates.unpaid_fleet_attrition_per_hour)
        fleet.strength = max(0.0, fleet.strength - fleet.strength * attrition)

        ctx.log(
            "upkeep_shortfall",
            f"{fleet.name} went {shortfall * 100:.0f}% unsupplied"
            + (f" from {supplier.name}" if supplier else " (no colony in range)")
            + "; ships are deserting",
            civ_id=civ.id,
            payload={"fleet_id": fleet.id, "shortfall": round(shortfall, 4)},
        )


# -------------------------------------------------------------- construction


def _start_structures(ctx: TickContext) -> None:
    """Charge for and lay the foundations of ordered buildings."""
    for intent in queries.active_intents(
        ctx.session, ctx.universe.id, IntentKind.BUILD_STRUCTURE.value
    ):
        if intent.status != IntentStatus.QUEUED.value:
            continue

        colony = ctx.session.get(Colony, intent.payload.get("colony_id", -1))
        if colony is None or colony.civ_id != intent.civ_id:
            _fail(ctx, intent, "no such colony", "Construction order")
            continue

        kind = str(intent.payload.get("kind", ""))
        try:
            spec = building_type(kind)
        except KeyError as exc:
            _fail(ctx, intent, str(exc), "Construction order")
            continue

        # Not slots: people and ground. An industry needs staff to run it and
        # land to stand on, and a colony that has run out of either cannot
        # develop further until it grows.
        ceiling = max_total_levels(colony.population, colony.world.land_area_km2)
        if levels_in_use(colony.buildings) >= ceiling:
            _fail(
                ctx,
                intent,
                f"{colony.name} cannot staff or site more industry "
                f"({levels_in_use(colony.buildings)}/{ceiling} levels)",
                "Construction order",
            )
            continue

        # An existing industry is deepened rather than duplicated.
        existing = next((b for b in colony.buildings if b.kind == kind), None)
        if existing is not None and not existing.is_complete:
            intent.result = f"{colony.name} is already expanding its {spec.name}"
            continue

        level = (existing.level + 1) if existing is not None else 1
        cost = cost_of_level(spec.cost, level)
        if not can_afford(colony.stockpile, cost):
            intent.result = f"insufficient resources at {colony.name}"
            continue

        spend(colony.stockpile, cost)
        if existing is not None:
            existing.level = level
            existing.work_remaining = work_of_level(spec.work, level)
            existing.completed_tick = None
            ctx.invalidate_colony(colony.id)
        else:
            ctx.session.add(
                Building(
                    colony_id=colony.id,
                    kind=kind,
                    level=1,
                    work_remaining=work_of_level(spec.work, 1),
                    started_tick=ctx.tick,
                )
            )
        intent.status = IntentStatus.IN_PROGRESS.value
        intent.result = ""
        ctx.log(
            "construction_started",
            f"{colony.name} began "
            + (f"expanding its {spec.name} to level {level}" if existing else f"a {spec.name}"),
            civ_id=colony.civ_id,
            payload={"colony_id": colony.id, "kind": kind, "level": level},
        )


def _start_fleets(ctx: TickContext) -> None:
    """Charge for ordered ships. Requires a shipyard at the building colony."""
    for intent in queries.active_intents(
        ctx.session, ctx.universe.id, IntentKind.BUILD_FLEET.value
    ):
        if intent.status != IntentStatus.QUEUED.value:
            continue

        colony = ctx.session.get(Colony, intent.payload.get("colony_id", -1))
        if colony is None or colony.civ_id != intent.civ_id:
            _fail(ctx, intent, "no such colony", "Build order")
            continue

        strength = float(intent.payload.get("strength", 1.0))
        if strength <= 0:
            _fail(ctx, intent, "strength must be positive", "Build order")
            continue

        if FLEET_CONSTRUCTION not in effects_for(ctx, colony).grants:
            _fail(
                ctx,
                intent,
                f"{colony.name} has no shipyard",
                "Build order",
            )
            continue

        # Strength is priced per point; hold is priced per tonne, and *beyond*
        # what the ship's own strength already provides. Without the second half
        # a player could order a strength-1 ship with a billion tonnes of hold
        # for the price of a gunboat -- cargo capacity was free, which made
        # freighters free, which made logistics free.
        cost = {r: amount * strength for r, amount in FLEET_COST_PER_STRENGTH.items()}
        default_hold = strength * ctx.rates.cargo_capacity_per_strength
        extra_hold = max(0.0, float(intent.payload.get("cargo_capacity", default_hold)) - default_hold)
        for resource, per_tonne in sorted(FREIGHTER_COST_PER_CAPACITY.items()):
            cost[resource] = cost.get(resource, 0.0) + per_tonne * extra_hold

        # Built here, paid for here. A yard can only use what has been shipped
        # to it.
        if not can_afford(colony.stockpile, cost):
            intent.result = f"insufficient resources at {colony.name}"
            continue

        spend(colony.stockpile, cost)
        intent.status = IntentStatus.IN_PROGRESS.value
        intent.result = ""
        intent.payload["work_remaining"] = ctx.rates.fleet_work_per_strength * strength
        ctx.log(
            "build_started",
            f"Began construction of a {strength:.1f}-strength fleet at {colony.name}",
            civ_id=colony.civ_id,
            payload={"colony_id": colony.id, "strength": strength},
        )


def _advance_construction(ctx: TickContext) -> None:
    """Spend each colony's industry output on whatever it is building.

    Capacity is split evenly across that colony's active projects. Even rather
    than prioritised because a priority rule is a design decision the player
    should be making, and there is no interface for it yet -- an even split at
    least never starves one project indefinitely.
    """
    for civ in queries.civs(ctx.session, ctx.universe.id):
        for colony in queries.colonies_of(ctx.session, civ.id):
            structures = [b for b in sorted(colony.buildings, key=lambda b: b.id or 0)
                          if not b.is_complete]
            fleet_orders = [
                intent
                for intent in queries.active_intents(
                    ctx.session, ctx.universe.id, IntentKind.BUILD_FLEET.value
                )
                if intent.status == IntentStatus.IN_PROGRESS.value
                and intent.payload.get("colony_id") == colony.id
            ]

            projects = len(structures) + len(fleet_orders)
            if projects == 0:
                continue

            share = construction_output(ctx, colony) / projects
            if share <= 0:
                continue

            for building in structures:
                building.work_remaining = max(0.0, building.work_remaining - share)
                if building.is_complete:
                    building.completed_tick = ctx.tick
                    # Infrastructure is what the industries standing here add up
                    # to, so finishing a level raises it by that level's share.
                    # Incremental rather than recomputed, because the expedition
                    # equipment a colony landed with is part of the same figure.
                    colony.infrastructure += effect_scale(building.level) - effect_scale(
                        building.level - 1
                    )
                    colony.development = development_of(colony)
                    # A finished building changes what the colony derives.
                    ctx.invalidate_colony(colony.id)
                    spec = building_type(building.kind)
                    ctx.log(
                        "construction_completed",
                        f"{spec.name} at {colony.name} reached level {building.level}",
                        civ_id=colony.civ_id,
                        payload={
                            "colony_id": colony.id,
                            "kind": building.kind,
                            "level": building.level,
                        },
                    )

            for intent in fleet_orders:
                remaining = float(intent.payload.get("work_remaining", 0.0)) - share
                if remaining > 0:
                    intent.payload["work_remaining"] = remaining
                    continue
                _commission_fleet(ctx, colony, intent)


def _commission_fleet(ctx: TickContext, colony: Colony, intent) -> None:
    position = colony.world.system.position
    strength = float(intent.payload.get("strength", 1.0))
    fleet = Fleet(
        universe_id=ctx.universe.id,
        civ_id=colony.civ_id,
        name=str(intent.payload.get("name") or f"{colony.civ.name} Fleet"),
        strength=strength,
        colony_pods=int(intent.payload.get("colony_pods", 0)),
        speed_ly_per_hour=ctx.rates.base_speed_ly_per_hour,
        # Every ship has some hold. A dedicated freighter is one built with a
        # lot of it and little else, rather than a separate kind of entity --
        # which is why cargo runs reuse the ordinary movement resolver.
        cargo_capacity=float(
            intent.payload.get("cargo_capacity", strength * ctx.rates.cargo_capacity_per_strength)
        ),
        cargo={},
        x=position.x,
        y=position.y,
        z=position.z,
    )
    ctx.session.add(fleet)
    intent.status = IntentStatus.COMPLETED.value
    intent.resolved_tick = ctx.tick
    ctx.log(
        "build_completed",
        f"{fleet.name} commissioned at {colony.name}",
        civ_id=colony.civ_id,
        payload={"strength": strength},
    )


def _fail(ctx: TickContext, intent, reason: str, label: str = "Order") -> None:
    intent.status = IntentStatus.FAILED.value
    intent.resolved_tick = ctx.tick
    intent.result = reason
    ctx.log("intent_failed", f"{label} failed: {reason}", civ_id=intent.civ_id)
