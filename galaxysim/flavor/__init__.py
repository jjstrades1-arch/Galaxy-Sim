"""Free-form flavor generation: names now, descriptions and lore later.

Flavor carries no mechanical weight, which is exactly why it can be generated
freely -- there is no balance risk in a name. Build-order step 7 expands this
into culture-tagged phoneme banks and generated descriptions for tech, worlds
and species.
"""

from galaxysim.flavor.names import system_name, world_name

__all__ = ["system_name", "world_name"]
