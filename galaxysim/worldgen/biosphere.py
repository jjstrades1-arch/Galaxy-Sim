"""Life, where the conditions allow it and there has been time.

Nothing here is rolled independently of the world. A biosphere needs a solvent,
a temperature band that stays in range, and — the constraint people forget —
**time**. Life took roughly a billion years to appear on Earth and another three
to get past single cells. A 0.4 Gyr system has not had the opportunity, however
pleasant it looks.

The payoff of doing it this way is that a complex biosphere is genuinely rare
and genuinely earned, and its presence explains other things about the world.
Free oxygen in an atmosphere is not a coincidence: oxygen is so reactive that
without something continually replenishing it, it vanishes. **An oxygen-rich
atmosphere is a biosignature**, and in this model that causality runs the right
way round — life produces the oxygen, rather than the oxygen being rolled and
life being inferred.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

STERILE = "sterile"
PREBIOTIC = "prebiotic"
MICROBIAL = "microbial"
SIMPLE = "simple"
COMPLEX = "complex"
INTELLIGENT = "intelligent"

#: In order. Each stage needs the one before it, plus time.
STAGES: tuple[str, ...] = (STERILE, PREBIOTIC, MICROBIAL, SIMPLE, COMPLEX, INTELLIGENT)

#: Roughly how long each transition took on Earth, in billions of years.
#: Abiogenesis was fast; getting past single cells took most of the planet's
#: history. These are the real bottlenecks and they shape how common each
#: stage is across a galaxy.
STAGE_TIME_GYR: dict[str, float] = {
    PREBIOTIC: 0.2,
    MICROBIAL: 1.0,
    SIMPLE: 2.8,
    COMPLEX: 4.0,
    INTELLIGENT: 4.5,
}

#: Solvent chemistries, with the temperature band each can work in.
CARBON_WATER = "carbon-water"
AMMONIA = "ammonia-based"
METHANE = "methane-cryogenic"
SILICATE = "silicate-thermophile"

BIOCHEMISTRY_RANGE: dict[str, tuple[float, float]] = {
    CARBON_WATER: (250.0, 395.0),
    AMMONIA: (170.0, 250.0),
    METHANE: (80.0, 130.0),
    SILICATE: (395.0, 600.0),
}

#: Which chemistries a human-like species can eat, breathe around, and share a
#: world with. Everything else is a laboratory, not a farm.
COMPATIBLE_BIOCHEMISTRY: frozenset[str] = frozenset({CARBON_WATER})


@dataclass(frozen=True, slots=True)
class Biosphere:
    """A world's life, if it has any."""

    stage: str
    biochemistry: str | None
    #: Total living mass in tonnes. Earth is ~2e12 t.
    biomass_tonnes: float
    #: 0-1. How dangerous the local microbiology is to an unadapted coloniser.
    pathogen_hazard: float
    #: True when photosynthesis has been running long enough to oxygenate the
    #: air. This is what puts free O2 into the atmosphere.
    oxygenating: bool

    @property
    def exists(self) -> bool:
        return self.stage not in (STERILE, PREBIOTIC)

    @property
    def is_compatible(self) -> bool:
        """Whether colonists could eat and metabolise anything here."""
        return self.biochemistry in COMPATIBLE_BIOCHEMISTRY

    @property
    def hazard_label(self) -> str:
        if self.pathogen_hazard >= 0.6:
            return "SEVERE"
        if self.pathogen_hazard >= 0.3:
            return "MODERATE"
        if self.pathogen_hazard > 0.0:
            return "low"
        return "none"


def viable_biochemistry(surface_temp_k: float) -> str | None:
    """Which solvent chemistry, if any, could work at this temperature."""
    for chemistry, (low, high) in BIOCHEMISTRY_RANGE.items():
        if low <= surface_temp_k <= high:
            return chemistry
    return None


def roll_biosphere(
    rng: random.Random,
    *,
    surface_temp_k: float,
    seasonal_swing_k: float,
    has_liquid_water: bool,
    ice_fraction: float,
    pressure_bar: float,
    star_age_gyr: float,
    is_shielded: bool,
) -> Biosphere:
    """Decide whether life arose here, and how far it got.

    Called *before* the final atmosphere pass, because whether the world is
    oxygenated is an output of this function rather than an input to it.
    """
    sterile = Biosphere(STERILE, None, 0.0, 0.0, False)

    chemistry = viable_biochemistry(surface_temp_k)
    if chemistry is None or pressure_bar < 0.01:
        return sterile

    # Carbon-water life needs actual liquid water, not merely a temperature
    # that would permit it. Exotic chemistries use their own solvent.
    if chemistry == CARBON_WATER and not (has_liquid_water or ice_fraction > 0.2):
        return sterile

    # A world whose temperature swings wildly across the seasons never holds
    # still long enough for biochemistry to establish.
    if seasonal_swing_k > 80.0:
        return sterile

    # Unshielded worlds are sterilised at the surface by stellar radiation.
    # Life can still arise in the oceans, so this is a penalty, not a veto.
    radiation_penalty = 1.0 if is_shielded else 0.25

    # How favourable conditions are, which sets how fast life progresses
    # relative to Earth's timeline.
    ideal_temp = sum(BIOCHEMISTRY_RANGE[chemistry]) / 2.0
    band = BIOCHEMISTRY_RANGE[chemistry][1] - BIOCHEMISTRY_RANGE[chemistry][0]
    temp_quality = max(0.0, 1.0 - abs(surface_temp_k - ideal_temp) / (band * 0.6))
    stability = max(0.0, 1.0 - seasonal_swing_k / 80.0)
    quality = temp_quality * stability * radiation_penalty
    if quality <= 0.02:
        return sterile

    # Effective time available: a favourable world runs the clock faster than
    # a marginal one.
    effective_gyr = star_age_gyr * quality * rng.uniform(0.6, 1.5)

    stage = STERILE
    for candidate in (PREBIOTIC, MICROBIAL, SIMPLE, COMPLEX, INTELLIGENT):
        if effective_gyr < STAGE_TIME_GYR[candidate]:
            break
        # Even with time, each step is a genuine hurdle. Intelligence
        # especially -- Earth managed it exactly once in four billion years.
        odds = 0.9 if candidate in (PREBIOTIC, MICROBIAL) else 0.45
        if candidate == INTELLIGENT:
            odds = 0.04
        if rng.random() > odds:
            break
        stage = candidate

    if stage in (STERILE, PREBIOTIC):
        return Biosphere(stage, chemistry if stage == PREBIOTIC else None, 0.0, 0.0, False)

    # Biomass scales with how much of the world is habitable and how developed
    # life is. Earth is the reference at ~2e12 tonnes.
    stage_scale = {MICROBIAL: 0.05, SIMPLE: 0.4, COMPLEX: 1.0, INTELLIGENT: 1.1}[stage]
    biomass = 2.0e12 * stage_scale * quality * rng.uniform(0.3, 2.2)

    # More complex ecosystems carry more, and nastier, pathogens.
    hazard = {MICROBIAL: 0.25, SIMPLE: 0.4, COMPLEX: 0.55, INTELLIGENT: 0.6}[stage]
    hazard *= rng.uniform(0.4, 1.5)
    # Life that does not share our chemistry mostly cannot infect us.
    if chemistry not in COMPATIBLE_BIOCHEMISTRY:
        hazard *= 0.15

    # Photosynthesis: oxygenation needs a water-based biosphere that has been
    # running for a long time. Earth's own Great Oxidation Event came roughly
    # two billion years after life began.
    oxygenating = (
        chemistry == CARBON_WATER
        and stage in (SIMPLE, COMPLEX, INTELLIGENT)
        and effective_gyr > 2.0
    )

    return Biosphere(
        stage=stage,
        biochemistry=chemistry,
        biomass_tonnes=round(biomass, 1),
        pathogen_hazard=round(min(1.0, hazard), 4),
        oxygenating=oxygenating,
    )


def describe(biosphere: Biosphere) -> str:
    """A line of survey prose about what lives here."""
    if biosphere.stage == STERILE:
        return "No indication of life, past or present."
    if biosphere.stage == PREBIOTIC:
        return "Complex organic chemistry present; no self-replication detected."
    if biosphere.stage == MICROBIAL:
        return "Single-celled life throughout the available solvent."
    if biosphere.stage == SIMPLE:
        return "Multicellular ecosystems, none of them mobile at scale."
    if biosphere.stage == COMPLEX:
        return "A full ecosystem with mobile macrofauna and layered food webs."
    return "Tool use, structures, and signals in the radio spectrum."


def oxygen_note(biosphere: Biosphere) -> str | None:
    """The observation that ties the biosphere to the atmosphere."""
    if not biosphere.oxygenating:
        return None
    return "An oxygen atmosphere this rich is biological in origin."
