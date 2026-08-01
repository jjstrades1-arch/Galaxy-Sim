"""Assembling a whole world, in causal order.

This module is the pipeline. It exists so the ordering is written down in one
place, because the ordering *is* the guarantee that worlds stay coherent:

    orbit -> body -> provisional atmosphere & temperature
          -> hydrosphere -> biosphere -> oxygenation
          -> final atmosphere & temperature -> geology -> habitability

Note where the biosphere sits. It is generated *between* two atmosphere passes,
because whether a world has free oxygen is a consequence of whether it has
photosynthetic life — not an independent roll that life is then inferred from.
Run it in that order and an oxygen world always has a reason for its oxygen.

Habitability is the last thing computed and never an input to anything above it.
It is a **conclusion**, and :meth:`Survey.habitability_reasons` can always say
why it came out the way it did.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace

from galaxysim.worldgen.biosphere import Biosphere, roll_biosphere, viable_biochemistry
from galaxysim.worldgen.geology import Deposit, add_ice_deposits, roll_geology
from galaxysim.worldgen.planet import (
    WATER_FREEZE_K,
    Atmosphere,
    Body,
    Climate,
    Hydrosphere,
    Orbit,
    build_atmosphere,
    build_hydrosphere,
    roll_body,
    roll_orbit,
    roll_water_endowment,
)
from galaxysim.worldgen.star import Star

#: Surface gravity a human-like species tolerates indefinitely, in g.
GRAVITY_COMFORT = (0.6, 1.4)
GRAVITY_SURVIVABLE = (0.15, 2.5)

#: Surface temperature a species tolerates unprotected, in kelvin.
TEMP_COMFORT = (283.0, 303.0)
TEMP_SURVIVABLE = (243.0, 323.0)

#: People per square kilometre a fully developed world sustains. Anchored to
#: Earth: ~8 billion people across ~1.5e8 km2 of land is about 53/km2.
SUSTAINABLE_DENSITY_PER_KM2 = 53.0

#: Earth's radius, for converting relative radii into real surface area.
EARTH_RADIUS_KM = 6371.0


@dataclass(frozen=True, slots=True)
class Survey:
    """Everything known about a world. The unit this module produces."""

    star: Star
    orbit: Orbit
    body: Body
    atmosphere: Atmosphere
    climate: Climate
    hydrosphere: Hydrosphere
    biosphere: Biosphere
    deposits: dict[str, Deposit]
    moons: int
    has_rings: bool
    world_class: str
    habitability: float

    # --- derived geometry ---------------------------------------------------

    @property
    def surface_area_km2(self) -> float:
        radius_km = self.body.radius_earth * EARTH_RADIUS_KM
        return 4.0 * math.pi * radius_km * radius_km

    @property
    def land_area_km2(self) -> float:
        """Usable surface. Ocean does not count; ice caps mostly do not."""
        usable = self.hydrosphere.land_fraction - self.hydrosphere.ice_fraction * 0.7
        return self.surface_area_km2 * max(0.02, usable)

    @property
    def carrying_capacity(self) -> float:
        """How many people this world could eventually support.

        Real land area times a real density, scaled by how pleasant the world
        is. An Earth-analogue lands in the billions.

        **No floor.** A dead world's natural capacity is genuinely zero, because
        nobody lives outdoors on Mars. What holds an outpost up there is the
        habitats somebody built, and that belongs in
        :func:`galaxysim.colony.population.habitat_capacity` where it is a fact
        about the colony rather than about the planet. A floor here would quietly
        hand a barren rock a natural capacity of tens of millions and make
        terraforming pointless.
        """
        return self.land_area_km2 * SUSTAINABLE_DENSITY_PER_KM2 * max(0.0, self.habitability)

    # --- the conclusion -----------------------------------------------------

    def habitability_reasons(self) -> list[str]:
        """Why habitability came out where it did, in plain language.

        The point of deriving rather than rolling: the number can always
        explain itself.
        """
        reasons: list[str] = []
        if self.atmosphere.is_breathable:
            reasons.append("breathable air")
        elif self.atmosphere.pressure_bar <= 0.001:
            reasons.append("no atmosphere")
        else:
            toxins = self.atmosphere.toxins()
            reasons.append(f"toxic atmosphere ({', '.join(toxins)})" if toxins else "unbreathable air")

        if self.hydrosphere.liquid_water:
            reasons.append("liquid water")
        elif self.hydrosphere.ice_fraction > 0.05:
            reasons.append("water only as ice")
        else:
            reasons.append("no surface water")

        gravity = self.body.gravity_g
        if GRAVITY_COMFORT[0] <= gravity <= GRAVITY_COMFORT[1]:
            reasons.append(f"{gravity:.2f} g")
        else:
            reasons.append(f"{gravity:.2f} g — {'crushing' if gravity > 1 else 'debilitating'}")

        reasons.append("shielded" if self.body.is_shielded else "unshielded — surface radiation")

        temp = self.climate.surface_temp_k
        if not (TEMP_SURVIVABLE[0] <= temp <= TEMP_SURVIVABLE[1]):
            reasons.append(f"{temp - WATER_FREEZE_K:.0f} °C")

        if self.biosphere.exists:
            reasons.append(
                "compatible biosphere" if self.biosphere.is_compatible else "alien biochemistry"
            )
        return reasons


def _band_score(value: float, comfort: tuple[float, float], survivable: tuple[float, float]) -> float:
    """1.0 inside the comfort band, tapering to 0 at the survivable limits."""
    low_c, high_c = comfort
    low_s, high_s = survivable
    if low_c <= value <= high_c:
        return 1.0
    if value < low_c:
        return max(0.0, (value - low_s) / (low_c - low_s)) if low_c > low_s else 0.0
    return max(0.0, (high_s - value) / (high_s - high_c)) if high_s > high_c else 0.0


def derive_habitability(
    atmosphere: Atmosphere,
    climate: Climate,
    body: Body,
    hydrosphere: Hydrosphere,
    biosphere: Biosphere,
) -> float:
    """Habitability as a conclusion drawn from the physical state.

    Deliberately multiplicative on the things that are non-negotiable: a world
    with no atmosphere cannot be scored up by being otherwise pleasant. That is
    what stops the number drifting away from what the survey plainly says.
    """
    if atmosphere.pressure_bar <= 0.001:
        return 0.0

    temperature = _band_score(climate.surface_temp_k, TEMP_COMFORT, TEMP_SURVIVABLE)
    gravity = _band_score(body.gravity_g, GRAVITY_COMFORT, GRAVITY_SURVIVABLE)
    if temperature <= 0.0 or gravity <= 0.0:
        return 0.0

    if atmosphere.is_breathable:
        air = 1.0
    else:
        # Unbreathable but present air still helps: it holds pressure and heat,
        # and a sealed habitat has something to work with.
        air = 0.25 if not atmosphere.toxins() else 0.12
        if not (0.1 <= atmosphere.pressure_bar <= 10.0):
            air *= 0.4

    water = 1.0 if hydrosphere.liquid_water else (0.5 if hydrosphere.ice_fraction > 0.05 else 0.25)
    shielding = 1.0 if body.is_shielded else 0.55
    stability = max(0.4, 1.0 - climate.seasonal_swing_k / 150.0)

    life = 1.0
    if biosphere.exists:
        # A compatible biosphere is food and breathable air; an incompatible one
        # is a biohazard you must seal against.
        life = 1.25 if biosphere.is_compatible else 0.8
        life *= 1.0 - 0.35 * biosphere.pathogen_hazard

    score = temperature * gravity * air * water * shielding * stability * life
    return round(max(0.0, min(1.0, score)), 4)


def classify(body: Body, atmosphere: Atmosphere, climate: Climate, hydro: Hydrosphere) -> str:
    """A short label for what kind of world this is.

    Descriptive only -- assigned from the physics after the fact, never used to
    decide it. This is the inversion from the old hand-authored type table.
    """
    if body.mass_earth >= 8.0 and atmosphere.composition.get("H2", 0.0) > 0.2:
        return "gas giant"
    if body.mass_earth >= 3.0:
        prefix = "super-earth"
    elif body.mass_earth < 0.05:
        prefix = "planetoid"
    elif body.mass_earth < 0.3:
        prefix = "small rocky world"
    else:
        prefix = "terrestrial"

    if atmosphere.pressure_bar <= 0.001:
        return f"airless {prefix}"
    if climate.surface_temp_k > 450:
        return f"infernal {prefix}"
    if hydro.ocean_fraction > 0.9:
        return "ocean world"
    if climate.surface_temp_k < WATER_FREEZE_K - 40:
        return f"frozen {prefix}"
    if hydro.liquid_water and atmosphere.is_breathable:
        return f"garden {prefix}"
    if hydro.ice_fraction > 0.4:
        return f"glacial {prefix}"
    if not hydro.liquid_water and hydro.ice_fraction < 0.05:
        return f"desert {prefix}"
    return prefix


def survey_world(
    rng: random.Random,
    star: Star,
    distance_au: float,
    mass_earth: float,
) -> Survey:
    """Generate one complete world at a given orbit. The pipeline."""
    beyond_frost = distance_au > star.frost_line_au

    orbit = roll_orbit(rng, star, distance_au, star.age_gyr)
    body = roll_body(rng, mass_earth, beyond_frost, star.age_gyr)

    # Rolled before the atmosphere: whether the world has water decides whether
    # its climate has a thermostat, which is the difference between an Earth
    # and a Venus.
    water = roll_water_endowment(rng, beyond_frost)

    # First pass: what the world holds and how warm it is, with no life.
    atmosphere, climate = build_atmosphere(
        rng, star, body, orbit, beyond_frost, oxygenated=False, water_endowment=water
    )
    hydrosphere = build_hydrosphere(rng, body, atmosphere, climate, beyond_frost, water)

    # Life, judged against the abiotic world it would have had to start on.
    biosphere = roll_biosphere(
        rng,
        surface_temp_k=climate.surface_temp_k,
        seasonal_swing_k=climate.seasonal_swing_k,
        has_liquid_water=hydrosphere.liquid_water,
        ice_fraction=hydrosphere.ice_fraction,
        pressure_bar=atmosphere.pressure_bar,
        star_age_gyr=star.age_gyr,
        is_shielded=body.is_shielded,
    )

    # Second pass: if photosynthesis has been running, the air is different now
    # and so is the temperature. This is the causal arrow that makes free oxygen
    # mean something.
    if biosphere.oxygenating:
        oxygenated_atmosphere, oxygenated_climate = build_atmosphere(
            rng, star, body, orbit, beyond_frost, oxygenated=True, water_endowment=water
        )
        oxygenated_hydrosphere = build_hydrosphere(
            rng, body, oxygenated_atmosphere, oxygenated_climate, beyond_frost, water
        )
        # Oxygenation changes the greenhouse and therefore the temperature, and
        # the new climate has to still support the life that caused it. If it
        # does not, the biosphere never got that far -- keep the abiotic world
        # rather than leave a contradiction on the books.
        if viable_biochemistry(oxygenated_climate.surface_temp_k) == biosphere.biochemistry:
            atmosphere = oxygenated_atmosphere
            climate = oxygenated_climate
            hydrosphere = oxygenated_hydrosphere
        else:
            biosphere = replace(biosphere, oxygenating=False)

    deposits = roll_geology(
        rng,
        metallicity=star.metallicity,
        beyond_frost_line=beyond_frost,
        tectonic_activity=body.tectonic_activity,
        mass_earth=body.mass_earth,
        age_gyr=star.age_gyr,
        has_atmosphere=atmosphere.pressure_bar > 0.001,
    )
    add_ice_deposits(rng, deposits, hydrosphere.ice_fraction, hydrosphere.ocean_fraction)

    moons = _roll_moons(rng, body.mass_earth)
    has_rings = body.mass_earth > 5.0 and rng.random() < 0.35

    habitability = derive_habitability(atmosphere, climate, body, hydrosphere, biosphere)

    return Survey(
        star=star,
        orbit=orbit,
        body=body,
        atmosphere=atmosphere,
        climate=climate,
        hydrosphere=hydrosphere,
        biosphere=biosphere,
        deposits=deposits,
        moons=moons,
        has_rings=has_rings,
        world_class=classify(body, atmosphere, climate, hydrosphere),
        habitability=habitability,
    )


def _roll_moons(rng: random.Random, mass_earth: float) -> int:
    """Bigger worlds capture and retain more satellites."""
    if mass_earth < 0.1:
        return 0
    expected = math.log10(1.0 + mass_earth * 10.0) * 1.6
    return max(0, int(rng.gauss(expected, expected * 0.6)))


def plausible_orbits(rng: random.Random, star: Star, count: int) -> list[float]:
    """Semi-major axes for a system's planets, in AU.

    Spaced geometrically rather than uniformly, because real systems obey an
    approximate Titius-Bode progression -- each planet sits roughly a fixed
    ratio further out than the last. Inner edge scales with luminosity, since a
    bright star clears its neighbourhood further out.
    """
    inner = 0.05 * math.sqrt(max(star.luminosity_solar, 0.001)) * rng.uniform(0.7, 1.6)
    inner = max(0.02, inner)
    ratio = rng.uniform(1.35, 2.0)

    orbits: list[float] = []
    distance = inner
    for _ in range(count):
        orbits.append(round(distance * rng.uniform(0.9, 1.1), 4))
        distance *= ratio
    return orbits


def plausible_mass(rng: random.Random, distance_au: float, star: Star) -> float:
    """A plausible planet mass at this distance, in Earth masses.

    Giants form beyond the frost line where there was ice to accrete; the inner
    system produces rocky worlds. That single fact explains the architecture of
    most real systems.
    """
    if distance_au > star.frost_line_au and rng.random() < 0.45:
        return rng.uniform(8.0, 320.0)  # ice giant to gas giant
    roll = rng.random()
    if roll < 0.35:
        return rng.uniform(0.005, 0.1)  # planetoid or large moon
    if roll < 0.8:
        return rng.uniform(0.1, 2.0)  # rocky
    return rng.uniform(2.0, 8.0)  # super-earth
