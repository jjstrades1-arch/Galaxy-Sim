"""What exists in the economy.

Hand-authored and fixed, like the building catalogue and the world type table.
Procedural generation never invents a material, so what a player learns about
steel stays true. (``new_resource_type`` remains a legal high-tier tech effect,
but that adds to this table rather than replacing the idea of a table.)

Raw material names deliberately match the element keys that
:mod:`galaxysim.worldgen.geology` produces, so extraction is a direct lookup
rather than a mapping layer. That is the whole point of the change: a colony's
output is its world's actual crust.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class MaterialClass(str, enum.Enum):
    """What a material is for, which decides how it moves through the economy."""

    #: Dug out of the ground. Yield comes from the world's deposits.
    RAW = "raw"
    #: Made by industry from raw inputs. Everything worth building needs these.
    REFINED = "refined"
    #: Consumed continuously by population rather than spent on projects.
    CONSUMABLE = "consumable"


@dataclass(frozen=True, slots=True)
class Material:
    """One substance."""

    key: str
    name: str
    material_class: MaterialClass
    description: str
    #: Tonnes per unit, for cargo capacity. Most things are one; gases and
    #: finished goods are lighter per unit of usefulness, which is why shipping
    #: electronics is cheap and shipping ore is not.
    mass_per_unit: float = 1.0

    @property
    def is_raw(self) -> bool:
        return self.material_class is MaterialClass.RAW


def _m(key, name, cls, description, mass=1.0) -> Material:
    return Material(key, name, cls, description, mass)


RAW = MaterialClass.RAW
REFINED = MaterialClass.REFINED
CONSUMABLE = MaterialClass.CONSUMABLE

# --- raw: these keys match galaxysim.worldgen.geology element names -----------

IRON = "iron"
COPPER = "copper"
ALUMINIUM = "aluminium"
TITANIUM = "titanium"
NICKEL = "nickel"
URANIUM = "uranium"
THORIUM = "thorium"
RARE_EARTHS = "rare_earths"
SILICON = "silicon"
CARBON = "carbon"
SULFUR = "sulfur"
PHOSPHATES = "phosphates"
MAGNESIUM = "magnesium"
CALCIUM = "calcium"
WATER_ICE = "water_ice"
HELIUM3 = "helium3"
DEUTERIUM = "deuterium"

# --- refined -----------------------------------------------------------------

STEEL = "steel"
ALLOYS = "alloys"
ELECTRONICS = "electronics"
POLYMERS = "polymers"
CERAMICS = "ceramics"
FISSILES = "fissiles"
FUEL = "fuel"
FERTILISER = "fertiliser"
CONSTRUCTION = "construction"

# --- consumables -------------------------------------------------------------

FOOD = "food"
WATER = "water"
AIR = "air"


MATERIALS: dict[str, Material] = {
    m.key: m
    for m in (
        # Raw
        _m(IRON, "Iron ore", RAW, "The backbone of every heavy industry.", 1.0),
        _m(COPPER, "Copper ore", RAW, "Conductors. Nothing electronic exists without it.", 1.0),
        _m(ALUMINIUM, "Bauxite", RAW, "Light structural metal, cheap where it occurs.", 1.0),
        _m(TITANIUM, "Titanium ore", RAW, "Strong, light, and awkward to refine.", 1.0),
        _m(NICKEL, "Nickel ore", RAW, "Alloying metal; concentrated in small dense bodies.", 1.0),
        _m(URANIUM, "Uranium ore", RAW, "Fissionable. Rare, and worth going to war over.", 1.0),
        _m(THORIUM, "Thorium ore", RAW, "The other fissionable, commoner and harder to use.", 1.0),
        _m(RARE_EARTHS, "Rare earth ore", RAW, "Trace metals that make electronics possible.", 1.0),
        _m(SILICON, "Silicates", RAW, "Abundant almost everywhere; ceramics and substrates.", 1.0),
        _m(CARBON, "Carbonaceous ore", RAW, "Feedstock for polymers and for life itself.", 1.0),
        _m(SULFUR, "Sulfur", RAW, "Industrial chemistry, and a volcanic world has plenty.", 1.0),
        _m(PHOSPHATES, "Phosphates", RAW, "Fertiliser. Agriculture stops without it.", 1.0),
        _m(MAGNESIUM, "Magnesium ore", RAW, "Light alloying metal.", 1.0),
        _m(CALCIUM, "Calcium ore", RAW, "Construction chemistry and cement.", 1.0),
        _m(WATER_ICE, "Water ice", RAW, "Drinking water, agriculture, and breathable oxygen.", 1.0),
        _m(HELIUM3, "Helium-3", RAW, "Clean fusion fuel, implanted by stellar wind.", 0.2),
        _m(DEUTERIUM, "Deuterium", RAW, "Fusion fuel, extracted from water.", 0.2),
        # Refined
        _m(STEEL, "Steel", REFINED, "Structures, hulls, heavy machinery.", 1.0),
        _m(ALLOYS, "Light alloys", REFINED, "Everything that has to be strong and light.", 0.8),
        _m(ELECTRONICS, "Electronics", REFINED, "Control systems, sensors, computation.", 0.3),
        _m(POLYMERS, "Polymers", REFINED, "Seals, insulation, and habitat interiors.", 0.5),
        _m(CERAMICS, "Ceramics", REFINED, "Heat shielding and pressure vessels.", 0.9),
        _m(FISSILES, "Fissiles", REFINED, "Reactor fuel and, if you insist, warheads.", 0.6),
        _m(FUEL, "Fuel", REFINED, "Drive reaction mass and chemical energy.", 0.7),
        _m(FERTILISER, "Fertiliser", REFINED, "What makes agriculture productive.", 1.0),
        _m(CONSTRUCTION, "Construction materials", REFINED, "Bulk cement, panels, prefabs.", 1.2),
        # Consumable
        _m(FOOD, "Food", CONSUMABLE, "Eaten. Not optional.", 0.6),
        _m(WATER, "Water", CONSUMABLE, "Drunk, and the input to most life support.", 1.0),
        _m(AIR, "Breathable air", CONSUMABLE, "Only needed where the world does not provide.", 0.4),
    )
}

RAW_MATERIALS: tuple[str, ...] = tuple(
    k for k, m in MATERIALS.items() if m.material_class is RAW
)
REFINED_MATERIALS: tuple[str, ...] = tuple(
    k for k, m in MATERIALS.items() if m.material_class is REFINED
)
CONSUMABLES: tuple[str, ...] = tuple(
    k for k, m in MATERIALS.items() if m.material_class is CONSUMABLE
)

#: The shape of a developed civilization's warehouses, as a share of what it
#: produces in an hour. Not absolute tonnages: a homeworld of nine billion and
#: one of one billion should both open with *the same amount of history behind
#: them*, and pinning that to production means the opening survives the next
#: time the rates move.
STARTING_STOCKPILE_HOURS: dict[str, float] = {
    STEEL: 40.0,
    ALLOYS: 15.0,
    ELECTRONICS: 8.0,
    POLYMERS: 9.0,
    CERAMICS: 9.0,
    CONSTRUCTION: 30.0,
    FUEL: 20.0,
    FOOD: 50.0,
    WATER: 50.0,
    IRON: 20.0,
    SILICON: 15.0,
    WATER_ICE: 12.0,
}


def starting_stockpile(hourly_output: float) -> dict[str, float]:
    """Opening warehouses for a civ producing ``hourly_output`` tonnes an hour.

    A species with lightspeed travel is not a landing party -- it has been
    industrial for a long time and has the stock to mount several expeditions
    and build a fleet without waiting on production. What it does *not* have is
    a reserve unrelated to its own size.
    """
    return {key: hours * hourly_output for key, hours in STARTING_STOCKPILE_HOURS.items()}


#: Convenience for tests and tooling that want a plausible bag without computing
#: a civ's output first. One tonne an hour is a village; scale it as needed.
STARTING_STOCKPILE: dict[str, float] = starting_stockpile(1.0)


def material(key: str) -> Material:
    """Look up a material, with a helpful error for a typo."""
    try:
        return MATERIALS[key]
    except KeyError:
        raise KeyError(
            f"unknown material {key!r}; known: {', '.join(sorted(MATERIALS))}"
        ) from None


def mass_of(manifest: dict[str, float]) -> float:
    """Cargo tonnage of a manifest, respecting per-unit mass.

    Why this exists: a hold full of electronics and a hold full of ore are very
    different amounts of usefulness, and freighter capacity should notice.
    """
    return sum(
        amount * MATERIALS[key].mass_per_unit
        for key, amount in manifest.items()
        if key in MATERIALS
    )
