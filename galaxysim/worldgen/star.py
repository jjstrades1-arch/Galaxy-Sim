"""Stars, generated from real astrophysics.

Everything here follows from a single roll -- the star's mass -- plus its age and
metallicity. Luminosity, radius, temperature, colour, habitable zone and frost
line are all *derived*, so a star never contradicts itself and a player who
knows a little astronomy will find the numbers behave the way they should.

Three things a star decides for the whole system, which is why this module comes
first:

* **Luminosity** sets where the habitable zone is, how much flux every planet
  receives, and therefore every planet's temperature.
* **Age** gates biospheres. Life needs time; a 0.4 Gyr system has not had any.
* **Metallicity** is how much heavy element existed to build planets out of. A
  metal-poor halo star simply has less iron to go around, system-wide.

The spectral class distribution is the real one, which matters more than it
looks: **roughly three quarters of all stars are M dwarfs**, and Sun-like G
stars are under 8%. Finding a G2V with a world in its habitable zone should feel
like a find, because in reality it is one.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

#: Relative frequency of each spectral class on the main sequence, from
#: observed solar-neighbourhood counts. O stars are so rare (~1 in 3 million)
#: that they are folded into B rather than given a slot that never fires.
SPECTRAL_DISTRIBUTION: tuple[tuple[str, float], ...] = (
    ("M", 0.7645),
    ("K", 0.1210),
    ("G", 0.0760),
    ("F", 0.0300),
    ("A", 0.0060),
    ("B", 0.0025),
)

#: Mass range in solar masses for each class, main sequence.
CLASS_MASS_RANGE: dict[str, tuple[float, float]] = {
    "M": (0.08, 0.45),
    "K": (0.45, 0.80),
    "G": (0.80, 1.04),
    "F": (1.04, 1.40),
    "A": (1.40, 2.10),
    "B": (2.10, 16.0),
}

#: Surface temperature range in kelvin for each class.
CLASS_TEMP_RANGE: dict[str, tuple[float, float]] = {
    "M": (2400, 3700),
    "K": (3700, 5200),
    "G": (5200, 6000),
    "F": (6000, 7500),
    "A": (7500, 10000),
    "B": (10000, 30000),
}

#: Human-readable colour, purely descriptive.
CLASS_COLOUR: dict[str, str] = {
    "M": "red dwarf",
    "K": "orange dwarf",
    "G": "yellow dwarf",
    "F": "yellow-white",
    "A": "white",
    "B": "blue-white",
}

#: Main-sequence lifetime of the Sun, in billions of years. A star's lifetime
#: scales as M/L, which is why massive stars burn out fast and M dwarfs outlive
#: the present age of the universe.
SOLAR_LIFETIME_GYR = 10.0

#: Age of the universe in Gyr -- nothing can be older.
UNIVERSE_AGE_GYR = 13.8


@dataclass(frozen=True, slots=True)
class Star:
    """A main-sequence star, fully derived from mass, age and metallicity."""

    spectral_class: str
    subclass: int
    mass_solar: float
    radius_solar: float
    luminosity_solar: float
    temperature_k: float
    age_gyr: float
    #: [Fe/H], the log ratio of iron to hydrogen against the Sun. Positive is
    #: metal-rich. Sets how much heavy element the planets could form from.
    metallicity: float

    @property
    def designation(self) -> str:
        """Standard notation, e.g. ``G2V``. The V is the main sequence."""
        return f"{self.spectral_class}{self.subclass}V"

    @property
    def colour(self) -> str:
        return CLASS_COLOUR[self.spectral_class]

    @property
    def lifetime_gyr(self) -> float:
        """Main-sequence lifetime. Scales as M/L, so roughly M^-2.5."""
        return SOLAR_LIFETIME_GYR * self.mass_solar / self.luminosity_solar

    @property
    def habitable_zone(self) -> tuple[float, float]:
        """Conservative habitable zone in AU, scaled by the square root of L.

        Flux falls off as 1/a², so the distance at which a planet receives a
        given amount of light scales as sqrt(L). The bounds are the runaway
        greenhouse and maximum greenhouse limits.
        """
        root_l = math.sqrt(self.luminosity_solar)
        return (root_l / math.sqrt(1.10), root_l / math.sqrt(0.53))

    @property
    def frost_line_au(self) -> float:
        """Distance beyond which water stays frozen during planet formation.

        The single most important line in a system: inside it worlds form rocky
        and dry, outside they keep their ice and volatiles.
        """
        return 4.85 * math.sqrt(self.luminosity_solar)

    def flux_at(self, distance_au: float) -> float:
        """Stellar flux at ``distance_au``, in Earth-equivalent units."""
        if distance_au <= 0:
            raise ValueError("distance must be positive")
        return self.luminosity_solar / (distance_au * distance_au)

    def in_habitable_zone(self, distance_au: float) -> bool:
        inner, outer = self.habitable_zone
        return inner <= distance_au <= outer


def roll_star(rng: random.Random) -> Star:
    """Generate a star. Mass is the only free roll; the rest follows."""
    spectral_class = _weighted_class(rng)
    low, high = CLASS_MASS_RANGE[spectral_class]
    mass = rng.uniform(low, high)

    # Main-sequence mass-luminosity relation. The exponent varies with mass --
    # low-mass stars are dimmer than a flat M^3.5 would suggest, which is why
    # red dwarfs are so faint and their habitable zones so tight.
    if mass < 0.43:
        luminosity = 0.23 * mass**2.3
    elif mass < 2.0:
        luminosity = mass**4.0
    else:
        luminosity = 1.5 * mass**3.5

    # Mass-radius relation, again piecewise across the low-mass break.
    radius = mass**0.8 if mass < 1.0 else mass**0.57

    # Temperature within the class band, positioned by where this mass sits in
    # the class's mass range, so a heavy K star is a hot K star.
    temp_low, temp_high = CLASS_TEMP_RANGE[spectral_class]
    position = (mass - low) / (high - low) if high > low else 0.5
    temperature = temp_low + position * (temp_high - temp_low)

    # Subclass 0-9 runs hot to cool within the class, so it is the inverse of
    # position within the mass range.
    subclass = min(9, max(0, int((1.0 - position) * 10)))

    star = Star(
        spectral_class=spectral_class,
        subclass=subclass,
        mass_solar=round(mass, 4),
        radius_solar=round(radius, 4),
        luminosity_solar=round(luminosity, 6),
        temperature_k=round(temperature, 1),
        age_gyr=0.0,  # replaced below; lifetime needs the derived luminosity
        metallicity=0.0,
    )

    # A star cannot be older than the universe, nor older than it can burn.
    max_age = min(UNIVERSE_AGE_GYR, star.lifetime_gyr)
    age = rng.uniform(0.1, max_age)

    # Metallicity correlates with age: the galaxy has been enriching itself
    # with every generation of stars, so old stars formed from poorer gas.
    age_fraction = age / UNIVERSE_AGE_GYR
    metallicity = rng.gauss(0.15 - 0.7 * age_fraction, 0.18)

    return Star(
        spectral_class=star.spectral_class,
        subclass=star.subclass,
        mass_solar=star.mass_solar,
        radius_solar=star.radius_solar,
        luminosity_solar=star.luminosity_solar,
        temperature_k=star.temperature_k,
        age_gyr=round(age, 3),
        metallicity=round(max(-2.5, min(0.6, metallicity)), 3),
    )


def _weighted_class(rng: random.Random) -> str:
    roll = rng.random()
    cumulative = 0.0
    for spectral_class, weight in SPECTRAL_DISTRIBUTION:
        cumulative += weight
        if roll <= cumulative:
            return spectral_class
    return SPECTRAL_DISTRIBUTION[0][0]  # unreachable outside float rounding
