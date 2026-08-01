"""World generation, derived from physics.

Generation runs in causal order, and that ordering is the reason worlds stay
coherent -- see :mod:`galaxysim.worldgen.survey`, which is the entry point:

    orbit -> body -> outgassing -> [temperature <-> atmosphere] -> hydrosphere
          -> biosphere -> free oxygen -> final temperature -> geology
          -> habitability

``types.py`` is the old hand-authored world-type table. It is superseded:
generation no longer picks a type and rolls stats within its ranges. Worlds are
built from physics and *then* classified descriptively by
:func:`galaxysim.worldgen.survey.classify`.
"""

from galaxysim.worldgen.biosphere import Biosphere, roll_biosphere
from galaxysim.worldgen.geology import Deposit, roll_geology
from galaxysim.worldgen.planet import Atmosphere, Body, Climate, Hydrosphere, Orbit
from galaxysim.worldgen.serialize import survey_from_json, survey_to_json
from galaxysim.worldgen.star import Star, roll_star
from galaxysim.worldgen.survey import (
    Survey,
    classify,
    derive_habitability,
    plausible_mass,
    plausible_orbits,
    survey_world,
)

__all__ = [
    "Survey",
    "survey_world",
    "classify",
    "derive_habitability",
    "plausible_orbits",
    "plausible_mass",
    "Star",
    "roll_star",
    "Orbit",
    "Body",
    "Atmosphere",
    "Climate",
    "Hydrosphere",
    "Biosphere",
    "roll_biosphere",
    "Deposit",
    "roll_geology",
    "survey_to_json",
    "survey_from_json",
]
