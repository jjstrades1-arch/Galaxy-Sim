"""Storing a :class:`Survey` and reading it back.

The full physical description of a world is a lot of structure, and almost all
of it is read-only after generation: it is displayed, and a handful of numbers
feed the economy. So it lives in a single JSON column rather than forty
columns, and the few fields the engine actually queries or mutates are promoted
onto :class:`~galaxysim.model.entities.World` proper.

Terraforming later writes back through here, which is why the round trip is
tested: a stored world must reload identical to the one that was generated.
"""

from __future__ import annotations

from dataclasses import asdict

from galaxysim.worldgen.biosphere import Biosphere
from galaxysim.worldgen.geology import Deposit
from galaxysim.worldgen.planet import Atmosphere, Body, Climate, Hydrosphere, Orbit
from galaxysim.worldgen.star import Star
from galaxysim.worldgen.survey import Survey


def survey_to_json(survey: Survey) -> dict:
    """Flatten a survey into plain JSON-safe structures."""
    return {
        "star": asdict(survey.star),
        "orbit": asdict(survey.orbit),
        "body": asdict(survey.body),
        "atmosphere": {
            "pressure_bar": survey.atmosphere.pressure_bar,
            "composition": dict(survey.atmosphere.composition),
            "albedo": survey.atmosphere.albedo,
            "greenhouse_k": survey.atmosphere.greenhouse_k,
        },
        "climate": asdict(survey.climate),
        "hydrosphere": asdict(survey.hydrosphere),
        "biosphere": asdict(survey.biosphere),
        "deposits": {name: asdict(d) for name, d in sorted(survey.deposits.items())},
        "moons": survey.moons,
        "has_rings": survey.has_rings,
        "world_class": survey.world_class,
        "habitability": survey.habitability,
    }


def survey_from_json(data: dict) -> Survey:
    """Rebuild a survey from storage."""
    return Survey(
        star=Star(**data["star"]),
        orbit=Orbit(**data["orbit"]),
        body=Body(**data["body"]),
        atmosphere=Atmosphere(
            pressure_bar=data["atmosphere"]["pressure_bar"],
            composition=dict(data["atmosphere"]["composition"]),
            albedo=data["atmosphere"]["albedo"],
            greenhouse_k=data["atmosphere"]["greenhouse_k"],
        ),
        climate=Climate(**data["climate"]),
        hydrosphere=Hydrosphere(**data["hydrosphere"]),
        biosphere=Biosphere(**data["biosphere"]),
        deposits={name: Deposit(**d) for name, d in data["deposits"].items()},
        moons=data["moons"],
        has_rings=data["has_rings"],
        world_class=data["world_class"],
        habitability=data["habitability"],
    )


#: How the real element inventory maps onto the three legacy resources the
#: economy currently runs on. Phase 2 replaces the legacy resources with the
#: elements themselves; until then this keeps the existing economy working off
#: real geology rather than a rolled yield table.
LEGACY_RESOURCE_SOURCES: dict[str, tuple[str, ...]] = {
    "metal": ("iron", "aluminium", "titanium", "copper", "nickel"),
    "energy": ("uranium", "thorium", "helium3", "deuterium", "carbon"),
    "volatiles": ("water_ice", "carbon", "sulfur", "phosphates"),
}

#: Scales the summed yield index of the contributing elements into the 0-2ish
#: range the existing production code expects. Calibrated against a sample of
#: generated worlds so the median lands near 0.6 -- the middle of the old
#: hand-authored yield ranges -- rather than saturating or vanishing. The
#: figures differ by orders of magnitude because the underlying crustal
#: abundances do: iron is percent-level, uranium is parts per million.
LEGACY_YIELD_SCALE: dict[str, float] = {
    "metal": 2.37,
    "energy": 1400.0,
    "volatiles": 144.0,
}


def legacy_resource_yield(survey: Survey) -> dict[str, float]:
    """Derive the old three-resource yields from real deposits.

    A bridge, deliberately: it means a world's economic value already follows
    from its actual geology -- ore grade, deposit depth, formation history --
    while the resource system itself is still the simple one.
    """
    yields: dict[str, float] = {}
    for resource, elements in LEGACY_RESOURCE_SOURCES.items():
        total = sum(
            survey.deposits[element].yield_index
            for element in elements
            if element in survey.deposits
        )
        scaled = total * LEGACY_YIELD_SCALE[resource]
        if scaled > 0.001:
            yields[resource] = round(min(3.0, scaled), 4)
    return yields
