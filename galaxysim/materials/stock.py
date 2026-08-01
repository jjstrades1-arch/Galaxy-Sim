"""Arithmetic on a stockpile.

Moved here from ``core.resources`` unchanged in behaviour -- these three
functions were always about *bags of stuff*, not about which stuff existed, and
they are the only place in the codebase that mutates a stockpile.
"""

from __future__ import annotations

from galaxysim.materials.catalogue import MATERIALS


def can_afford(stock: dict[str, float], cost: dict[str, float]) -> bool:
    """True if ``stock`` covers every line of ``cost``."""
    return all(stock.get(key, 0.0) >= amount for key, amount in cost.items())


def spend(stock: dict[str, float], cost: dict[str, float]) -> None:
    """Deduct ``cost`` from ``stock`` in place.

    Callers must check :func:`can_afford` first; this raises rather than letting
    a balance go negative, since a negative stockpile would silently corrupt
    every downstream production calculation.
    """
    if not can_afford(stock, cost):
        raise ValueError(f"cannot afford {cost} from {stock}")
    for key, amount in cost.items():
        stock[key] = stock.get(key, 0.0) - amount


def deposit(stock: dict[str, float], gains: dict[str, float]) -> None:
    """Add ``gains`` to ``stock`` in place, creating keys as needed."""
    for key, amount in gains.items():
        stock[key] = stock.get(key, 0.0) + amount


def total_mass(manifest: dict[str, float]) -> float:
    """Cargo tonnage of a manifest, respecting per-unit mass.

    A hold of electronics and a hold of ore are very different amounts of
    usefulness, and freighter capacity should notice.
    """
    return sum(
        amount * MATERIALS[key].mass_per_unit
        for key, amount in manifest.items()
        if key in MATERIALS
    )
