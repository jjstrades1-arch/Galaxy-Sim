"""Labor sectors: how a colony's population spends its time.

Five jobs, and the interesting ones are the last two, because they are the only
two that are *bills* rather than investments.

**Life support** is what a hostile world charges before anyone does anything
else. Load scales with ``1 - habitability``, so a gas giant with superb yields
may spend half its population simply staying alive. That is what turns
habitability from a growth cap into a tax on the workforce.

**Agriculture** is the same idea with a different geography. A billion people
eat eighty thousand tonnes a day, which no supply line covers, so food is a
place question rather than a shipping one -- and how many farmers it takes
depends enormously on whether the world has soil, water and something edible
already living in it. See :mod:`galaxysim.colony.agriculture`.

Between them they mean two worlds with identical ore can be completely different
propositions, because one of them hands you back most of your population and the
other does not.

Allocations are stored normalized, so they are always a division of the
population that exists rather than a wish list.
"""

from __future__ import annotations

EXTRACTION = "extraction"
INDUSTRY = "industry"
RESEARCH = "research"
LIFE_SUPPORT = "life_support"
AGRICULTURE = "agriculture"

#: Fixed order. Iterating sectors must be deterministic wherever it feeds a
#: calculation or an RNG draw.
SECTORS: tuple[str, ...] = (EXTRACTION, INDUSTRY, RESEARCH, LIFE_SUPPORT, AGRICULTURE)


def normalize(allocation: dict[str, float]) -> dict[str, float]:
    """Clean an allocation into fractions of the population that sum to 1.

    Negatives are clamped to zero and unknown sectors dropped. An allocation
    that sums to nothing falls back to :func:`balanced_allocation` rather than
    leaving a colony with no assignment at all -- an unassigned population would
    silently stop breathing.
    """
    cleaned = {
        sector: max(0.0, float(allocation.get(sector, 0.0) or 0.0)) for sector in SECTORS
    }
    total = sum(cleaned.values())
    if total <= 0:
        return balanced_allocation()
    return {sector: value / total for sector, value in cleaned.items()}


def balanced_allocation() -> dict[str, float]:
    """An even split across every sector. The default for a new colony."""
    share = 1.0 / len(SECTORS)
    return {sector: share for sector in SECTORS}


def workers_in(population: float, allocation: dict[str, float], sector: str) -> float:
    """How many people are working ``sector``."""
    if population <= 0:
        return 0.0
    return population * allocation.get(sector, 0.0)
