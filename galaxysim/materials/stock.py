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


def draw(stock: dict[str, float], cost: dict[str, float]) -> None:
    """Deduct ``cost``, flooring each line at zero instead of raising.

    For callers that computed the amount *from* the stockpile a moment earlier --
    research buying as much progress as it can pay for, life support burning
    what it has. There, ``a / b * b > a`` in floating point is a routine event
    and refusing the spend over a billionth of a tonne would be absurd.

    Everything a player or the AI *orders* still goes through :func:`spend`,
    which raises: an order that cannot be paid for must fail loudly.
    """
    for key, amount in cost.items():
        stock[key] = max(0.0, stock.get(key, 0.0) - amount)


def deposit(stock: dict[str, float], gains: dict[str, float]) -> None:
    """Add ``gains`` to ``stock`` in place, creating keys as needed."""
    for key, amount in gains.items():
        stock[key] = stock.get(key, 0.0) + amount


def gather(
    stock: dict[str, float], cost: dict[str, float], banked: dict[str, float]
) -> tuple[dict[str, float], dict[str, float]]:
    """Take what ``stock`` can spare toward ``cost``, adding it to ``banked``.

    Returns the new escrow and whatever is *still* short, so an empty second
    value means the bill is met and the work may begin.

    **An all-or-nothing bill cannot be saved for**, and that is not a small
    point -- it was a wall across the middle of the game. A capital's governor
    spends steel on its own industry the hour it is refined, so the warehouse
    never holds a large sum at any one instant; asking :func:`can_afford` about
    a colony pod therefore got "no" at every income, forever. Eight AI
    civilizations stopped dead at thirty-odd colonies for sixty simulated days,
    each with somewhere to settle, a ship with a pod aboard, and zero tonnes of
    steel in the bank. They could afford it over a week and could not afford it
    in an hour, and only the second question was ever asked.

    A yard, or a quartermaster loading an expedition, procures the way a real
    one does: it takes delivery of what it can each hour and holds it against
    the order. That makes an expensive thing *slow* rather than impossible,
    which is the difference between a price and a wall.

    The escrow is the caller's to store -- on the intent, so it survives a
    restart and can be handed back if the order is abandoned.
    """
    banked = dict(banked)
    for key, amount in sorted(cost.items()):
        outstanding = amount - banked.get(key, 0.0)
        if outstanding <= 1e-9:
            continue
        taken = min(outstanding, stock.get(key, 0.0))
        if taken <= 0.0:
            continue
        stock[key] = stock.get(key, 0.0) - taken
        banked[key] = banked.get(key, 0.0) + taken

    short = {
        key: amount - banked.get(key, 0.0)
        for key, amount in sorted(cost.items())
        if amount - banked.get(key, 0.0) > 1e-9
    }
    return banked, short


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
