"""Pacing: how fast the world moves, kept separate from how often it updates.

These are two different knobs and conflating them is the single easiest way to
wreck the game's feel:

* **Cadence** is how often the world updates. We want it frequent -- five
  minutes by default -- so that async play feels responsive and a player coming
  back after a night away reads a fine-grained history instead of one enormous
  jump.

* **Rates** are how fast a civilization actually grows. We want that slow. There
  is no get-big-quick path; a competitive multi-system civ should be weeks of
  real time.

The rule that keeps them separate: *every rate in this module is authored per
real hour*, and the only place ticks enter the arithmetic is
:meth:`Cadence.per_tick`. Nothing else in the codebase may express a quantity
"per tick". Follow that and switching from five-minute to hourly ticks changes
only the resolution of the event log -- nobody grows faster or slower, and the
balance numbers stay meaningful because they are anchored to wall-clock time.

:func:`galaxysim.engine.tick` and the pace-invariance test in
``tests/test_pacing.py`` both lean on this.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

SECONDS_PER_HOUR = 3600


@dataclass(frozen=True, slots=True)
class Cadence:
    """How often the world ticks.

    This carries no balance meaning on its own. It is a resolution setting.
    """

    seconds_per_tick: int = 300  # five minutes

    def __post_init__(self) -> None:
        if self.seconds_per_tick <= 0:
            raise ValueError("seconds_per_tick must be positive")

    @property
    def ticks_per_hour(self) -> float:
        return SECONDS_PER_HOUR / self.seconds_per_tick

    @property
    def hours_per_tick(self) -> float:
        return self.seconds_per_tick / SECONDS_PER_HOUR

    def per_tick(self, per_hour: float) -> float:
        """Convert an authored per-real-hour quantity into this tick's share.

        The only tick-rate-dependent arithmetic in the simulation.
        """
        return per_hour * self.hours_per_tick

    def ticks_for_hours(self, hours: float) -> int:
        """Whole ticks needed to cover ``hours`` of wall-clock time.

        Rounds up: a job that needs part of a tick still occupies a whole one.
        """
        if hours <= 0:
            return 0
        return -(-int(round(hours * SECONDS_PER_HOUR)) // self.seconds_per_tick)


#: Named cadences. Solo play resolves on demand rather than on a clock, which we
#: represent as an hour of simulated time per "end turn" so that a solo game and
#: a multiplayer game advance at the same rate per unit of simulated time.
CADENCE_FIVE_MINUTE = Cadence(300)
CADENCE_HOURLY = Cadence(SECONDS_PER_HOUR)
CADENCE_SOLO_TURN = Cadence(SECONDS_PER_HOUR)


@dataclass(frozen=True, slots=True)
class Rates:
    """Every pacing constant, authored per real hour.

    Deliberately slow. These are first-pass numbers to be tuned against the
    pacing soak run, not final balance.
    """

    # --- Economy -----------------------------------------------------------
    #: Resource output per worker assigned to extraction, per real hour, before
    #: world yields and building bonuses.
    extraction_per_worker_per_hour: float = 1.0
    #: Industry-work per worker assigned to industry, per real hour. Refining,
    #: buildings and ships are all paid for in this.
    industry_per_worker_per_hour: float = 0.5
    #: Share of a colony's industry-work that goes to running refining chains
    #: rather than to construction. Ore is useless until it is processed and
    #: nothing is built out of ore, so a colony that put everything into
    #: construction would finish its current project and then have nothing to
    #: build the next one from. Half is the neutral default; tech and buildings
    #: are the right places to move it.
    refining_share_of_industry: float = 0.5
    #: Fraction of a colony's population that grows per real hour, before any
    #: habitability or tech modifier. 0.5%/hour is roughly 12%/day compounding.
    population_growth_per_hour: float = 0.005
    #: Hard ceiling on population as a multiple of the world's habitability
    #: score. Growth is logistic against this, so colonies plateau instead of
    #: compounding forever.
    population_capacity_factor: float = 100.0

    # --- Expansion ---------------------------------------------------------
    #: Wall-clock hours to establish a colony once a colony ship arrives.
    colonization_hours: float = 6.0
    #: Total administrative drag scales as
    #: ``colony_overhead_base * colony_count ** colony_overhead_exponent``.
    #: The exponent above 1.0 is what stops wide expansion from paying for
    #: itself: each new colony makes every existing one slightly more expensive.
    colony_overhead_base: float = 0.15
    colony_overhead_exponent: float = 1.35

    # --- Life support ------------------------------------------------------
    #: Life-support load per person per real hour on a wholly uninhabitable
    #: world. Scaled by ``(1 - effective_habitability)``, so a garden world
    #: costs nothing and a gas giant costs all of it.
    life_support_per_pop_per_hour: float = 0.08
    #: Life support one worker can sustain per real hour, before hydroponics.
    #: The ratio against the line above is what sets the workforce tax: at these
    #: numbers a fully hostile world spends roughly a fifth of its people just
    #: staying alive, before any dome is built.
    life_support_per_worker_per_hour: float = 0.4
    #: Water consumed per unit of life support *delivered* -- not per unit of
    #: shortfall. Sealed habitats need consumable input; workers cannot make air
    #: out of nothing on a bare rock.
    #:
    #: This is what makes hostile worlds supply-dependent rather than merely
    #: labour-expensive, and with real geology it decides which worlds can stand
    #: alone. Water ice occurs on roughly a tenth of worlds, so most colonies
    #: import their water -- and the richest mining worlds, which are dry by
    #: definition, live or die by their supply line.
    water_per_life_support: float = 1.0
    #: Water yielded per unit of water ice refined. The water_processing recipe
    #: is the only route, so a colony without ice in the ground has none.
    water_per_ice: float = 0.8
    #: Fraction of population lost per real hour at a total life-support
    #: failure. Deliberately gradual: an offline player should be able to see a
    #: colony dying and still have time to save it.
    starvation_per_hour: float = 0.04

    # --- Research ----------------------------------------------------------
    #: Research a colony's laboratories *could* do per real hour, before scaling
    #: by infrastructure and the square root of population. Capacity, not
    #: output: realising it costs materials out of that colony's stockpile
    #: (:data:`galaxysim.materials.costs.RESEARCH_COST_PER_PROGRESS`), and a
    #: colony with the workers but not the goods realises none of it.
    research_per_colony_per_hour: float = 0.5
    #: Cost of the next tech along a lineage scales as
    #: ``research_cost_base * (depth + 1) ** research_cost_exponent``. Also
    #: superlinear -- going deep gets progressively more expensive, so no
    #: lineage runs away.
    research_cost_base: float = 40.0
    research_cost_exponent: float = 1.6

    # --- Movement ----------------------------------------------------------
    #: Starting drive speed. Every civ begins holding Lightspeed Travel, so this
    #: is one light-year per hour: crossing a 10 ly sector takes ten hours.
    base_speed_ly_per_hour: float = 1.0

    # --- Construction ------------------------------------------------------
    #: Industry-work needed per unit of fleet strength. Construction is paid in
    #: work rather than wall-clock hours so that a colony's industry sector
    #: actually determines how fast it builds.
    fleet_work_per_strength: float = 6.0
    #: Cargo tonnage a ship carries per point of strength, unless the order
    #: specifies otherwise. Every ship has some hold; a freighter is one built
    #: with a lot of it.
    cargo_capacity_per_strength: float = 20.0

    # --- Combat ------------------------------------------------------------
    #: Fraction of a side's strength delivered as damage per real hour. Low on
    #: purpose: battles are attritional and play out over hours, so a player who
    #: logs in during one can still reinforce or withdraw.
    combat_intensity_per_hour: float = 0.25
    #: Symmetric swing applied to each side's effective strength, drawn from the
    #: tick seed. Enough that a narrow advantage is not a guaranteed win,
    #: not enough to make a real advantage meaningless.
    combat_variance: float = 0.12
    #: Fleets below this strength are destroyed rather than left as a rounding
    #: remnant that can never fight but still blocks the sector.
    fleet_destruction_threshold: float = 0.05
    #: Fraction of fleet strength lost per real hour when upkeep goes unpaid,
    #: at a total shortfall. Slow enough that a brief cash crunch is survivable.
    unpaid_fleet_attrition_per_hour: float = 0.1

    def colony_overhead(self, colony_count: int) -> float:
        """Total upkeep drag for holding ``colony_count`` colonies."""
        if colony_count <= 0:
            return 0.0
        return self.colony_overhead_base * colony_count**self.colony_overhead_exponent

    def research_cost(self, depth: float) -> float:
        """Research points to acquire a tech at ``depth`` along its lineage."""
        if depth < 0:
            raise ValueError("depth must be non-negative")
        return self.research_cost_base * (depth + 1.0) ** self.research_cost_exponent

    def with_overrides(self, **overrides: float) -> "Rates":
        """Return a copy with some constants replaced, for tuning experiments."""
        return replace(self, **overrides)


DEFAULT_RATES = Rates()


@dataclass(frozen=True, slots=True)
class SimConfig:
    """Everything that governs how a universe advances."""

    cadence: Cadence = CADENCE_FIVE_MINUTE
    rates: Rates = field(default_factory=lambda: DEFAULT_RATES)
