"""Feeding people, and why some worlds are so much better at it.

Food is the second thing a population needs after air, and unlike air it cannot
be shipped indefinitely at scale -- a billion people eat eighty thousand tonnes
a day, which no freighter fleet is going to cover. So agriculture is a place
question, and this module is what makes it one.

Three regimes, and a world falls into exactly one:

**Open farmland.** Liquid water, a temperature people can stand, and a
*compatible* biosphere -- carbon-water chemistry you can actually eat. Soil
exists, it already has an ecology in it, and crops grow in the ground. Cheap,
and the reason a garden world is worth fighting over beyond its habitability
score.

**Sealed agriculture.** Liquid water but nothing edible native to the place, or
a biochemistry that would poison you. Everything grows under glass in soil you
made. Workable, several times less productive per farmer.

**Hydroponics.** No water, no air, no soil, no help. Every calorie is
manufactured, and it wants fertiliser and water shipped or refined on site. This
is what a mining outpost eats, and it is why the richest worlds in the game are
also the ones that cannot feed themselves.

Gravity gets a modest say in all three: crops and the people tending them both
struggle well outside one gee, and a world at half a gee or two gees is a worse
farm than one at Earth's.
"""

from __future__ import annotations

from galaxysim.worldgen.biosphere import COMPLEX, INTELLIGENT, SIMPLE

#: Yield multipliers by regime, relative to open farmland on a compatible world.
OPEN_FARMLAND = 1.0
SEALED_AGRICULTURE = 0.35
HYDROPONICS = 0.12

#: Temperature band, in kelvin, within which crops grow without heroic effort.
#: Wider than the human comfort range: a greenhouse handles the edges.
CROP_TEMPERATURE_RANGE = (255.0, 320.0)

#: Surface gravity, in gees, that farming is calibrated to. Real crops are
#: sensitive to it in both directions -- water does not drain in low gravity and
#: stems cannot hold themselves up in high.
IDEAL_GRAVITY_G = 1.0
GRAVITY_TOLERANCE_G = 0.75

#: A mature native ecology is worth more than a young one: more topsoil, more
#: pollinators, more of the boring machinery that makes agriculture cheap.
ESTABLISHED_STAGES: frozenset[str] = frozenset({SIMPLE, COMPLEX, INTELLIGENT})
ESTABLISHED_BONUS = 0.25


def regime(survey) -> str:
    """Which of the three ways this world grows food."""
    if survey.hydrosphere.liquid_water and survey.biosphere.is_compatible:
        return "open farmland"
    if survey.hydrosphere.liquid_water:
        return "sealed agriculture"
    return "hydroponics"


def _base_yield(survey) -> float:
    return {
        "open farmland": OPEN_FARMLAND,
        "sealed agriculture": SEALED_AGRICULTURE,
        "hydroponics": HYDROPONICS,
    }[regime(survey)]


def _temperature_factor(survey) -> float:
    """How far outside the growing band this world sits.

    Applies to open farmland only in spirit, but kept general: a sealed farm on
    a world at 700 K is still fighting its environment through the walls.
    """
    low, high = CROP_TEMPERATURE_RANGE
    temperature = survey.climate.surface_temp_k
    if low <= temperature <= high:
        return 1.0
    excess = (low - temperature) if temperature < low else (temperature - high)
    # Falls off over roughly a hundred kelvin, and never quite to zero -- you can
    # always grow *something* indoors, just not much of it.
    return max(0.25, 1.0 - excess / 150.0)


def _gravity_factor(survey) -> float:
    offset = abs(survey.body.gravity_g - IDEAL_GRAVITY_G)
    if offset <= 0.25:
        return 1.0
    return max(0.4, 1.0 - (offset - 0.25) / GRAVITY_TOLERANCE_G * 0.6)


def quality(survey) -> float:
    """How productive a farmer is on this world, relative to Earth's best.

    Multiplicative, like habitability, and for the same reason: these are not
    independent bonuses to be added up, they are separate ways for a place to be
    a bad farm, and being bad in two ways at once should compound.
    """
    value = _base_yield(survey) * _temperature_factor(survey) * _gravity_factor(survey)
    if survey.biosphere.is_compatible and survey.biosphere.stage in ESTABLISHED_STAGES:
        value *= 1.0 + ESTABLISHED_BONUS
    return round(value, 4)


def reasons(survey) -> list[str]:
    """Why the number came out where it did, in plain language.

    Same contract as :meth:`Survey.habitability_reasons`: a derived figure that
    cannot explain itself is a figure nobody trusts.
    """
    notes = [regime(survey)]
    if survey.biosphere.is_compatible:
        if survey.biosphere.stage in ESTABLISHED_STAGES:
            notes.append("established compatible ecology")
        else:
            notes.append("compatible but primitive biosphere")
    elif survey.biosphere.exists:
        notes.append(f"native life is {survey.biosphere.biochemistry}, inedible")

    if _temperature_factor(survey) < 1.0:
        notes.append(f"{survey.climate.surface_temp_k:.0f} K is outside the growing band")
    if _gravity_factor(survey) < 1.0:
        notes.append(f"{survey.body.gravity_g:.2f} g is hard on crops")
    return notes
