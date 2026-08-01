"""Name generation from syllable banks.

Minimal on purpose. Step 7 replaces this with culture-tagged phoneme banks
derived from a civ's species profile, so that a civilization's own systems read
in its own language. The interface -- take an RNG, return a string -- is meant
to survive that change.
"""

from __future__ import annotations

import random

_ONSETS = (
    "b", "br", "c", "ch", "d", "dr", "f", "g", "gl", "h", "k", "kr", "l", "m",
    "n", "p", "ph", "q", "r", "s", "sh", "st", "t", "th", "tr", "v", "x", "z",
)
_NUCLEI = ("a", "e", "i", "o", "u", "ae", "ai", "ea", "io", "ou", "y")
_CODAS = ("", "", "l", "n", "r", "s", "th", "x", "n", "m", "k")

#: Bortle-style catalogue suffixes, so some systems read as surveyed rather than
#: named -- a galaxy where everything has a pretty name feels small.
_CATALOGUE_PREFIXES = ("HD", "GJ", "KX", "NGC", "TYC", "PSR")

_ROMAN = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII")


def _syllable(rng: random.Random) -> str:
    return rng.choice(_ONSETS) + rng.choice(_NUCLEI) + rng.choice(_CODAS)


def _word(rng: random.Random, syllables: int) -> str:
    return "".join(_syllable(rng) for _ in range(syllables)).capitalize()


def system_name(rng: random.Random) -> str:
    """A star system name: usually spoken, sometimes catalogued."""
    if rng.random() < 0.3:
        return f"{rng.choice(_CATALOGUE_PREFIXES)}-{rng.randrange(1000, 9999)}"
    name = _word(rng, rng.randint(2, 3))
    if rng.random() < 0.25:
        return f"{name} {rng.choice(_ROMAN)}"
    return name


def world_name(rng: random.Random, system: str, orbit_index: int) -> str:
    """A world's name.

    Most worlds are just numbered off their star, the way real catalogues do it;
    a minority get names of their own.
    """
    if rng.random() < 0.35:
        return _word(rng, rng.randint(2, 3))
    return f"{system} {_ROMAN[orbit_index % len(_ROMAN)]}"
