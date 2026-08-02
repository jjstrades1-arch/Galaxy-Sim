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


def promoted_fields(survey: Survey) -> dict:
    """The handful of derived facts the tick loop reads for every colony.

    Computed once here and stored as columns on :class:`World`, because the
    alternative is decoding the whole survey document per colony per tick to ask
    three small questions. Recomputed whenever terraforming changes the world,
    which is the only thing that can change the answers.
    """
    from galaxysim.colony.agriculture import quality, regime
    from galaxysim.materials.extraction import extraction_rates
    from galaxysim.terraform.plan import next_project

    return {
        "extraction": extraction_rates(survey.deposits),
        "surface_water": bool(survey.hydrosphere.liquid_water),
        "farm_quality": quality(survey),
        "needs_fertiliser": regime(survey) != "open farmland",
        # What this world can generate power from, which is a fact about where
        # it orbits and what its interior is doing.
        "stellar_flux": round(survey.star.flux_at(survey.orbit.semi_major_axis_au), 6),
        "tectonic_activity": round(survey.body.tectonic_activity, 4),
        # What terraforming this world would need next. A pure function of the
        # survey like everything else here, and promoted for the same reason:
        # deciding whether to terraform meant parsing the whole document, per
        # candidate world, per turn -- the exact cost these columns exist to
        # avoid, arrived at from the AI's side where the guard was not looking.
        "terraform_next": next_project(survey) or "",
    }


def has_surface_water(data: dict) -> bool:
    """Whether a world's own hydrosphere can supply a colony with water.

    Life support runs on water, and where there are oceans a colony draws its
    own. This is the line between a world that merely costs labour to live on
    and one that is permanently dependent on a supply route -- and it falls out
    of the phase diagram rather than being assigned. A frozen or dry world is
    supply-dependent no matter how much ice is locked in its crust, because
    getting water out of that ice is a refining chain somebody has to run.
    """
    return bool(data.get("hydrosphere", {}).get("liquid_water", False))


def deposits_from_json(data: dict) -> dict[str, Deposit]:
    """Just the geology out of a stored survey.

    Production asks this of every colony on every tick, and rebuilding the whole
    survey -- star, orbit, atmosphere, climate, biosphere, moons -- to read the
    ore grades would be an absurd amount of work to throw away. Extraction is
    the one part of the document the engine reads hot.
    """
    return {name: Deposit(**d) for name, d in sorted(data.get("deposits", {}).items())}
