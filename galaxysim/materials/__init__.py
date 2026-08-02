"""The materials economy.

Replaces the three abstract resources (metal, energy, volatiles) with real
substances that come out of the ground the geology generator already produces.

Two ideas do the work:

**Raw materials are the elements themselves.** What a world yields is decided by
its crustal composition, which was decided by its formation history -- the
metallicity of the gas its star formed from, and whether it accreted inside or
outside the frost line. So a world is not "worth 1.4 metal"; it has iron ore at
a particular grade and depth and no uranium whatsoever.

**Refining is where specialisation lives.** Ore is nearly useless on its own.
Industry converts it along chains -- iron to steel, bauxite to alloys, rare
earths and copper to electronics -- and each step needs the right inputs in the
right place. A world with excellent iron and no copper cannot make electronics
however rich it is, which is what turns trade between your own colonies from a
volume problem into a question of what each place can actually make.
"""

from galaxysim.materials.catalogue import (
    AIR,
    ALLOYS,
    ALUMINIUM,
    CALCIUM,
    CARBON,
    CERAMICS,
    CONSTRUCTION,
    CONSUMABLES,
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
    RAW_MATERIALS,
    REFINED_MATERIALS,
    SILICON,
    STARTING_STOCKPILE,
    STEEL,
    SULFUR,
    THORIUM,
    TITANIUM,
    URANIUM,
    WATER,
    WATER_ICE,
    Material,
    MaterialClass,
    material,
)
from galaxysim.materials.costs import (
    COLONIST_COST,
    EQUIPMENT_COST,
    FLEET_COST_PER_STRENGTH,
    FLEET_UPKEEP_PER_STRENGTH,
    FREIGHTER_COST_PER_CAPACITY,
    RESEARCH_ACCELERANTS,
    RESEARCH_COST_PER_PROGRESS,
    SALVAGE_FRACTION,
    STORES_COST,
    accelerant_multiplier,
)
from galaxysim.materials.extraction import (
    STRATEGIC_MATERIALS,
    extract,
    extractable,
    extraction_rates,
    richest,
    strategic_shortfall,
)
from galaxysim.materials.recipes import RECIPES, Recipe, missing_inputs, recipes_producing
from galaxysim.materials.refining import DEFAULT_PLAN, refine, shortfalls
from galaxysim.materials.stock import can_afford, deposit, draw, spend, total_mass

__all__ = [
    # catalogue
    "MATERIALS",
    "RAW_MATERIALS",
    "REFINED_MATERIALS",
    "CONSUMABLES",
    "STARTING_STOCKPILE",
    "Material",
    "MaterialClass",
    "material",
    # material keys
    "IRON",
    "COPPER",
    "ALUMINIUM",
    "TITANIUM",
    "NICKEL",
    "URANIUM",
    "THORIUM",
    "RARE_EARTHS",
    "SILICON",
    "CARBON",
    "SULFUR",
    "PHOSPHATES",
    "MAGNESIUM",
    "CALCIUM",
    "WATER_ICE",
    "HELIUM3",
    "DEUTERIUM",
    "STEEL",
    "ALLOYS",
    "ELECTRONICS",
    "POLYMERS",
    "CERAMICS",
    "FISSILES",
    "FUEL",
    "FERTILISER",
    "CONSTRUCTION",
    "FOOD",
    "WATER",
    "AIR",
    # costs
    "FLEET_COST_PER_STRENGTH",
    "FREIGHTER_COST_PER_CAPACITY",
    "FLEET_UPKEEP_PER_STRENGTH",
    "COLONIST_COST",
    "EQUIPMENT_COST",
    "STORES_COST",
    "RESEARCH_COST_PER_PROGRESS",
    "RESEARCH_ACCELERANTS",
    "SALVAGE_FRACTION",
    "accelerant_multiplier",
    # extraction
    "extractable",
    "extraction_rates",
    "extract",
    "richest",
    "strategic_shortfall",
    "STRATEGIC_MATERIALS",
    # refining
    "RECIPES",
    "Recipe",
    "recipes_producing",
    "missing_inputs",
    "refine",
    "shortfalls",
    "DEFAULT_PLAN",
    # stock arithmetic
    "can_afford",
    "spend",
    "draw",
    "deposit",
    "total_mass",
]
