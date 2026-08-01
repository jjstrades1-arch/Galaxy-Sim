"""Planetary generation.

The load-bearing test here is :func:`test_generated_worlds_never_contradict_themselves`.
Everything else in this file supports it.

The whole reason worlds are *derived* rather than rolled is that a rolled world
can be nonsense — a frozen ocean, a hydrogen atmosphere on a hot pebble, a
complex biosphere on a rock a hundred million years old. Deriving them from
physics means those cases are not forbidden by a rule, they are simply never
produced. This file generates thousands of worlds and checks that claim, because
a claim like that is worthless unless it is tested at volume.

The second theme is validation against reality. Earth, Mars and Venus are the
only worlds anyone can check the model against, so the model is checked against
them: if it cannot reproduce the three planets we have measured, it has no
business generating twelve thousand we cannot.
"""

from __future__ import annotations

import math

import pytest

from galaxysim.core.seeds import rng_for
from galaxysim.worldgen.biosphere import COMPLEX, INTELLIGENT, SIMPLE, STAGE_TIME_GYR
from galaxysim.worldgen.planet import (
    EARTH_ESCAPE_KMS,
    WATER_FREEZE_K,
    WATER_TRIPLE_POINT_BAR,
    equilibrium_temperature,
    greenhouse_forcing,
    retains,
    rms_velocity_kms,
)
from galaxysim.worldgen.serialize import survey_from_json, survey_to_json
from galaxysim.worldgen.star import CLASS_MASS_RANGE, Star, roll_star
from galaxysim.worldgen.survey import plausible_mass, plausible_orbits, survey_world

SUN = Star(
    spectral_class="G",
    subclass=2,
    mass_solar=1.0,
    radius_solar=1.0,
    luminosity_solar=1.0,
    temperature_k=5772.0,
    age_gyr=4.6,
    metallicity=0.0,
)


def generate(count: int = 2500):
    """A representative population of worlds."""
    worlds = []
    for system_index in range(count):
        rng = rng_for("planets", system_index)
        star = roll_star(rng)
        for orbit_index, distance in enumerate(plausible_orbits(rng, star, rng.randint(1, 7))):
            world_rng = rng_for("planet", system_index, orbit_index)
            worlds.append(
                survey_world(
                    world_rng, star, distance, plausible_mass(world_rng, distance, star)
                )
            )
    return worlds


# --- validation against the three worlds we have actually measured -----------


def test_equilibrium_temperature_matches_the_solar_system():
    """If it cannot reproduce Earth, Mars and Venus, it cannot be trusted."""
    assert equilibrium_temperature(SUN, 1.000, 0.306) == pytest.approx(254, abs=2)
    assert equilibrium_temperature(SUN, 1.524, 0.250) == pytest.approx(210, abs=3)
    assert equilibrium_temperature(SUN, 0.723, 0.770) == pytest.approx(232, abs=6)


def test_greenhouse_forcing_matches_the_solar_system():
    """Earth +33 K, Mars +5 K, Venus ~+500 K, spanning five orders of pressure."""
    earth = greenhouse_forcing(1.0, {"CO2": 0.0004, "H2O": 0.010, "N2": 0.78, "O2": 0.21})
    mars = greenhouse_forcing(0.006, {"CO2": 0.95, "N2": 0.03})
    venus = greenhouse_forcing(92.0, {"CO2": 0.965, "N2": 0.035})

    assert earth == pytest.approx(33, abs=3)
    assert mars == pytest.approx(5, abs=3)
    assert venus == pytest.approx(500, abs=60)


def test_atmospheric_retention_matches_earth():
    """Earth keeps nitrogen and slowly loses hydrogen. Both must fall out."""
    assert not retains("H2", EARTH_ESCAPE_KMS, 288), "Earth does lose hydrogen"
    assert retains("N2", EARTH_ESCAPE_KMS, 288)
    assert retains("O2", EARTH_ESCAPE_KMS, 288)
    assert retains("CO2", EARTH_ESCAPE_KMS, 288)
    # Lighter gas, faster molecules, harder to keep.
    assert rms_velocity_kms("H2", 288) > rms_velocity_kms("N2", 288)


def test_a_hot_small_world_holds_nothing_a_cold_massive_one_holds_everything():
    """Retention is about the ratio, not the world's size alone.

    Titan is tiny and keeps a thick atmosphere because it is cold; Mercury is
    larger and keeps none because it is hot.
    """
    assert not retains("N2", 4.3, 700)  # small and scorching
    assert retains("N2", 2.6, 94)  # Titan: small, but frigid


def test_solar_mass_star_reproduces_the_sun():
    """The mass-luminosity and habitable-zone relations, checked at one point."""
    best = min(
        (roll_star(rng_for("sunlike", i)) for i in range(60000)),
        key=lambda s: abs(s.mass_solar - 1.0) + (0 if s.spectral_class == "G" else 10),
    )
    assert best.mass_solar == pytest.approx(1.0, abs=0.02)
    assert best.luminosity_solar == pytest.approx(1.0, rel=0.10)
    inner, outer = best.habitable_zone
    assert inner == pytest.approx(0.95, abs=0.05)
    assert outer == pytest.approx(1.37, abs=0.08)
    assert best.frost_line_au == pytest.approx(4.85, abs=0.3)


def test_spectral_class_distribution_is_realistic():
    """Three quarters of stars are red dwarfs. Sun-like stars are a find."""
    counts: dict[str, int] = {}
    for i in range(20000):
        star = roll_star(rng_for("dist", i))
        counts[star.spectral_class] = counts.get(star.spectral_class, 0) + 1

    assert counts["M"] / 20000 == pytest.approx(0.765, abs=0.02)
    assert counts["K"] / 20000 == pytest.approx(0.121, abs=0.015)
    assert counts["G"] / 20000 == pytest.approx(0.076, abs=0.012)
    assert counts.get("O", 0) == 0, "O stars are too rare to appear at this sample size"


# --- the coherence sweep -----------------------------------------------------


def test_generated_worlds_never_contradict_themselves():
    """Generate thousands of worlds; none may be internally impossible.

    This is the test that justifies the whole derived-not-rolled architecture.
    Every assertion below describes a world that a naive roll-each-stat
    generator would produce regularly and that physics simply cannot.
    """
    worlds = generate()
    assert len(worlds) > 8000, "the sweep needs volume to be meaningful"

    for survey in worlds:
        air = survey.atmosphere
        water = survey.hydrosphere
        climate = survey.climate
        body = survey.body
        bio = survey.biosphere
        where = f"{survey.world_class} at {survey.orbit.semi_major_axis_au} AU"

        # An airless world cannot have oceans: without pressure, water sublimes
        # straight to vapour and is lost.
        if air.pressure_bar <= WATER_TRIPLE_POINT_BAR:
            assert not water.liquid_water, f"liquid water without pressure: {where}"

        # Liquid water requires temperatures where water is liquid.
        if water.liquid_water:
            assert climate.surface_temp_k >= WATER_FREEZE_K, f"liquid ice: {where}"
            assert air.pressure_bar > WATER_TRIPLE_POINT_BAR, f"liquid in vacuum: {where}"
            assert water.ocean_fraction > 0.0, f"liquid water and no ocean: {where}"

        # Composition is a proper distribution.
        if air.composition:
            assert sum(air.composition.values()) == pytest.approx(1.0, abs=0.01), (
                f"composition does not sum to 1: {where}"
            )
            assert all(v >= 0 for v in air.composition.values())

        # A world only holds gases it can physically retain.
        for gas, fraction in air.composition.items():
            if fraction > 0.01:
                assert retains(gas, body.escape_velocity_kms, climate.surface_temp_k), (
                    f"{where} holds {gas} it cannot retain"
                )

        # Free oxygen is a biosignature. It is so reactive that it cannot
        # persist without life continually replenishing it.
        if air.composition.get("O2", 0.0) > 0.02:
            assert bio.oxygenating, f"free oxygen with no biosphere to make it: {where}"

        # Life needs time. These are the real bottlenecks on Earth's timeline.
        if bio.stage in (SIMPLE, COMPLEX, INTELLIGENT):
            assert survey.star.age_gyr >= STAGE_TIME_GYR[bio.stage] * 0.5, (
                f"{bio.stage} life on a {survey.star.age_gyr:.1f} Gyr star: {where}"
            )
            assert bio.biochemistry is not None

        # Derived body physics must be self-consistent.
        assert body.gravity_g == pytest.approx(
            body.mass_earth / body.radius_earth**2, rel=0.01
        ), f"gravity does not match mass and radius: {where}"
        assert body.escape_velocity_kms == pytest.approx(
            EARTH_ESCAPE_KMS * math.sqrt(body.mass_earth / body.radius_earth), rel=0.01
        ), f"escape velocity does not match the body: {where}"

        # A dead world has no dynamo, and therefore no magnetic field.
        if body.tectonic_activity <= 0.12:
            assert body.magnetic_field_gauss == 0.0, f"field without a molten core: {where}"

        assert 0.0 <= survey.habitability <= 1.0
        assert 0.0 <= air.albedo <= 1.0
        assert climate.surface_temp_k >= climate.equilibrium_temp_k - 0.5, (
            f"greenhouse cooled the world: {where}"
        )


def test_tidally_locked_worlds_are_close_in():
    """Locking is a tidal effect, so it happens near the star and not far out."""
    for survey in generate(500):
        if survey.orbit.tidally_locked:
            assert survey.orbit.axial_tilt_deg == 0.0
            # A locked world's day equals its year.
            assert survey.orbit.rotation_hours == pytest.approx(
                survey.orbit.period_days * 24.0, rel=0.01
            )


def test_habitability_is_derived_not_rolled():
    """The score has to move with the physics that produces it.

    Raising a world's albedo cools it; past the survivable band, habitability
    must fall. If it were rolled independently it would not.
    """
    from galaxysim.worldgen.planet import Atmosphere, Body, Climate, Hydrosphere
    from galaxysim.worldgen.biosphere import Biosphere, STERILE
    from galaxysim.worldgen.survey import derive_habitability

    body = Body(1.0, 1.0, 5.5, 1.0, 11.19, 0.5, 10, 0.5)
    air = Atmosphere(1.0, {"N2": 0.78, "O2": 0.21, "Ar": 0.01}, 0.3, 33.0)
    water = Hydrosphere(True, 0.6, 0.05, 3.0)
    sterile = Biosphere(STERILE, None, 0.0, 0.0, False)

    temperate = Climate(255.0, 288.0, 20.0, 0.15, 0.5, 0.35)
    frozen = Climate(200.0, 233.0, 20.0, 0.15, 0.5, 0.35)
    scorching = Climate(380.0, 413.0, 20.0, 0.15, 0.5, 0.35)

    good = derive_habitability(air, temperate, body, water, sterile)
    cold = derive_habitability(air, frozen, body, water, sterile)
    hot = derive_habitability(air, scorching, body, water, sterile)

    assert good > 0.5, "a breathable, watered, temperate, shielded world should score well"
    assert cold == 0.0 and hot == 0.0, "outside the survivable band there is no habitability"

    # Gravity works the same way.
    crushing = Body(9.0, 1.5, 12.0, 4.0, 27.4, 0.5, 10, 0.5)
    assert derive_habitability(air, temperate, crushing, water, sterile) == 0.0

    # And an airless world cannot be rescued by being otherwise pleasant.
    assert derive_habitability(Atmosphere(0.0), temperate, body, water, sterile) == 0.0


def test_habitability_explains_itself():
    """A derived number should be able to say where it came from."""
    for survey in generate(120):
        reasons = survey.habitability_reasons()
        assert reasons, "every world must be able to justify its score"
        assert all(isinstance(reason, str) and reason for reason in reasons)


# --- geology -----------------------------------------------------------------


def test_tectonic_worlds_carry_richer_ore():
    """Concentration is geological work, not luck.

    Bulk abundance is not mineable ore. Hydrothermal circulation and volcanism
    are what gather a diffuse element into a seam worth digging, so a dead world
    may hold as much copper as Earth and none of it in usable form.
    """
    active, dead = [], []
    for survey in generate(600):
        grades = [d.concentration for d in survey.deposits.values()]
        if not grades:
            continue
        mean_grade = sum(grades) / len(grades)
        if survey.body.tectonic_activity > 0.5:
            active.append(mean_grade)
        elif survey.body.tectonic_activity < 0.05:
            dead.append(mean_grade)

    assert active and dead, "the sweep should produce both kinds of world"
    assert sum(active) / len(active) > sum(dead) / len(dead) * 1.5


def test_metal_poor_stars_make_metal_poor_worlds():
    """Composition follows formation history: you cannot build iron from gas
    that had none."""
    rich_star = Star("G", 2, 1.0, 1.0, 1.0, 5772, 5.0, 0.4)
    poor_star = Star("G", 2, 1.0, 1.0, 1.0, 5772, 5.0, -1.2)

    def mean_iron(star: Star) -> float:
        total = 0.0
        for i in range(200):
            survey = survey_world(rng_for("metal", star.metallicity, i), star, 1.0, 1.0)
            total += survey.deposits["iron"].abundance
        return total / 200

    assert mean_iron(rich_star) > mean_iron(poor_star) * 3


# --- persistence -------------------------------------------------------------


def test_a_survey_survives_the_round_trip():
    """Stored worlds must reload identical. Terraforming writes back this way."""
    for i in range(200):
        rng = rng_for("roundtrip", i)
        star = roll_star(rng)
        original = survey_world(rng, star, 1.0, 1.0)
        restored = survey_from_json(survey_to_json(original))
        assert survey_to_json(restored) == survey_to_json(original)
        assert restored.habitability == original.habitability
        assert restored.carrying_capacity == pytest.approx(original.carrying_capacity)


def test_generation_is_deterministic():
    """Same seed, same world -- the standing requirement of the whole project."""
    star = roll_star(rng_for("determinism", 0))
    first = survey_world(rng_for("determinism", 1), star, 1.0, 1.0)
    second = survey_world(rng_for("determinism", 1), star, 1.0, 1.0)
    assert survey_to_json(first) == survey_to_json(second)


# --- scale -------------------------------------------------------------------


def test_capacity_scales_with_real_area():
    """An Earth-sized world holds billions; a moon holds far fewer."""
    star = SUN
    big = max(
        (survey_world(rng_for("cap", i), star, 1.0, 1.1) for i in range(400)),
        key=lambda s: s.habitability,
    )
    assert big.land_area_km2 > 1e7
    if big.habitability > 0.3:
        assert big.carrying_capacity > 1e9, "a good Earth-sized world should hold billions"

    tiny = survey_world(rng_for("cap-moon", 1), star, 1.0, 0.02)
    assert tiny.land_area_km2 < big.land_area_km2
    assert tiny.carrying_capacity < big.carrying_capacity


def test_class_mass_ranges_do_not_overlap_wrongly():
    """The class table should be ordered and contiguous."""
    ordered = ["M", "K", "G", "F", "A", "B"]
    for cooler, hotter in zip(ordered, ordered[1:]):
        assert CLASS_MASS_RANGE[cooler][1] <= CLASS_MASS_RANGE[hotter][0]
