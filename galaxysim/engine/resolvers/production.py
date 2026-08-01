"""Economy, life support and construction.

This resolver runs for every civilization on every tick regardless of whether
anyone is logged in. Offline players must keep producing, or an async game
punishes people for sleeping.

A colony's tick runs in a deliberate order:

1. **Life support first.** People have to breathe before they work. A hostile
   world consumes labor, then stockpiled volatiles, and then population.
2. **Extraction, industry, research** from whatever labor is left, modified by
   the world's yields and the colony's buildings.
3. **Construction** spends the industry-work just produced.

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
from galaxysim.core.resources import (
    FLEET_COST_PER_STRENGTH,
    FLEET_UPKEEP_PER_STRENGTH,
    VOLATILES,
    can_afford,
    deposit,
    spend,
)
from galaxysim.engine.context import TickContext
from galaxysim.engine.resolvers import queries
from galaxysim.model.entities import Building, Civ, Colony, Fleet, IntentKind, IntentStatus


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
        effects = colony_effects(colony)
        allocation = normalize(colony.labor)

        _run_life_support(ctx, colony, allocation, effects)
        if colony.population <= 0:
            continue

        _grow_population(ctx, colony, effects)
        _extract(ctx, colony, allocation, effects, drag)
        research_gain += _research_output(ctx, colony, allocation, effects) * drag

    # Knowledge is the one thing that is civ-wide: a discovery is known
    # everywhere the moment it is made.
    civ.research_points += research_gain
    _charge_fleet_upkeep(ctx, civ)


def colony_effects(colony: Colony) -> "ColonyEffects":
    """Aggregate what a colony's finished buildings do for it."""
    sector_bonus: dict[str, float] = {}
    resource_bonus: dict[str, float] = {}
    habitability_offset = 0.0
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
        recycling += spec.life_support_recycling
        grants.update(spec.grants)
        throughput += spec.cargo_throughput

    return ColonyEffects(
        sector_bonus=sector_bonus,
        resource_bonus=resource_bonus,
        habitability_offset=habitability_offset,
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
        "life_support_recycling",
        "grants",
        "cargo_throughput",
    )

    def __init__(
        self,
        sector_bonus: dict[str, float],
        resource_bonus: dict[str, float],
        habitability_offset: float,
        life_support_recycling: float,
        grants: frozenset[str],
        cargo_throughput: float,
    ) -> None:
        self.sector_bonus = sector_bonus
        self.resource_bonus = resource_bonus
        self.habitability_offset = habitability_offset
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

    Three layers, in order: assigned workers, then stockpiled volatiles, then
    population. A colony at the end of a cut supply line works through its
    stores and only then starts dying -- so the failure is visible in the log
    for a long while before it is fatal.

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

    per_unit = ctx.rates.volatiles_per_life_support * (1.0 - effects.life_support_recycling)
    available = colony.stockpile.get(VOLATILES, 0.0)
    supply_capacity = (available / per_unit) if per_unit > 0 else need

    delivered = min(need, labor_capacity, supply_capacity)
    if delivered > 0 and per_unit > 0:
        colony.stockpile[VOLATILES] = max(0.0, available - delivered * per_unit)

    deficit = need - delivered
    if deficit <= 1e-12:
        return

    # Short. People start dying, in proportion to how much of the requirement
    # went unmet.
    unmet = min(1.0, deficit / need)
    lost = colony.population * unmet * ctx.per_tick(ctx.rates.starvation_per_hour)
    colony.population = max(0.0, colony.population - lost)

    starved_of = "volatiles" if supply_capacity < labor_capacity else "life-support workers"
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

    Output lands where it was produced. There is no treasury to sweep it into --
    moving it anywhere else is a shipping problem.
    """
    workers = workers_in(colony.population, allocation, EXTRACTION)
    if workers <= 0:
        return

    gains: dict[str, float] = {}
    for resource, yield_factor in sorted(colony.world.resource_yield.items()):
        per_hour = (
            ctx.rates.extraction_per_worker_per_hour
            * workers
            * colony.infrastructure
            * float(yield_factor)
            * effects.sector(EXTRACTION)
            * effects.resource(resource)
        )
        gains[resource] = ctx.per_tick(per_hour) * drag

    deposit(colony.stockpile, gains)


def _research_output(
    ctx: TickContext, colony: Colony, allocation: dict[str, float], effects: ColonyEffects
) -> float:
    """Research points this colony contributes this tick.

    Sublinear in headcount: a colony twice the size does not think twice as
    fast. Linear would make population the only thing that matters, and combined
    with the superlinear cost curve it would leave research income and cost
    growing at the same rate, so progress would never actually slow down.
    """
    workers = workers_in(colony.population, allocation, RESEARCH)
    if workers <= 0:
        return 0.0
    return ctx.per_tick(
        ctx.rates.research_per_colony_per_hour
        * math.sqrt(workers)
        * colony.infrastructure
        * effects.sector(RESEARCH)
    )


def industry_output(ctx: TickContext, colony: Colony) -> float:
    """Industry-work this colony produces this tick.

    The currency of construction: buildings and ships are both paid for in it,
    so a colony with nobody in industry finishes nothing regardless of how rich
    it is.
    """
    allocation = normalize(colony.labor)
    workers = workers_in(colony.population, allocation, INDUSTRY)
    if workers <= 0:
        return 0.0
    return ctx.per_tick(
        ctx.rates.industry_per_worker_per_hour
        * workers
        * colony.infrastructure
        * colony_effects(colony).sector(INDUSTRY)
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

        if FLEET_CONSTRUCTION not in colony_effects(colony).grants:
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

            share = industry_output(ctx, colony) / projects
            if share <= 0:
                continue

            for building in structures:
                building.work_remaining = max(0.0, building.work_remaining - share)
                if building.is_complete:
                    building.completed_tick = ctx.tick
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
