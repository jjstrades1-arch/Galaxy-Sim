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
4. **Research** buys progress with materials, out of the stockpile where the
   laboratories stand.
5. **Construction** spends whatever industry-work refining left.

Steps 3 and 5 draw on the same industry-work pool, split by
:attr:`Rates.refining_share_of_industry`. That competition is the point: a
colony cannot both process everything it digs and build at full speed.

The pacing rules live here more than anywhere else:

* Population grows **logistically** against effective habitability, so a colony
  plateaus instead of compounding forever.
* Every civ pays **superlinear administrative drag** on its colony count
  (:meth:`Rates.colony_overhead`), applied as a divisor on output and research.
* A hostile world **taxes the workforce**. Habitability is not just a population
  cap: at low habitability a large share of the colony is occupied simply
  staying alive, and is therefore not mining, building or researching.
"""

from __future__ import annotations

import math

from galaxysim.colony.buildings import FLEET_CONSTRUCTION, building_type
from galaxysim.colony.labor import (
    EXTRACTION,
    INDUSTRY,
    LIFE_SUPPORT,
    RESEARCH,
    normalize,
    workers_in,
)
from galaxysim.materials import (
    FLEET_COST_PER_STRENGTH,
    FLEET_UPKEEP_PER_STRENGTH,
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
from galaxysim.worldgen.serialize import deposits_from_json, has_surface_water


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

    # Superlinear in colony count -- the anti-wide-expansion brake.
    drag = 1.0 / (1.0 + ctx.rates.colony_overhead(len(colonies)))
    research_gain = 0.0

    for colony in colonies:
        effects = effects_for(ctx, colony)
        allocation = normalize(colony.labor)

        _run_life_support(ctx, colony, allocation, effects)
        if colony.population <= 0:
            continue

        _grow_population(ctx, colony, effects)
        _extract(ctx, colony, allocation, effects, drag)
        _refine(ctx, colony)
        research_gain += _research_output(ctx, colony, allocation, effects) * drag

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
        for sector, bonus in sorted(spec.sector_bonus.items()):
            sector_bonus[sector] = sector_bonus.get(sector, 0.0) + bonus
        for resource, bonus in sorted(spec.resource_bonus.items()):
            resource_bonus[resource] = resource_bonus.get(resource, 0.0) + bonus
        habitability_offset += spec.habitability_offset
        refining_bonus += spec.refining_bonus
        recycling += spec.life_support_recycling
        grants.update(spec.grants)
        throughput += spec.cargo_throughput

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


def _grow_population(ctx: TickContext, colony: Colony, effects: ColonyEffects) -> None:
    """Logistic growth toward the world's carrying capacity."""
    capacity = effective_habitability(colony, effects) * ctx.rates.population_capacity_factor
    if capacity <= 0:
        # A world with no natural or artificial habitability supports no
        # natural growth at all -- such a colony only grows by immigration.
        return

    headroom = 1.0 - (colony.population / capacity)
    if headroom <= 0:
        # Over capacity. Let it decay back down rather than pinning it, so
        # losing habitability actually costs something.
        colony.population = max(
            0.0,
            colony.population
            + ctx.per_tick(colony.population * ctx.rates.population_growth_per_hour * headroom),
        )
        return

    growth_per_hour = (
        colony.population
        * ctx.rates.population_growth_per_hour
        * headroom
        * (1.0 - colony.world.hazard)
    )
    colony.population += ctx.per_tick(growth_per_hour)


def _extract(
    ctx: TickContext,
    colony: Colony,
    allocation: dict[str, float],
    effects: ColonyEffects,
    drag: float,
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
        * colony.infrastructure
        * effects.sector(EXTRACTION)
    ) * drag

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
        * colony.infrastructure
        * effects.sector(RESEARCH)
    )
    if capacity <= 0:
        return 0.0

    # How much of that capacity the stockpile can actually supply. The binding
    # constraint is whichever input runs out first, so a colony short of one
    # thing is short of research -- there is no substituting polymers for
    # electronics.
    affordable = capacity
    for material, per_progress in sorted(RESEARCH_COST_PER_PROGRESS.items()):
        if per_progress <= 0:
            continue
        affordable = min(affordable, colony.stockpile.get(material, 0.0) / per_progress)

    progress = max(0.0, min(capacity, affordable))
    if progress <= 0:
        return 0.0

    draw(colony.stockpile, {m: c * progress for m, c in RESEARCH_COST_PER_PROGRESS.items()})

    # Rare materials never gate research -- they only speed it up, out of
    # whatever this colony happens to be sitting on. A civ that draws a
    # metal-poor start researches slower, never not at all.
    multiplier, consumed = accelerant_multiplier(colony.stockpile, progress)
    if consumed:
        draw(colony.stockpile, consumed)

    return progress * multiplier


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
        * colony.infrastructure
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

        # Slots count everything standing or underway, so you cannot queue five
        # buildings on a three-slot world and have them all appear.
        used = len(colony.buildings)
        if used >= colony.world.slots:
            _fail(
                ctx,
                intent,
                f"{colony.world.name} has no free slots ({used}/{colony.world.slots})",
                "Construction order",
            )
            continue

        if any(b.kind == kind for b in colony.buildings):
            _fail(ctx, intent, f"{colony.name} already has a {spec.name}", "Construction order")
            continue

        if not can_afford(colony.stockpile, spec.cost):
            intent.result = f"insufficient resources at {colony.name}"
            continue

        spend(colony.stockpile, spec.cost)
        ctx.session.add(
            Building(
                colony_id=colony.id,
                kind=kind,
                work_remaining=spec.work,
                started_tick=ctx.tick,
            )
        )
        intent.status = IntentStatus.IN_PROGRESS.value
        intent.result = ""
        ctx.log(
            "construction_started",
            f"Began building a {spec.name} at {colony.name}",
            civ_id=colony.civ_id,
            payload={"colony_id": colony.id, "kind": kind},
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

        cost = {r: amount * strength for r, amount in FLEET_COST_PER_STRENGTH.items()}
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
                    # A finished building changes what the colony derives.
                    ctx.invalidate_colony(colony.id)
                    spec = building_type(building.kind)
                    ctx.log(
                        "construction_completed",
                        f"{spec.name} finished at {colony.name}",
                        civ_id=colony.civ_id,
                        payload={"colony_id": colony.id, "kind": building.kind},
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
