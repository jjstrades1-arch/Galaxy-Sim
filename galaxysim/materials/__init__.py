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
    CONSUMABLES,
    MATERIALS,
    RAW_MATERIALS,
    REFINED_MATERIALS,
    Material,
    MaterialClass,
    material,
)
from galaxysim.materials.recipes import RECIPES, Recipe, recipes_producing
from galaxysim.materials.stock import can_afford, deposit, spend, total_mass

__all__ = [
    "MATERIALS",
    "RAW_MATERIALS",
    "REFINED_MATERIALS",
    "CONSUMABLES",
    "Material",
    "MaterialClass",
    "material",
    "RECIPES",
    "Recipe",
    "recipes_producing",
    "can_afford",
    "spend",
    "deposit",
    "total_mass",
]
