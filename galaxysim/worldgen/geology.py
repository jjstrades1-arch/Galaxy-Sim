"""Crustal composition and ore deposits.

Two ideas make this more than a second yield table:

**Composition comes from formation history.** How much heavy element a world has
is set by the metallicity of the gas its star formed from, and whether it formed
inside or outside the frost line. A metal-poor halo star simply had less iron to
build planets out of, and every world in that system is poorer for it.

**Tectonics concentrates ore.** Bulk abundance is not the same as mineable ore.
Hydrothermal circulation, volcanism and plate recycling are what gather a
diffuse element into a seam worth digging. A geologically dead world may hold as
much copper as Earth and have none of it in any usable concentration. That is
real, and it is what makes a volcanically active world worth the hazard.

Abundances are anchored to Earth's actual crust, so the numbers a player reads
are recognisable.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

#: Earth crustal abundance by mass fraction. These are the real figures.
EARTH_CRUST: dict[str, float] = {
    "silicon": 0.2770,
    "aluminium": 0.0820,
    "iron": 0.0563,
    "calcium": 0.0415,
    "magnesium": 0.0233,
    "titanium": 0.0057,
    "phosphates": 0.00105,
    "sulfur": 0.00035,
    "carbon": 0.00020,
    "nickel": 0.000084,
    "copper": 0.000060,
    "rare_earths": 0.00015,
    "thorium": 0.0000096,
    "uranium": 0.0000027,
}

#: Elements forged only in supernovae and neutron-star mergers, so their
#: abundance tracks stellar metallicity far more steeply than the light rock
#: formers do. An old metal-poor world is dramatically short of these.
HEAVY_ELEMENTS: frozenset[str] = frozenset(
    {"iron", "nickel", "copper", "titanium", "rare_earths", "thorium", "uranium"}
)

#: Volatiles, which survive only beyond the frost line or on cold worlds.
VOLATILE_RESOURCES: frozenset[str] = frozenset({"water_ice", "carbon", "sulfur", "phosphates"})

#: Deposit grade bands, by how far concentration exceeds bulk abundance.
GRADE_BANDS: tuple[tuple[float, str], ...] = (
    (8.0, "RICH"),
    (3.0, "good"),
    (1.2, "fair"),
    (0.0, "poor"),
)

#: How hard a deposit is to reach.
DEPTH_BANDS: tuple[tuple[float, str], ...] = (
    (0.66, "deep"),
    (0.33, "moderate"),
    (0.0, "shallow"),
)


@dataclass(frozen=True, slots=True)
class Deposit:
    """One element as it actually occurs on a world."""

    element: str
    #: Bulk fraction of the crust by mass.
    abundance: float
    #: How much richer exploitable deposits are than the bulk. Driven by
    #: tectonic concentration.
    concentration: float
    #: 0-1; higher is harder and more expensive to extract.
    depth: float

    @property
    def grade(self) -> str:
        for threshold, label in GRADE_BANDS:
            if self.concentration >= threshold:
                return label
        return "poor"

    @property
    def depth_label(self) -> str:
        for threshold, label in DEPTH_BANDS:
            if self.depth >= threshold:
                return label
        return "shallow"

    @property
    def yield_index(self) -> float:
        """How productive this deposit is per unit of extraction effort.

        Abundance times concentration, divided by the cost of digging deep.
        This is the single number the economy consumes; everything else about
        the deposit is there to explain it.
        """
        return self.abundance * self.concentration / (1.0 + 2.0 * self.depth)

    def describe_abundance(self) -> str:
        """Human units: percent for common elements, ppm for trace."""
        if self.abundance >= 0.001:
            return f"{self.abundance * 100:.2f}%"
        return f"{self.abundance * 1e6:.0f} ppm"


def roll_geology(
    rng: random.Random,
    *,
    metallicity: float,
    beyond_frost_line: bool,
    tectonic_activity: float,
    mass_earth: float,
    age_gyr: float,
    has_atmosphere: bool,
) -> dict[str, Deposit]:
    """Generate a world's full crustal inventory."""
    # Metallicity is a log scale: [Fe/H] = 0 is solar, +0.3 is twice solar.
    metal_factor = 10.0**metallicity
    deposits: dict[str, Deposit] = {}

    for element, earth_abundance in EARTH_CRUST.items():
        abundance = earth_abundance * rng.uniform(0.35, 2.6)

        if element in HEAVY_ELEMENTS:
            abundance *= metal_factor
        else:
            # Rock formers are less sensitive -- silicon and aluminium were
            # abundant even in the early galaxy.
            abundance *= metal_factor**0.35

        if beyond_frost_line:
            # Ice-rich worlds are proportionally poorer in rock and metal, but
            # far richer in volatiles.
            abundance *= 0.45 if element not in VOLATILE_RESOURCES else 2.4

        # Differentiation: a large body sinks its iron into the core, leaving
        # the crust relatively depleted. Small bodies stay well mixed, which is
        # why metallic asteroids are such good mining targets.
        if element in ("iron", "nickel") and mass_earth > 0.3:
            abundance *= 0.55 + 0.45 / (1.0 + mass_earth)

        # Concentration into ore bodies. Hydrothermal and volcanic processes do
        # this work, so it tracks tectonic activity -- and it has had longer to
        # happen on an older world.
        geologic_work = tectonic_activity * math.log1p(age_gyr) / math.log(5.0)
        concentration = 0.8 + 11.0 * geologic_work * rng.uniform(0.4, 1.4)

        # Erosion exposes ore. Without an atmosphere there is no weathering, so
        # deposits stay buried where they formed.
        depth = rng.uniform(0.0, 1.0)
        if has_atmosphere:
            depth *= 0.75

        deposits[element] = Deposit(
            element=element,
            abundance=round(abundance, 9),
            concentration=round(concentration, 3),
            depth=round(depth, 3),
        )

    # Surface and near-surface resources that are not crustal rock.
    if beyond_frost_line or not has_atmosphere:
        # Helium-3 is implanted by stellar wind, so it accumulates only where
        # no atmosphere and no magnetic field deflect it -- the reason lunar
        # regolith is the classic target.
        deposits["helium3"] = Deposit(
            element="helium3",
            abundance=round(rng.uniform(1e-9, 2e-8), 12),
            concentration=round(rng.uniform(0.9, 2.2), 3),
            depth=round(rng.uniform(0.0, 0.2), 3),
        )

    return deposits


def add_ice_deposits(
    rng: random.Random,
    deposits: dict[str, Deposit],
    ice_fraction: float,
    ocean_fraction: float,
) -> None:
    """Record surface water as an extractable resource, in place.

    Water is the one resource whose availability comes from the hydrosphere
    rather than the crust, and it is the one a hostile colony needs most.
    """
    surface_water = ice_fraction + ocean_fraction
    if surface_water <= 0.001:
        return

    deposits["water_ice"] = Deposit(
        element="water_ice",
        abundance=round(surface_water * rng.uniform(0.4, 1.0), 6),
        concentration=round(rng.uniform(1.5, 4.0), 3),
        # Open ocean is trivially accessible; buried ice is not.
        depth=round(0.05 if ocean_fraction > 0.05 else rng.uniform(0.2, 0.6), 3),
    )
    deposits["deuterium"] = Deposit(
        element="deuterium",
        # Deuterium is ~156 ppm of natural hydrogen, hence of water.
        abundance=round(surface_water * 1.56e-4 * rng.uniform(0.7, 1.4), 10),
        concentration=round(rng.uniform(0.9, 1.4), 3),
        depth=round(0.05 if ocean_fraction > 0.05 else 0.4, 3),
    )
