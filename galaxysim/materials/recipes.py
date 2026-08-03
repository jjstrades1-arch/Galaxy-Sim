"""Refining: turning what a world has into what a civilization needs.

This is where specialisation comes from. Ore is nearly useless on its own, and
every chain needs specific inputs — so a world with superb iron and no copper
cannot make electronics no matter how rich it is. Trade between your own
colonies stops being about moving volume and becomes about moving *the thing
this place cannot make*.

Two rules keep the chains honest:

**Mass is roughly conserved, minus waste.** Refining discards tailings, so
outputs weigh less than inputs. You cannot launder ten tonnes of ore into twenty
tonnes of steel, which means the total material in a civilization is bounded by
what it has actually dug up.

**Nothing substitutes.** There is no generic "refine anything" recipe. If a
recipe wants uranium ore, only uranium ore will do — which is what makes a
uranium world a strategic objective rather than a preference.
"""

from __future__ import annotations

from dataclasses import dataclass

from galaxysim.materials.catalogue import (
    AIR,
    ALLOYS,
    ALUMINIUM,
    CALCIUM,
    CARBON,
    CERAMICS,
    CONSTRUCTION,
    COPPER,
    DEUTERIUM,
    ELECTRONICS,
    FERTILISER,
    FISSILES,
    FOOD,
    FUEL,
    HELIUM3,
    IRON,
    MAGNESIUM,
    MATERIALS,
    NICKEL,
    PHOSPHATES,
    POLYMERS,
    RARE_EARTHS,
    SILICON,
    STEEL,
    SULFUR,
    THORIUM,
    TITANIUM,
    URANIUM,
    WATER,
    WATER_ICE,
)


@dataclass(frozen=True, slots=True)
class Recipe:
    """One conversion an industrial sector can run."""

    key: str
    name: str
    inputs: dict[str, float]
    outputs: dict[str, float]
    #: Industry-work per run. Refining competes with construction for the same
    #: labour, which is the trade-off that makes the industry sector interesting.
    work: float
    description: str = ""

    @property
    def input_mass(self) -> float:
        return sum(
            amount * MATERIALS[key].mass_per_unit for key, amount in self.inputs.items()
        )

    @property
    def output_mass(self) -> float:
        return sum(
            amount * MATERIALS[key].mass_per_unit for key, amount in self.outputs.items()
        )

    @property
    def yield_fraction(self) -> float:
        """Fraction of input mass that survives as product. Always below one."""
        return self.output_mass / self.input_mass if self.input_mass else 0.0


RECIPES: dict[str, Recipe] = {
    r.key: r
    for r in (
        Recipe(
            "smelting",
            "Smelting",
            inputs={IRON: 10.0, CARBON: 1.0},
            outputs={STEEL: 7.0},
            work=2.0,
            description="Iron ore and carbon into steel. The first thing any colony builds.",
        ),
        Recipe(
            "light_alloys",
            "Light alloy works",
            inputs={ALUMINIUM: 8.0, MAGNESIUM: 3.0, TITANIUM: 1.0},
            outputs={ALLOYS: 7.0},
            work=3.5,
            description="Bauxite, magnesium and a little titanium into structural alloy.",
        ),
        Recipe(
            "electronics",
            "Electronics fabrication",
            # Silicon-dominant, with a little copper for interconnect and traces
            # of rare earths -- which is both what electronics are actually made
            # of and what the ground actually gives.
            #
            # It used to ask for four parts copper to six of silicon. Measured
            # across four hundred and fifty generated worlds, mining yields
            # copper at 0.02 t/h against silicon at 0.75 -- so the recipe wanted
            # copper twenty-five times faster than any world produces it, and
            # copper alone decided how much anybody could make. The visible
            # result was empires sitting on twenty-one *thousand* megatonnes of
            # silicon and a tenth of a megatonne of copper, unable to build the
            # one material terraforming, shipyards and laboratories all need.
            inputs={COPPER: 1.0, RARE_EARTHS: 1.0, SILICON: 12.0},
            outputs={ELECTRONICS: 6.0},
            work=6.0,
            description="The chain everyone needs and few worlds can supply alone.",
        ),
        Recipe(
            "polymers",
            "Polymer plant",
            inputs={CARBON: 8.0, SULFUR: 1.0},
            outputs={POLYMERS: 6.0},
            work=2.5,
            description="Carbonaceous ore into seals, insulation and habitat interiors.",
        ),
        Recipe(
            "ceramics",
            "Ceramics kiln",
            inputs={SILICON: 8.0, CALCIUM: 3.0},
            outputs={CERAMICS: 8.0},
            work=2.0,
            description="Cheap and available almost anywhere silicates are.",
        ),
        Recipe(
            "enrichment",
            "Enrichment",
            inputs={URANIUM: 12.0, FUEL: 2.0},
            outputs={FISSILES: 3.0},
            work=9.0,
            description="Uranium into reactor fuel. Wasteful, slow, and irreplaceable.",
        ),
        Recipe(
            "fuel_synthesis",
            "Fuel synthesis",
            inputs={WATER_ICE: 6.0, CARBON: 2.0},
            outputs={FUEL: 5.0},
            work=1.5,
            description="Cracked ice and carbon into drive reaction mass.",
        ),
        Recipe(
            "fertiliser",
            "Fertiliser works",
            inputs={PHOSPHATES: 5.0, SULFUR: 2.0, WATER_ICE: 2.0},
            outputs={FERTILISER: 7.0},
            work=2.0,
            description="What turns a marginal agricultural world into a productive one.",
        ),
        Recipe(
            "construction",
            "Construction materials",
            inputs={SILICON: 6.0, CALCIUM: 4.0, IRON: 2.0},
            outputs={CONSTRUCTION: 9.0},
            work=1.5,
            description="Bulk cement and prefab. Heavy, cheap, and needed everywhere.",
        ),
        Recipe(
            "silicate_ceramics",
            "Silicate ceramics",
            inputs={SILICON: 9.0, MAGNESIUM: 3.0},
            outputs={CERAMICS: 8.0},
            work=2.5,
            description="Magnesium silicates -- steatite and cordierite -- for worlds "
            "with no calcium. Slightly worse than the calcium route and available "
            "almost everywhere, which is the point: research runs on ceramics, and "
            "no crust should be able to lock a civilization out of thinking.",
        ),
        Recipe(
            "alloy_steel",
            "Alloy steel",
            inputs={STEEL: 6.0, NICKEL: 2.0, TITANIUM: 1.0},
            outputs={ALLOYS: 7.0},
            work=4.0,
            description="A second route to alloys for worlds with nickel but no bauxite.",
        ),
        Recipe(
            "thorium_cycle",
            "Thorium cycle",
            inputs={THORIUM: 20.0, FUEL: 3.0},
            outputs={FISSILES: 3.0},
            work=14.0,
            description="Thorium is commoner than uranium and far harder to use. "
            "The fallback for a world with no uranium at all.",
        ),
        Recipe(
            "water_processing",
            "Water processing",
            inputs={WATER_ICE: 10.0},
            outputs={WATER: 8.0, AIR: 1.0},
            work=1.0,
            description="Ice into drinking water and breathable oxygen.",
        ),
    )
}

#: Raw materials that no recipe consumes, because they are burned directly for
#: power rather than refined into anything. Deuterium and helium-3 are fusion
#: fuel; see the energy model. Listed explicitly so that
#: ``test_materials.py`` can tell "deliberately has no recipe" apart from
#: "somebody forgot", which is the failure this catches.
POWER_FUELS: frozenset[str] = frozenset({DEUTERIUM, HELIUM3})

#: Food has no recipe either: it comes from the agriculture labour sector,
#: not from industry. Added in the societies phase.
GROWN_NOT_REFINED: frozenset[str] = frozenset({FOOD})


def recipes_producing(material_key: str) -> tuple[Recipe, ...]:
    """Every recipe that yields ``material_key``.

    More than one route exists for alloys on purpose: a world with nickel and no
    bauxite should not be locked out of a whole tier of industry by geology
    alone. Electronics deliberately has only one route, because it is meant to
    be the chain that forces trade.
    """
    return tuple(r for r in RECIPES.values() if material_key in r.outputs)


def missing_inputs(recipe: Recipe, stock: dict[str, float]) -> tuple[str, ...]:
    """Which inputs a stockpile cannot cover. Empty means the recipe can run."""
    return tuple(
        sorted(key for key, amount in recipe.inputs.items() if stock.get(key, 0.0) < amount)
    )
