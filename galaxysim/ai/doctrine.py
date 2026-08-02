"""How hard an opponent is, and what that is allowed to be made of.

Difficulty here is composed of exactly three things, and each of them is
something a human player could also have or do:

* **Attention** -- how often it re-plans. In an asynchronous game this is the
  truest difficulty axis there is, because it is precisely what differs between
  human opponents: how often they check in. A dormant rival thinks once a day; a
  relentless one every hour.
* **Competence** -- how well it plays. How many of its worlds become industrial
  centres, whether it settles the *best* world or merely the nearest, how deep a
  build queue it keeps, how early it commits to terraforming.
* **Circumstance** -- what kind of galaxy the game is played in. The core is
  crowded and metal-rich, the rim thin and quiet, and
  :func:`galaxysim.bootstrap.add_civ` seats civilizations as far apart as the
  region allows -- so the region *is* the rival-proximity dial.

**What it is never made of: free materials or hidden information.** No field
here multiplies production, discounts a price or reveals anything a player could
not see, and ``tests/test_ai.py`` asserts that rather than trusting this comment.

The reason is narrower than the usual one and worth stating, because this is a
single-player game and the familiar argument -- keeping solo an honest rehearsal
for multiplayer -- does not apply. It is that **the soak is the only instrument
for telling whether the economy works.** Every economic failure this project has
found (a fuel famine that emptied every fleet on day twenty-six, a colony pod
that cost nothing at all, a sixty-day wall in front of expansion, a refining plan
sorted by alphabet so a capital on eight billion tonnes of iron made no steel)
surfaced by watching AI civilizations run under exactly the constraints a player
faces. Hand the AI a production multiplier and the soak stops measuring the game
and starts measuring the multiplier, and the one tool that keeps catching these
goes blind.

The second reason is smaller but real: being outplayed reads differently from
being out-subsidised, and players work out which one happened.
"""

from __future__ import annotations

from dataclasses import dataclass

#: How a civilization picks its next world.
NEAREST = "nearest"
BEST = "best"


@dataclass(frozen=True, slots=True)
class Doctrine:
    """One named standard of play.

    Every field is a timing, a threshold, a count, or a choice between legal
    player actions. That is the whole permitted vocabulary.
    """

    key: str
    name: str
    description: str

    # --- attention ----------------------------------------------------------
    #: Hours of simulated time between decisions. **Hours, never ticks**: a
    #: five-minute universe must not think twelve times as often as an hourly
    #: one, which is the rule every rate in the game is authored under.
    decision_interval_hours: float

    # --- competence ---------------------------------------------------------
    #: How many colonies are run as industrial centres, and therefore how many
    #: shipyards exist. Every AI in the game has had exactly one, permanently,
    #: because only the capital was ever given an industry policy -- so all
    #: expansion funnelled through a single yard on a single world.
    industrial_worlds: int
    #: :data:`NEAREST` or :data:`BEST`. Nearest is the reflex; best weighs
    #: habitability, room and *geology*, so an empire short of carbon goes and
    #: settles carbon.
    settle_by: str
    #: Ship orders a civ will keep in flight at once. At a colony pod costing a
    #: fortnight of a capital's yard, one at a time is a hard throttle.
    build_queue_depth: int
    #: Candidate systems weighed when choosing where to send a scout.
    scout_candidates: int
    #: Scouting range as a fraction of ``SUPPLY_RANGE_LY``. Bounded below one
    #: on purpose: upkeep is charged from the warehouses nearest a fleet and
    #: there is nothing to draw on past supply range, so a scout sent further
    #: deserts before it arrives -- exploration that consumes the explorer.
    #: Holding it inside the line also makes scouting and settling leapfrog
    #: outward together rather than one running away from the other.
    scout_range_fraction: float
    #: Habitability at or below which a settled world is worth reshaping, and
    #: the least construction its neighbourhood must muster before committing.
    #: A bolder doctrine starts campaigns on better worlds and with less behind
    #: it.
    terraform_habitability: float
    terraform_minimum_neighbourhood_work: float
    #: Terraforming campaigns a civ will run at once.
    #:
    #: One at a time is why a world took four months even after the projects
    #: themselves were made affordable: a full transformation is fourteen
    #: projects, and a civilization that runs them strictly in sequence is
    #: waiting on its own queue rather than on its industry.
    terraform_campaigns: int
    #: Hours of fleet upkeep banked before another hull is bought.
    #:
    #: Measured against the *upkeep materials* rather than output in general,
    #: which is the distinction that matters: upkeep is largely fuel, fuel is
    #: synthesised from ice and carbon, and an empire can hold a hundred million
    #: tonnes of steel while unable to keep one ship flying. Prudence is a real
    #: dial and it cuts both ways -- a fortnight's reserve on a large fleet
    #: wants more fuel than a civ that size ever accumulates, so an over-careful
    #: opponent stops expanding while entirely solvent.
    upkeep_reserve_hours: float
    #: Warship strength garrisoned per colony held.
    garrison_per_colony: float
    #: Warship strength this opponent will commit to a raid, over and above its
    #: garrison. ``0`` means it does not raid at all.
    #:
    #: The sharpest difficulty dial in the ladder, because it is the only one
    #: that can take something away from you. A steady opponent expands into
    #: empty sky and leaves your worlds alone; a driven one will blockade and
    #: annex your frontier the moment it has ships to spare. Nothing about the
    #: mechanic changes between levels -- the same siege, the same colony pod,
    #: the same supply problem -- only whether the opponent is willing to spend
    #: a fleet on it.
    raid_strength: float
    #: Largest population this opponent will pick a fight with. A frontier world
    #: falls in a day; a capital cannot be taken from orbit at all, so sending a
    #: raid at one is a fleet thrown away, and an opponent that does it reads as
    #: stupid rather than as difficult.
    raid_population_ceiling: float

    # --- circumstance -------------------------------------------------------
    #: Where the game is seated when the player does not say. A property of the
    #: galaxy rather than a gift to anybody: the player lives under the same sky.
    preferred_region: str
    #: How many rivals the level puts in the galaxy by default.
    rivals: int

    @property
    def settles_by_quality(self) -> bool:
        return self.settle_by == BEST


#: Barely playing. Thinks once a day, settles whatever is closest, keeps one
#: yard and one order, and will only reshape a world that is nearly dead
#: already. Seated out on the rim, where neighbours are a long way off.
DORMANT = Doctrine(
    key="dormant",
    name="Dormant",
    description="Checks in once a day and takes what is nearest. Far-flung neighbours.",
    decision_interval_hours=24.0,
    industrial_worlds=1,
    settle_by=NEAREST,
    build_queue_depth=1,
    scout_candidates=12,
    scout_range_fraction=0.5,
    terraform_habitability=0.10,
    terraform_minimum_neighbourhood_work=2.0e6,
    terraform_campaigns=1,
    upkeep_reserve_hours=24.0 * 7.0,
    garrison_per_colony=1.0,
    raid_strength=0.0,
    raid_population_ceiling=0.0,
    preferred_region="rim",
    rivals=2,
)

#: **The behaviour this project has had all along**, field for field. Kept as
#: the default so the existing calibration -- six colonies by day twenty-eight,
#: thirteen by day sixty -- keeps meaning what it measured.
STEADY = Doctrine(
    key="steady",
    name="Steady",
    description="Competent and unhurried. The pace the economy is calibrated against.",
    decision_interval_hours=2.0,
    industrial_worlds=1,
    settle_by=NEAREST,
    build_queue_depth=1,
    scout_candidates=40,
    scout_range_fraction=0.8,
    terraform_habitability=0.25,
    terraform_minimum_neighbourhood_work=5.0e5,
    terraform_campaigns=1,
    upkeep_reserve_hours=24.0 * 3.0,
    garrison_per_colony=2.0,
    raid_strength=0.0,
    raid_population_ceiling=0.0,
    preferred_region="arm",
    rivals=3,
)

#: Plays the game properly: several industrial worlds building pods in parallel,
#: worlds chosen for what is under them, and terraforming begun early rather
#: than as a last resort.
DRIVEN = Doctrine(
    key="driven",
    name="Driven",
    description="Builds several yards, settles for geology, and terraforms early.",
    decision_interval_hours=1.0,
    industrial_worlds=3,
    settle_by=BEST,
    build_queue_depth=2,
    scout_candidates=60,
    scout_range_fraction=0.9,
    terraform_habitability=0.35,
    terraform_minimum_neighbourhood_work=3.0e5,
    terraform_campaigns=2,
    upkeep_reserve_hours=24.0 * 2.0,
    garrison_per_colony=2.5,
    raid_strength=12.0,
    raid_population_ceiling=5.0e6,
    preferred_region="arm",
    rivals=5,
)

#: Everything Driven does, wider and with less margin, in the one part of the
#: galaxy where every good world is contested from the first week.
RELENTLESS = Doctrine(
    key="relentless",
    name="Relentless",
    description="Six industrial worlds, four hulls in build, thin reserves, and the Core.",
    decision_interval_hours=1.0,
    industrial_worlds=6,
    settle_by=BEST,
    build_queue_depth=4,
    scout_candidates=80,
    scout_range_fraction=0.95,
    terraform_habitability=0.45,
    terraform_minimum_neighbourhood_work=2.0e5,
    terraform_campaigns=4,
    upkeep_reserve_hours=24.0,
    garrison_per_colony=3.0,
    raid_strength=20.0,
    raid_population_ceiling=5.0e7,
    preferred_region="core",
    rivals=8,
)

#: Easiest first. The order is the ladder, and ``tests/test_ai.py`` asserts that
#: every monotonic dial actually climbs along it.
LADDER: tuple[Doctrine, ...] = (DORMANT, STEADY, DRIVEN, RELENTLESS)

DOCTRINES: dict[str, Doctrine] = {d.key: d for d in LADDER}

DEFAULT_DOCTRINE = STEADY


def doctrine(key: str | None) -> Doctrine:
    """Look up a doctrine, falling back to the default rather than raising.

    Tolerant on purpose: this is read from a stored universe on every tick, and
    a game saved before difficulty existed -- or one carrying a key from a later
    build -- should keep playing rather than crash.
    """
    return DOCTRINES.get(str(key or "").strip().lower(), DEFAULT_DOCTRINE)
