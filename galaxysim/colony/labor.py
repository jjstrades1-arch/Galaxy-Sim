"""Labor sectors: how a colony's population spends its time.

Four jobs, and the interesting one is the last. On a garden world life support
costs nothing and the whole population is free to work. On a hostile world it
takes real people to keep the air breathable, and those people are not mining.
That is what turns habitability from a growth cap into a *tax on the workforce* —
a gas giant with superb yields may spend half its population simply staying
alive, which is precisely the trade that makes such worlds interesting to hold.

Allocations are stored normalized, so they are always a division of the
population that exists rather than a wish list.
"""

from __future__ import annotations

EXTRACTION = "extraction"
INDUSTRY = "industry"
RESEARCH = "research"
LIFE_SUPPORT = "life_support"

#: Fixed order. Iterating sectors must be deterministic wherever it feeds a
#: calculation or an RNG draw.
SECTORS: tuple[str, ...] = (EXTRACTION, INDUSTRY, RESEARCH, LIFE_SUPPORT)


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
