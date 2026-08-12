"""What a technology is made of, and what it is allowed to be worth.

``DESIGN.md`` §1 calls tech-as-a-generative-lineage the core mechanism of the
game, and until now it was a counter: ``Civ.techs_known`` went up and **nothing
in the simulation ever read it**. Civilizations bought it with electronics,
polymers, ceramics and fuel taken out of real colony stockpiles, and received a
number. This module is the half that says what they receive instead.

**Fairness is enforced here, at generation, rather than by rerolling until a
candidate looks fair.** The magnitude of a tech is a function of its depth and
nothing else; the roll chooses only *shape* -- which domain it belongs to, which
stat it moves, what it is called. Two civilizations that have spent the same
research are holding the same total magnitude, differently arranged. There is no
lucky lineage, because there is nothing for luck to act on.

That is a deliberate reading of DESIGN's two statements about fairness, which
are in mild tension: it also asks for per-stat diminishing returns so that "deep
single-stat lineages self-limit and lateral expansion stays competitive". Those
cannot both hold exactly -- a concave stacking curve means a civ that pours
everything into one stat gets *less* total effect than one that spreads, so
equal spend cannot be exactly equal power. What holds here is the pair that is
mechanically sound and testable:

* **monotone** -- more research paid is always more effective power, and
* **bounded spread** -- shape moves total power within a known band, and
  concentrating is never *better* than spreading.

``tests/test_tech.py`` asserts both rather than trusting this note.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The domain vocabulary. Fixed and small, because a domain is what a lineage
#: *is about* -- the thing a civilization can recognisably specialise in -- and
#: an unbounded list would make every civ's tree look like every other's.
DOMAINS: tuple[str, ...] = (
    "propulsion",
    "energy",
    "materials",
    "biology",
    "computation",
    "gravitics",
    "metallurgy",
    "cryogenics",
    "optics",
    "logistics",
)

#: Concepts are the open half: a freely mutable bag of tags carried down a
#: lineage, which is what lets two civs in the same domain arrive somewhere
#: different. They carry no mechanical weight on their own -- they condition
#: which stat a derived tech is *likely* to move, and they give a lineage a
#: recognisable identity.
CONCEPTS: tuple[str, ...] = (
    "lattice",
    "resonance",
    "catalysis",
    "superfluid",
    "annealing",
    "coherence",
    "symbiosis",
    "recursion",
    "harmonics",
    "sintering",
    "plasma",
    "quantum",
)

# --- the stats a tech may move -----------------------------------------------
#
# Every one of these is a number the engine **already reads**. That is the whole
# selection rule: an effect grammar that can name a stat nothing consumes is how
# you end up with a second counter, which is the defect this module exists to
# remove.

INDUSTRY = "industry"
RESEARCH = "research"
CONSTRUCTION = "construction"
DRIVE = "drive"
LIFE_SUPPORT = "life_support"
REFINING = "refining"

STATS: tuple[str, ...] = (INDUSTRY, RESEARCH, CONSTRUCTION, DRIVE, LIFE_SUPPORT, REFINING)

#: Which stats a domain plausibly reaches. Shape, not strength: this decides
#: what a lineage in a given domain tends to be good at, and never how much.
DOMAIN_STATS: dict[str, tuple[str, ...]] = {
    "propulsion": (DRIVE, CONSTRUCTION),
    "energy": (INDUSTRY, REFINING),
    "materials": (CONSTRUCTION, REFINING),
    "biology": (LIFE_SUPPORT, RESEARCH),
    "computation": (RESEARCH, INDUSTRY),
    "gravitics": (DRIVE, INDUSTRY),
    "metallurgy": (CONSTRUCTION, INDUSTRY),
    "cryogenics": (LIFE_SUPPORT, REFINING),
    "optics": (RESEARCH, DRIVE),
    "logistics": (INDUSTRY, LIFE_SUPPORT),
}


@dataclass(frozen=True, slots=True)
class Effect:
    """One bounded change to one stat.

    ``magnitude`` is a fractional improvement -- 0.03 is three percent -- and it
    is always positive. A tech never makes a civilization worse; the cost curve
    is what stops research being free.
    """

    stat: str
    magnitude: float


def budget(depth: int, *, base: float, exponent: float) -> float:
    """What a tech at ``depth`` is worth, regardless of what it turns out to be.

    Grows gently with depth so a deep lineage is not merely more expensive but
    also individually stronger, which is what keeps going deep a real choice
    against going wide. It is a *pure function of depth* -- see this module's
    docstring for why that is the entire fairness mechanism.
    """
    return base * float(depth + 1) ** exponent


def combine(magnitudes: list[float]) -> float:
    """Stack same-stat effects on the spec's diminishing-returns curve.

    ``1 - Π(1 - xᵢ)``: each further improvement acts on what is left rather than
    on the original, so twenty modest steps in one direction approach a ceiling
    instead of multiplying without limit. This is what makes a narrow lineage
    self-limiting and keeps a broad one competitive.
    """
    remaining = 1.0
    for magnitude in magnitudes:
        remaining *= 1.0 - max(0.0, min(0.99, magnitude))
    return 1.0 - remaining


@dataclass(frozen=True, slots=True)
class TechEffects:
    """Everything a civilization's techs do, collapsed to one multiplier a stat.

    Built once per civ per tick and read by the resolvers that own those stats.
    A civ holding nothing returns 1.0 everywhere, which is what makes the root
    state and the pre-tech state identical.
    """

    multipliers: dict[str, float]

    def of(self, stat: str) -> float:
        """The multiplier for ``stat``; 1.0 when nothing touches it."""
        return self.multipliers.get(stat, 1.0)

    @classmethod
    def from_effects(cls, effects: list[Effect]) -> "TechEffects":
        by_stat: dict[str, list[float]] = {}
        for effect in effects:
            by_stat.setdefault(effect.stat, []).append(effect.magnitude)
        return cls({stat: 1.0 + combine(mags) for stat, mags in sorted(by_stat.items())})

    def power(self) -> float:
        """Total effective power, for the balance invariant.

        The sum of what every stat was raised by. Deliberately shape-blind: it
        does not weight industry above drive, because the invariant being tested
        is that spend buys power, not that the game values one stat correctly.
        """
        return sum(value - 1.0 for value in self.multipliers.values())


EMPTY = TechEffects({})
