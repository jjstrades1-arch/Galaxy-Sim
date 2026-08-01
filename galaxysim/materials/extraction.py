"""Turning a world's geology into a colony's output.

The bridge that used to exist -- mapping real elements onto three abstract
resources -- is gone. Extraction now reads the deposits directly, so what a
colony produces *is* what its world is made of.

Three properties of a deposit matter, and they matter differently:

* **Abundance** is how much of the element the crust holds at all. Sets the
  ceiling.
* **Concentration** is how far ore bodies exceed that bulk figure. This is
  geological work -- hydrothermal circulation and volcanism gathering a diffuse
  element into a seam -- so a tectonically dead world can hold as much copper as
  Earth and have none of it worth digging.
* **Depth** is how hard it is to reach, and it divides everything.

A world with no uranium in its crust yields no uranium at any labour
allocation, any infrastructure level, and any tech. That is the point: it makes
a uranium-bearing world a place worth taking rather than a preference.
"""

from __future__ import annotations

from galaxysim.materials.catalogue import MATERIALS, RAW_MATERIALS
from galaxysim.worldgen.geology import Deposit

#: Scales a deposit's yield index into tonnes per worker per hour.
#:
#: Calibrated against the *other* side of the economy rather than picked: a
#: worker-hour of industry can push about 1.1 tonnes of ore through the refining
#: chains, and at this scale a median world gives up about 2.5 tonnes per
#: worker-hour across all its deposits combined. So a colony splitting its people
#: evenly mines a little faster than it can process, ore accumulates slowly, and
#: shipping the surplus somewhere with spare industry is worth doing.
#:
#: The first version of this number was seventy times higher, and the result was
#: a colony sitting on forty thousand tonnes of bauxite it could never refine --
#: which makes both extraction and geology meaningless, since every world is
#: effectively infinite.
EXTRACTION_SCALE = 0.9

#: Compresses the output range. Real crustal abundance spans five orders of
#: magnitude -- silicon is thousands of times commoner than copper, which is
#: thousands of times commoner than uranium -- and taking that literally makes
#: recipes impossible to author: a colony would drown in silicon while measuring
#: copper in grams.
#:
#: Raising the yield index to a fractional power keeps the *ordering* and the
#: relative scarcity while pulling the spread into a range recipes can be
#: written against. Rarity is still expressed twice over: rare elements are
#: absent from most worlds entirely, and where present they yield far less.
EXTRACTION_COMPRESSION = 0.4

#: Below this yield index a deposit is not worth the shaft. Keeps a colony's
#: output list to things it actually produces rather than eleven trace elements
#: at four grams an hour.
VIABLE_YIELD_INDEX = 2.0e-5

#: Materials worked by a different method, and therefore worth extracting at
#: concentrations that would never justify a mine.
#:
#: Helium-3 is the case this exists for. It is implanted by the stellar wind and
#: sits in the top few metres of regolith at around twenty parts per *billion* --
#: the real figure, and four orders of magnitude below what any ore seam needs
#: to be worth digging. But it is not dug: it is strip-mined and baked, megatonnes
#: of dust for kilograms of gas, which is a thing worth doing when the product is
#: fusion fuel.
#:
#: Without this it was catalogued, named a strategic material, offered as the
#: best research accelerant in the game, and obtainable nowhere -- a dead
#: mechanic that a scarcity sweep over a hundred thousand systems found by
#: reporting "absent" where it should have said "rare".
SPECIAL_VIABILITY: dict[str, float] = {
    "helium3": 1.0e-9,
}


def viability_floor(material: str) -> float:
    return SPECIAL_VIABILITY.get(material, VIABLE_YIELD_INDEX)


def extractable(deposits: dict[str, Deposit]) -> dict[str, Deposit]:
    """The deposits worth mining, keyed by material.

    Filters to elements that are in the materials catalogue *and* rich enough to
    be worth working -- a world's survey lists everything it contains, but its
    economy only produces what pays.
    """
    return {
        key: deposit
        for key, deposit in sorted(deposits.items())
        if key in MATERIALS
        and MATERIALS[key].is_raw
        and deposit.yield_index >= viability_floor(key)
    }


#: Materials whose extraction is a bulk process rather than a mine, scaled so
#: that a real trace abundance still yields a usable trickle. Helium-3 at twenty
#: parts per billion is worth having precisely because you process a mountain of
#: regolith for it.
BULK_PROCESS_SCALE: dict[str, float] = {
    "helium3": 40.0,
}


def extraction_rates(deposits: dict[str, Deposit]) -> dict[str, float]:
    """Tonnes per worker-hour for each material this world can produce."""
    return {
        key: round(
            deposit.yield_index**EXTRACTION_COMPRESSION
            * EXTRACTION_SCALE
            * BULK_PROCESS_SCALE.get(key, 1.0),
            6,
        )
        for key, deposit in extractable(deposits).items()
    }


def extract(
    deposits: dict[str, Deposit],
    worker_hours: float,
    efficiency: float = 1.0,
) -> dict[str, float]:
    """What ``worker_hours`` of extraction labour produces on this world.

    Workers are *not* split between deposits -- a mining workforce works every
    seam the colony has opened. Splitting them would make a world with many
    poor deposits worse than one with a single poor deposit, which is backwards.
    """
    if worker_hours <= 0 or efficiency <= 0:
        return {}
    return {
        key: rate * worker_hours * efficiency
        for key, rate in extraction_rates(deposits).items()
    }


def richest(deposits: dict[str, Deposit], limit: int = 5) -> list[tuple[str, Deposit]]:
    """The deposits that most define this world, best first. For display."""
    return sorted(
        extractable(deposits).items(), key=lambda item: -item[1].yield_index
    )[:limit]


def strategic_shortfall(
    deposits: dict[str, Deposit], wanted: tuple[str, ...]
) -> tuple[str, ...]:
    """Which of ``wanted`` this world simply cannot supply.

    Used to answer "what does this colony need shipped in", which is the
    question that turns geology into logistics and logistics into conflict.
    """
    available = extractable(deposits)
    return tuple(key for key in wanted if key not in available)


#: Materials whose absence tends to decide a colony's fate, in rough order of
#: how badly a civilization notices. Used for survey summaries and by the AI
#: when judging whether a world is worth settling.
STRATEGIC_MATERIALS: tuple[str, ...] = (
    "uranium",
    "rare_earths",
    "copper",
    "titanium",
    "water_ice",
    "phosphates",
    "carbon",
)

assert all(key in RAW_MATERIALS for key in STRATEGIC_MATERIALS)
