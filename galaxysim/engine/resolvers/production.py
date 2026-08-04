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

from galaxysim.colony import energy
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
    COLONY_POD_COST,
    COLONY_POD_WORK,
    FERTILISER,
    FLEET_COST_PER_STRENGTH,
    FLEET_UPKEEP_PER_STRENGTH,
    FOOD,
    FREIGHTER_COST_PER_CAPACITY,
    RESEARCH_COST_PER_PROGRESS,
    SALVAGE_FRACTION,
    WATER,
    accelerant_multiplier,
    can_afford,
    deposit,
    draw,
    gather,
    refine,
    spend,
)
from galaxysim.core.space import distance
from galaxysim.engine.context import TickContext
from galaxysim.engine.resolvers import queries
from galaxysim.model.entities import Building, Civ, Colony, Fleet, IntentKind, IntentStatus


#: How far a colony's warehouses can sustain a fleet, in light-years. Inside
#: this a fleet draws on whatever its civ has nearby; outside it there is
#: nothing to draw on and ships start deserting. This is the number that prices
#: force projection -- deep strikes cost, and they cost because of *distance*.
SUPPLY_RANGE_LY = 25.0

#: How close a fleet must be to a colony's system to be *at* it -- to exchange
#: cargo, or to be broken up in its yards. Lives here rather than in
#: :mod:`~galaxysim.engine.resolvers.logistics` only because logistics already
#: imports this module and the reverse would be a cycle.
DOCKING_TOLERANCE_LY = 0.01


def resolve(ctx: TickContext) -> None:
    # Read the world once. Every resolver below walks the same colonies and the
    # same fleets, and asking per civilization meant re-reading and re-filtering
    # the whole table once per civ -- which is how a tick's cost came to scale
    # with the size of the game rather than with what happened in it.
    colonies = queries.colonies_grouped(ctx)
    fleets = queries.fleets_by_civ(ctx.session, ctx.universe.id)

    for civ in queries.civs(ctx.session, ctx.universe.id):
        _produce(ctx, civ, colonies.get(civ.id, []), fleets.get(civ.id, []))
    _start_structures(ctx)
    _start_fleets(ctx)
    _decommission_fleets(ctx)
    _advance_construction(ctx, colonies)


# ---------------------------------------------------------------- production


def _produce(
    ctx: TickContext, civ: Civ, colonies: list[Colony], fleets: list[Fleet]
) -> None:
    if not colonies:
        return

    research_gain = 0.0

    for colony in colonies:
        effects = effects_for(ctx, colony)
        allocation = normalize(colony.labor)

        # Work against a plain dict and write it back once.
        #
        # ``Colony.stockpile`` is a MutableDict, so *every* key assignment fires
        # SQLAlchemy's change tracking -- weakref bookkeeping, parent lookups,
        # the lot. A tick's refining alone touches a few dozen keys per colony,
        # and at a hundred colonies that was seven thousand change events a tick
        # and the single largest cost in the engine. The ORM only needs to be
        # told once that the bag changed.
        stock = dict(colony.stockpile)

        _run_life_support(ctx, colony, stock, allocation, effects)
        if colony.population <= 0:
            colony.stockpile = stock
            continue

        # Power before work, because power is what work runs on. Everything
        # below reads the satisfaction this publishes.
        _run_power(ctx, colony, stock, allocation, effects)

        # Farm, then eat. A colony's standard of living is how much of what its
        # people need it actually met this hour, and that is what decides
        # whether the population grows, stalls or falls.
        _farm(ctx, colony, stock, allocation, effects)
        colony.standard_of_living = _feed(ctx, colony, stock)
        _grow_population(ctx, colony, effects)
        _extract(ctx, colony, stock, allocation, effects)
        _refine(ctx, colony, stock)
        research_gain += _research_output(ctx, colony, stock, allocation, effects)
        colony.stockpile = stock

    # Knowledge is the one thing that is civ-wide: a discovery is known
    # everywhere the moment it is made. Note what is *not* civ-wide: the
    # materials that bought it, which came out of specific warehouses on
    # specific worlds.
    civ.research_progress += research_gain
    _charge_fleet_upkeep(ctx, civ, colonies, fleets)


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
    generation: dict[str, float] = {}

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
        if spec.generation:
            # **Linear in level, unlike everything else here.** The square-root
            # curve above is right for a multiplier -- a level-100 mine is worth
            # ten level-1 mines because it is raising a rate. Power is not a
            # rate being raised, it is a quantity being produced, and ten
            # reactors make ten reactors' worth of it.
            #
            # Diminishing returns still apply, because the *materials* for the
            # next level stay quadratic. So a deep plant costs progressively
            # more per unit of power without the physics having to pretend.
            generation[spec.generation] = generation.get(spec.generation, 0.0) + max(
                1, building.level
            )

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
        generation=generation,
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
        "generation",
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
        generation: dict[str, float],
    ) -> None:
        self.sector_bonus = sector_bonus
        self.resource_bonus = resource_bonus
        self.habitability_offset = habitability_offset
        self.refining_bonus = refining_bonus
        self.life_support_recycling = life_support_recycling
        self.grants = grants
        self.cargo_throughput = cargo_throughput
        self.generation = generation

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


#: Satisfaction at or above which a colony counts as fully powered. Short of
#: 1.0 because satisfaction is a ratio of two floating-point sums and a colony
#: meeting its demand exactly should not read as browning out.
FULL_POWER = 0.999


#: Cache key under which a colony's power satisfaction is published for the
#: rest of the tick to read.
def _power_key(colony_id: int) -> tuple[str, int]:
    return ("power", colony_id)


def power_satisfaction(ctx: TickContext, colony: Colony) -> float:
    """How much of the power this colony wanted it actually had, 0.15 to 1.

    Falls back to the stored column when this tick has not computed it yet,
    which is the honest answer for the two callers that ask early: the governor
    resolves before production, so what it can react to is last tick's brownout.
    Once :func:`_run_power` has run, everything downstream reads the fresh
    figure it published.
    """
    return ctx.cached_effects(
        _power_key(colony.id), lambda: float(colony.power_satisfaction or 1.0)
    )


def _run_power(
    ctx: TickContext,
    colony: Colony,
    stock: dict[str, float],
    allocation: dict[str, float],
    effects: ColonyEffects,
) -> float:
    """Generate, burn the fuel it took, and publish what the colony got.

    The one quantity in the game that is a rate against a rate. A colony short
    of power is throttled rather than killed: smelters run slow, mines run slow,
    and it recovers the hour somebody delivers fuel. That is both what actually
    happens and the only version that is not a trap, since a colony with no
    power cannot mine the fuel to restart.

    Free routes first -- sunlight and ground heat cost nothing and are burned
    whether you like it or not -- then fuel is bought only for the shortfall. So
    a world with good sun runs its fusion plants at idle and keeps its helium-3,
    which is the right incentive and nobody had to write it.

    **Everything here is per real hour**, and it has to be said out loud because
    it was not true. Three of these terms used to be per *tick* -- the two
    industrial demands and the baseline generation -- while the life-support
    demand, sunlight, ground heat and reactor capacity were per hour. At the
    hourly cadence every soak and every test runs at, the two coincide and the
    mixture is invisible. Away from it, it is not:

    ======  =================  ======
    s/tick  min satisfaction   median
    ======  =================  ======
    3600    0.496              1.000
    900     1.000              1.000
    300     0.415              0.454
    ======  =================  ======

    The same world, the same ten simulated days, at half power in one universe
    and full in another because of a knob this project calls granularity rather
    than pace. ``hours`` now appears exactly where a tick genuinely consumes
    something -- the fuel actually burned -- and nowhere else.
    """
    hours = ctx.cadence.hours_per_tick
    wanted = energy.demand(
        industry_capacity_per_hour(ctx, colony),
        _extraction_capacity_per_hour(ctx, colony, allocation, effects),
        colony.population,
        effective_habitability(colony, effects),
    )
    if wanted <= 0:
        colony.power_satisfaction = 1.0
        return ctx.remember(_power_key(colony.id), 1.0)

    free = (
        energy.baseline_output(colony.population)
        + energy.solar_output(
            effects.generation.get("solar", 0.0), colony.world.stellar_flux
        )
        + energy.geothermal_output(
            effects.generation.get("geothermal", 0.0), colony.world.tectonic_activity
        )
    )

    shortfall = max(0.0, wanted - free)
    fuelled = min(
        shortfall,
        energy.fuelled_capacity(
            effects.generation.get("fission", 0.0), effects.generation.get("fusion", 0.0)
        ),
        energy.power_from_fuel_available(stock, hours),
    )
    for material, tonnes in energy.fuel_draw(fuelled, hours, stock).items():
        stock[material] = max(0.0, stock.get(material, 0.0) - tonnes)

    met = energy.satisfaction(free + fuelled, wanted)

    # The *edge*, not the state. A brownout is a condition that lasts, and
    # logging a condition every tick is not news -- it is 43% of the event log,
    # measured: eight homeworlds each writing "still at 50% power" 1,440 times
    # over a sixty-day soak, burying the 141 terraform completions and the 95
    # life-support failures a player actually needed to see. What an offline
    # player needs is the same pair a blockade gives them -- the lights went out,
    # and later they came back on -- which is why ``Colony.blockaded`` exists and
    # why this reads the stored satisfaction the same way.
    was_short = (colony.power_satisfaction or 1.0) < FULL_POWER
    colony.power_satisfaction = round(met, 4)
    if met < FULL_POWER and not was_short:
        ctx.log(
            "power_shortfall",
            f"{colony.name} is running at {met * 100:.0f}% power; "
            "industry and extraction are throttled",
            civ_id=colony.civ_id,
            payload={"colony_id": colony.id, "satisfaction": round(met, 4)},
        )
    elif met >= FULL_POWER and was_short:
        ctx.log(
            "power_restored",
            f"{colony.name} is back to full power",
            civ_id=colony.civ_id,
            payload={"colony_id": colony.id, "satisfaction": round(met, 4)},
        )
    return ctx.remember(_power_key(colony.id), met)


def _run_life_support(
    ctx: TickContext,
    colony: Colony,
    stock: dict[str, float],
    allocation: dict[str, float],
    effects: ColonyEffects,
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
        if colony.world.surface_water
        else ctx.rates.water_per_life_support * (1.0 - effects.life_support_recycling)
    )
    available = stock.get(WATER, 0.0)
    supply_capacity = (available / per_unit) if per_unit > 0 else need

    delivered = min(need, labor_capacity, supply_capacity)
    if delivered > 0 and per_unit > 0:
        stock[WATER] = max(0.0, available - delivered * per_unit)

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
    ctx: TickContext,
    colony: Colony,
    stock: dict[str, float],
    allocation: dict[str, float],
    effects: ColonyEffects,
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
        available = stock.get(FERTILISER, 0.0)
        if wanted > 0:
            supplied = min(wanted, available)
            draw(stock, {FERTILISER: supplied})
            grown *= max(0.2, supplied / wanted)

    deposit(stock, {FOOD: grown})


def _farm_profile(ctx: TickContext, colony: Colony) -> tuple[float, bool]:
    """This world's farm quality and whether it needs fertiliser shipped in.

    Columns, not a survey parse. See :func:`mining_rates`.
    """

    return colony.world.farm_quality, colony.world.needs_fertiliser


def _needs_fertiliser(ctx: TickContext, colony: Colony) -> bool:
    """True where nothing native is doing the soil chemistry for free."""
    return _farm_profile(ctx, colony)[1]


def agricultural_quality(ctx: TickContext, colony: Colony) -> float:
    """How productive a farmer is on this world. Memoized for the tick."""
    return _farm_profile(ctx, colony)[0]


def _feed(ctx: TickContext, colony: Colony, stock: dict[str, float]) -> float:
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

    available = stock.get(FOOD, 0.0)
    eaten = min(need, available)
    draw(stock, {FOOD: eaten})
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
    stock: dict[str, float],
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

    # Draglines and ore processing run on electricity. A colony that cannot
    # power them digs slower rather than not at all.
    worker_hours = _extraction_worker_hours(ctx, colony, allocation, effects) * (
        power_satisfaction(ctx, colony)
    )

    deposit(
        stock,
        {
            material: rate * worker_hours * effects.resource(material)
            for material, rate in rates.items()
        },
    )


def _extraction_worker_hours_per_hour(
    ctx: TickContext,
    colony: Colony,
    allocation: dict[str, float],
    effects: ColonyEffects,
) -> float:
    """Effective worker-hours of mining per real hour, before power.

    The per-hour figure exists in its own right because power demand is measured
    against generation, and generation is authored per hour. Handing the *per
    tick* number to :func:`galaxysim.colony.energy.demand` made a colony's power
    satisfaction depend on the universe's tick rate -- see :func:`_run_power`.
    """
    workers = workers_in(colony.population, allocation, EXTRACTION)
    if workers <= 0:
        return 0.0
    return (
        ctx.rates.extraction_per_worker_per_hour
        * workers
        * productivity_of(ctx, colony)
        * effects.sector(EXTRACTION)
    )


def _extraction_worker_hours(
    ctx: TickContext,
    colony: Colony,
    allocation: dict[str, float],
    effects: ColonyEffects,
) -> float:
    """Effective worker-hours of mining this tick, before power is considered."""
    return ctx.per_tick(
        _extraction_worker_hours_per_hour(ctx, colony, allocation, effects)
    )


def _extraction_capacity_per_hour(
    ctx: TickContext,
    colony: Colony,
    allocation: dict[str, float],
    effects: ColonyEffects,
) -> float:
    """Tonnes this colony could pull per real hour at full power."""
    rates = mining_rates(ctx, colony)
    if not rates:
        return 0.0
    hours = _extraction_worker_hours_per_hour(ctx, colony, allocation, effects)
    return sum(rate * hours * effects.resource(m) for m, rate in rates.items())


def _extraction_capacity(
    ctx: TickContext,
    colony: Colony,
    allocation: dict[str, float],
    effects: ColonyEffects,
) -> float:
    """Tonnes this colony could pull this tick at full power."""
    return ctx.per_tick(
        _extraction_capacity_per_hour(ctx, colony, allocation, effects)
    )


def mining_rates(ctx: TickContext, colony: Colony) -> dict[str, float]:
    """Tonnes per worker-hour this colony can pull, by material.

    Read off the world rather than derived from its survey. The deposits live
    inside a large JSON document and this is asked for every colony on every
    tick; :func:`galaxysim.worldgen.serialize.promoted_fields` computes it once
    at generation and terraforming refreshes it.
    """
    return colony.world.extraction or {}


def _refine(ctx: TickContext, colony: Colony, stock: dict[str, float]) -> None:
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
        stock,
        budget,
        ctx.cadence.hours_per_tick,
        priorities=colony.refining or None,
    )


def _research_output(
    ctx: TickContext,
    colony: Colony,
    stock: dict[str, float],
    allocation: dict[str, float],
    effects: ColonyEffects,
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
        covered += min(1.0, stock.get(material, 0.0) / want) if want > 0 else 1.0
    coverage = covered / len(RESEARCH_COST_PER_PROGRESS)

    progress = max(0.0, capacity * coverage)
    if progress <= 0:
        return 0.0

    # Spend what is actually there, up to the share this much progress wanted.
    for material, per_progress in sorted(RESEARCH_COST_PER_PROGRESS.items()):
        wanted = capacity * per_progress * coverage
        draw(stock, {material: min(wanted, stock.get(material, 0.0))})

    # Rare materials never gate research -- they only speed it up, out of
    # whatever this colony happens to be sitting on. A civ that draws a
    # metal-poor start researches slower, never not at all.
    multiplier, consumed = accelerant_multiplier(stock, progress)
    if consumed:
        draw(stock, consumed)

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


def industry_capacity_per_hour(ctx: TickContext, colony: Colony) -> float:
    """Industry-work per real hour at full power.

    The per-hour figure, because that is the unit power demand is measured in --
    generation is authored per hour throughout
    :mod:`galaxysim.colony.energy`. See :func:`_run_power`.
    """
    allocation = normalize(colony.labor)
    workers = workers_in(colony.population, allocation, INDUSTRY)
    if workers <= 0:
        return 0.0
    return (
        ctx.rates.industry_per_worker_per_hour
        * workers
        * productivity_of(ctx, colony)
        * effects_for(ctx, colony).sector(INDUSTRY)
    )


def industry_capacity(ctx: TickContext, colony: Colony) -> float:
    """Industry-work this colony could produce this tick with all the power it
    wanted.

    Kept separate from :func:`industry_output` because power demand is computed
    from it. Demand read off the *throttled* figure would fall as the brownout
    deepened, and a shortage would quietly cure itself by shrinking the load
    that caused it.
    """
    return ctx.per_tick(industry_capacity_per_hour(ctx, colony))


def industry_output(ctx: TickContext, colony: Colony) -> float:
    """Total industry-work this colony produces this tick.

    The currency of everything industrial: refining, buildings and ships are all
    paid for out of it, so a colony with nobody in industry neither processes
    nor finishes anything regardless of how rich the ground under it is.

    Smelting is where an industrial economy's energy actually goes, so a
    brownout shows up here first and hardest -- and since refining,
    construction, shipbuilding and terraforming are all paid out of this pool,
    throttling it throttles all four at once.
    """
    return industry_capacity(ctx, colony) * power_satisfaction(ctx, colony)


# ------------------------------------------------------------------- upkeep


def _charge_fleet_upkeep(
    ctx: TickContext, civ: Civ, colonies: list[Colony], fleets: list[Fleet]
) -> None:
    """Bill each fleet to the colonies near enough to supply it.

    A fleet you cannot pay for does not simply persist for free: unpaid ships
    desert, scaled to how badly short its supply was. That is what makes fleet
    size a standing economic commitment rather than a one-time purchase.

    Billing *local* stockpiles rather than a civ-wide pot is what gives this
    teeth now that matter is local. But local means "the worlds within reach",
    not "the single closest world": a fleet parked over a two-week-old outpost
    is not unsupplied because that outpost has no fuel, it is supplied from the
    developed world one jump behind it. Charging only the nearest colony meant a
    frontier fleet bled continuously while a full warehouse sat four light-years
    away, and filled the log with shortfalls that meant nothing.

    So the bill walks outward from the fleet, taking what each colony has until
    it is paid. Past :data:`SUPPLY_RANGE_LY` there is nothing to draw on, and
    *that* is what makes projecting force far from home expensive -- distance,
    rather than an accident of which rock the fleet happens to be sitting over.
    """
    civ.upkeep_paid = 1.0
    if not fleets:
        return

    billed = 0.0
    settled = 0.0

    for fleet in fleets:
        if fleet.strength <= 0:
            continue

        suppliers = queries.sorted_by_distance(
            colonies, fleet.position, within_ly=SUPPLY_RANGE_LY
        )
        shortfall = 0.0
        for resource, per_strength in sorted(FLEET_UPKEEP_PER_STRENGTH.items()):
            owed = ctx.per_tick(per_strength * fleet.strength)
            if owed <= 0:
                continue
            outstanding = owed
            for supplier in suppliers:
                if outstanding <= 1e-12:
                    break
                available = supplier.stockpile.get(resource, 0.0)
                paid = min(outstanding, available)
                if paid > 0:
                    supplier.stockpile[resource] = available - paid
                    outstanding -= paid
            shortfall = max(shortfall, outstanding / owed)
            billed += owed
            settled += owed - outstanding

        if shortfall <= 1e-9:
            continue

        attrition = shortfall * ctx.per_tick(ctx.rates.unpaid_fleet_attrition_per_hour)
        fleet.strength = max(0.0, fleet.strength - fleet.strength * attrition)

        ctx.log(
            "upkeep_shortfall",
            f"{fleet.name} went {shortfall * 100:.0f}% unsupplied"
            + (
                f" from {len(suppliers)} colonies in range"
                if suppliers
                else f" (nothing within {SUPPLY_RANGE_LY:.0f} ly)"
            )
            + "; ships are deserting",
            civ_id=civ.id,
            payload={"fleet_id": fleet.id, "shortfall": round(shortfall, 4)},
        )

    # The whole bill, in tonnes, against what the warehouses could cover. Read
    # next tick by anyone deciding whether this civ can carry another hull.
    if billed > 0:
        civ.upkeep_paid = min(1.0, settled / billed)


# -------------------------------------------------------------- construction


def _start_structures(ctx: TickContext) -> None:
    """Charge for and lay the foundations of ordered buildings."""
    for intent in queries.active_intents(
        ctx.session, ctx.universe.id, IntentKind.BUILD_STRUCTURE.value
    ):
        if intent.status != IntentStatus.QUEUED.value:
            continue

        colony = queries.colonies_by_id(ctx).get(intent.payload.get("colony_id", -1))
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
                    # The relationship, so the colony's in-memory building list
                    # includes it immediately. A raw ``colony_id`` leaves
                    # ``Colony.buildings`` as it was until something re-reads
                    # the row, and every industry effect is computed from that
                    # list.
                    colony=colony,
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

        colony = queries.colonies_by_id(ctx).get(intent.payload.get("colony_id", -1))
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

        # And the pods, which are the expensive part and were free.
        #
        # A pod is a civilization in a box -- reactors, foundries, pressure
        # vessels, soil and seed for fifty thousand people on a world that has
        # never held life. Charging nothing for it meant the pace of expansion
        # in the whole game was set by the build time of a gunboat, which is why
        # empires founded five hundred worlds in four weeks and grew four
        # percent in population doing it.
        pods = max(0, int(intent.payload.get("colony_pods", 0)))
        for resource, amount in sorted(COLONY_POD_COST.items()):
            cost[resource] = cost.get(resource, 0.0) + amount * pods

        # Built here, paid for here -- but paid for *over time*.
        #
        # An all-or-nothing bill cannot be saved for. A capital's governor
        # spends steel on its own industry the hour it is refined, so the
        # warehouse never holds a large sum at any one instant, and a colony pod
        # priced at more than one hour's surplus was unbuyable at any income:
        # eight civilizations sat on a hard wall at thirty-odd colonies for
        # sixty days, each with somewhere to settle, a ship to settle it, and
        # zero tonnes of steel. Not "too expensive" -- unsaveable-for.
        #
        # A yard now procures the way a real one does: it takes delivery of what
        # it can each hour and holds it against the order until the bill is met.
        # That makes an expensive thing *slow* rather than impossible, which is
        # the difference between a price and a wall.
        stock = dict(colony.stockpile)
        banked, short = gather(stock, cost, intent.payload.get("paid") or {})
        colony.stockpile = stock

        if short:
            intent.payload["paid"] = banked
            intent.result = (
                f"gathering materials at {colony.name} ("
                + ", ".join(
                    f"{amount:,.0f} {resource} short"
                    for resource, amount in sorted(short.items())
                )
                + ")"
            )
            continue

        intent.payload.pop("paid", None)
        intent.status = IntentStatus.IN_PROGRESS.value
        intent.result = ""
        intent.payload["work_remaining"] = (
            ctx.rates.fleet_work_per_strength * strength + COLONY_POD_WORK * pods
        )
        ctx.log(
            "build_started",
            f"Began construction of a {strength:.1f}-strength fleet at {colony.name}"
            + (f" with {pods} colony pod{'s' if pods != 1 else ''}" if pods else ""),
            civ_id=colony.civ_id,
            payload={"colony_id": colony.id, "strength": strength},
        )


def _decommission_fleets(ctx: TickContext) -> None:
    """Break up ships, returning part of their materials to the colony below.

    The inverse of :func:`_start_fleets`, and the reason it exists is that
    without it upkeep is a one-way ratchet. A fleet is a standing bill; a colony
    ship that has landed its pod is a hull with no remaining purpose still
    drawing that bill every hour, and the only way to stop paying was to let the
    crew desert -- which is to say, to fail. Ending a commitment has to be
    something a civilization can *decide*.

    Salvage lands where the ship is, like everything else made of matter.
    """
    orders = queries.active_intents(
        ctx.session, ctx.universe.id, IntentKind.DECOMMISSION.value
    )
    if not orders:
        return

    all_fleets = queries.fleets_by_id(ctx)
    colonies = queries.colonies_grouped(ctx)

    for intent in orders:
        fleet = all_fleets.get(intent.payload.get("fleet_id", -1))
        if fleet is None or fleet.civ_id != intent.civ_id:
            _fail(ctx, intent, "no such fleet", "Decommission order")
            continue

        if fleet.in_transit:
            # Not a failure: a ship under way will arrive.
            intent.status = IntentStatus.IN_PROGRESS.value
            intent.result = "awaiting arrival"
            continue

        yard = queries.nearest_of(colonies.get(intent.civ_id, []), fleet.position)
        if yard is None or distance(fleet.position, yard.world.system.position) > DOCKING_TOLERANCE_LY:
            _fail(
                ctx,
                intent,
                "a fleet can only be broken up at one of your colonies",
                "Decommission order",
            )
            continue

        salvage = {
            resource: amount * fleet.strength * SALVAGE_FRACTION
            for resource, amount in sorted(FLEET_COST_PER_STRENGTH.items())
        }
        extra_hold = max(
            0.0, fleet.cargo_capacity - fleet.strength * ctx.rates.cargo_capacity_per_strength
        )
        for resource, per_tonne in sorted(FREIGHTER_COST_PER_CAPACITY.items()):
            salvage[resource] = salvage.get(resource, 0.0) + per_tonne * extra_hold * SALVAGE_FRACTION

        # Whatever was in the hold comes off first. It was never part of the
        # ship, and losing a freighter's last load to the scrappers would be a
        # nasty piece of hidden arithmetic.
        deposit(yard.stockpile, dict(fleet.cargo))
        deposit(yard.stockpile, salvage)
        if fleet.passengers > 0:
            yard.population += fleet.passengers

        ctx.log(
            "fleet_decommissioned",
            f"{fleet.name} was broken up at {yard.name}, recovering "
            + ", ".join(f"{amount:,.0f} {resource}" for resource, amount in sorted(salvage.items())),
            civ_id=intent.civ_id,
            payload={"colony_id": yard.id, "strength": fleet.strength},
        )
        ctx.session.delete(fleet)
        all_fleets.pop(fleet.id, None)

        intent.status = IntentStatus.COMPLETED.value
        intent.resolved_tick = ctx.tick


def _advance_construction(ctx: TickContext, colonies_by_civ: dict) -> None:
    """Spend each colony's industry output on whatever it is building.

    Capacity is split evenly across that colony's active projects. Even rather
    than prioritised because a priority rule is a design decision the player
    should be making, and there is no interface for it yet -- an even split at
    least never starves one project indefinitely.
    """
    # Grouped once, outside the loop. This query used to run per colony, so a
    # civilization with a hundred colonies read the whole intent table a hundred
    # times to find the handful of orders that belonged to it.
    orders_by_colony: dict[int, list] = {}
    for intent in queries.active_intents(
        ctx.session, ctx.universe.id, IntentKind.BUILD_FLEET.value
    ):
        if intent.status == IntentStatus.IN_PROGRESS.value:
            orders_by_colony.setdefault(intent.payload.get("colony_id"), []).append(intent)

    structure_orders = _open_structure_orders(ctx)

    for civ in queries.civs(ctx.session, ctx.universe.id):
        for colony in colonies_by_civ.get(civ.id, []):
            structures = [b for b in sorted(colony.buildings, key=lambda b: b.id or 0)
                          if not b.is_complete]
            fleet_orders = orders_by_colony.get(colony.id, [])

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
                    # And tell the *order* it is done. Nothing did, and nothing
                    # else ever would have -- see :func:`_open_structure_orders`.
                    order = structure_orders.pop((colony.id, building.kind), None)
                    if order is not None:
                        order.status = IntentStatus.COMPLETED.value
                        order.resolved_tick = ctx.tick

            for intent in fleet_orders:
                remaining = float(intent.payload.get("work_remaining", 0.0)) - share
                if remaining > 0:
                    intent.payload["work_remaining"] = remaining
                    continue
                _commission_fleet(ctx, colony, intent)

    # Whatever is left in the map is an order this civilization will never see
    # finish, and an order that waits for ever is the bug being fixed here.
    _close_orphaned_structure_orders(ctx, structure_orders)


def _open_structure_orders(ctx: TickContext) -> dict[tuple[int, str], object]:
    """Construction orders still under way, keyed by the colony and what is rising.

    **Nothing used to close one.** ``_start_structures`` charged for the order,
    laid the foundations and set it ``IN_PROGRESS``, and that was the last thing
    that ever happened to it -- ``BUILD_STRUCTURE`` appeared exactly once in this
    module. The building finished, ``construction_completed`` was logged, and the
    order stayed open for the rest of the game.

    That is not a tidiness problem, it is the largest defect this project has
    found. ``queries.active_intents`` returns queued *and* in-progress orders, so
    :func:`galaxysim.engine.resolvers.governor.resolve` saw that kind as still
    pending for ever, and ``_maybe_build`` opens by returning when the colony is
    already building something. **Every governed colony in the game built exactly
    one structure and then stopped, permanently.** Measured across a 60-day soak:
    eighty-five colonies, sixteen buildings, all sixteen orders still open with
    their buildings long finished -- the first capital's fusion plant was ordered
    on tick 1, completed on tick 2, and its order was still holding the colony's
    only build slot fourteen hundred ticks later. That is why every homeworld in
    every soak ran at half power for the whole game: it could never build another
    reactor, or another anything.

    The asymmetry was right there in this file. ``BUILD_FLEET`` carries its work
    in the intent payload and :func:`_commission_fleet` closes it; a structure
    carries its work on the ``Building`` row, and the order was simply forgotten.

    Keyed on ``(colony_id, kind)`` because that pair is unique among open orders:
    ``_start_structures`` refuses to begin a second order for a kind whose
    building is still going up, so only one of each can ever be in flight.
    """
    open_orders: dict[tuple[int, str], object] = {}
    for intent in queries.active_intents(
        ctx.session, ctx.universe.id, IntentKind.BUILD_STRUCTURE.value
    ):
        if intent.status != IntentStatus.IN_PROGRESS.value:
            continue
        colony_id = intent.payload.get("colony_id")
        kind = str(intent.payload.get("kind", ""))
        if colony_id is not None:
            open_orders[(colony_id, kind)] = intent
    return open_orders


def _close_orphaned_structure_orders(
    ctx: TickContext, open_orders: dict[tuple[int, str], object]
) -> None:
    """End orders whose building this civilization is never going to see finish.

    Completing an order when its building completes fixes the ordinary case. It
    does not fix the two cases where the building never will, and an order left
    open in either of them reproduces the original bug exactly -- a colony that
    can no longer build anything, for a reason nobody can see.

    **The colony changed hands.** Worlds are captured now, and a capture
    reassigns ``colony.civ`` underneath whatever was rising there. The order
    belongs to the civilization that placed it, which no longer owns the ground.

    **The building is not there.** Nothing deletes buildings today, so this is
    defensive -- but "the order waits for ever" is precisely the failure being
    fixed, and it should not be reintroduced by an unhandled case.
    """
    if not open_orders:
        return
    colonies = queries.colonies_by_id(ctx)
    for (colony_id, kind), intent in open_orders.items():
        colony = colonies.get(colony_id)
        if colony is not None and colony.civ_id == intent.civ_id:
            if any(b.kind == kind for b in colony.buildings):
                continue  # still going up, which is the ordinary case
            reason = f"nothing is being built at {colony.name}"
        elif colony is None:
            reason = "the colony is gone"
        else:
            reason = f"{colony.name} no longer belongs to this civilization"
        _fail(ctx, intent, reason, "Construction order")


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
