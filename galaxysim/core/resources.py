"""The starting resource set.

Deliberately small. ``new_resource_type`` is a legal (rare, expensive) tech
effect, so a mature civilization's resource bag is open-ended -- these three are
only what everybody begins with. Nothing in the engine may assume the bag
contains exactly these keys.
"""

from __future__ import annotations

METAL = "metal"
ENERGY = "energy"
VOLATILES = "volatiles"

#: Resources every civilization starts able to extract.
BASE_RESOURCES: tuple[str, ...] = (METAL, ENERGY, VOLATILES)

#: What a civilization begins with, enough to found a second colony and build a
#: small fleet before production has to carry it.
STARTING_STOCKPILE: dict[str, float] = {METAL: 100.0, ENERGY: 100.0, VOLATILES: 50.0}

#: Cost per point of fleet strength.
FLEET_COST_PER_STRENGTH: dict[str, float] = {METAL: 10.0, ENERGY: 5.0}

#: Ongoing cost per point of fleet strength, per real hour.
#:
#: Without this a fleet is free once built, so hoarding is strictly correct and
#: a civilization accumulates ships without limit -- which is exactly what the
#: first pacing run showed the AI doing. Upkeep is what makes "how big a navy
#: can I actually afford" a decision rather than a formality.
FLEET_UPKEEP_PER_STRENGTH: dict[str, float] = {METAL: 0.05, ENERGY: 0.1}

#: Founding a colony has no flat price. What an expedition costs is what it
#: carries -- see :mod:`galaxysim.colony.expedition`.


def can_afford(stock: dict[str, float], cost: dict[str, float]) -> bool:
    """True if ``stock`` covers every line of ``cost``."""
    return all(stock.get(resource, 0.0) >= amount for resource, amount in cost.items())


def spend(stock: dict[str, float], cost: dict[str, float]) -> None:
    """Deduct ``cost`` from ``stock`` in place.

    Callers must check :func:`can_afford` first; this raises rather than letting
    a balance go negative, since a negative stockpile would silently corrupt
    every downstream production calculation.
    """
    if not can_afford(stock, cost):
        raise ValueError(f"cannot afford {cost} from {stock}")
    for resource, amount in cost.items():
        stock[resource] = stock.get(resource, 0.0) - amount


def deposit(stock: dict[str, float], gains: dict[str, float]) -> None:
    """Add ``gains`` to ``stock`` in place, creating keys as needed."""
    for resource, amount in gains.items():
        stock[resource] = stock.get(resource, 0.0) + amount
