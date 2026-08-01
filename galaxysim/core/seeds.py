"""Deterministic seed derivation.

Everything random in this simulation traces back to a seed derived here. Two
rules make ticks replayable:

1. Never call :func:`hash` -- CPython randomizes string hashing per process, so
   any seed derived from it changes between runs. We use blake2b instead.
2. Never draw from the global ``random`` module. Always take a
   :class:`random.Random` from :func:`rng_for`, scoped to what you are rolling.

Scoping matters more than it looks. If movement and combat shared one stream,
adding a fleet would shift every later draw and the tick would stop being
reproducible. Each resolver derives its own stream from the tick seed plus the
identity of the thing it is rolling for, so draws stay independent.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

# blake2b digest size in bytes. 8 gives a 64-bit value, which we then mask down
# to 63 bits: seeds are stored in BIGINT columns, and Postgres BIGINT is signed,
# so an unsigned 64-bit seed would overflow on write.
_DIGEST_BYTES = 8
_SEED_MASK = (1 << 63) - 1

_SEPARATOR = b"\x1f"  # ASCII unit separator; cannot appear in our encoded parts


def _encode(part: Any) -> bytes:
    """Encode one seed component to bytes, stably across processes and platforms.

    Floats go through ``repr`` rather than their raw bytes so that the encoding
    does not depend on machine endianness.
    """
    if isinstance(part, bytes):
        return part
    if isinstance(part, bool):
        # Checked before int: bool is an int subclass and we want distinct
        # encodings for True and 1.
        return b"b:1" if part else b"b:0"
    if isinstance(part, int):
        return b"i:" + str(part).encode("utf-8")
    if isinstance(part, float):
        return b"f:" + repr(part).encode("utf-8")
    if isinstance(part, str):
        return b"s:" + part.encode("utf-8")
    if isinstance(part, (tuple, list)):
        return b"(" + _SEPARATOR.join(_encode(p) for p in part) + b")"
    raise TypeError(f"cannot derive a seed from {type(part).__name__}: {part!r}")


def derive_seed(*parts: Any) -> int:
    """Derive a stable 64-bit seed from an ordered sequence of components.

    The result depends on both the values and their order, so
    ``derive_seed(1, 2) != derive_seed(2, 1)``.
    """
    digest = hashlib.blake2b(
        _SEPARATOR.join(_encode(p) for p in parts), digest_size=_DIGEST_BYTES
    )
    return int.from_bytes(digest.digest(), "big") & _SEED_MASK


def rng_for(*parts: Any) -> random.Random:
    """Return a private RNG stream seeded from ``parts``.

    Callers should include enough identity in ``parts`` to keep their stream
    independent of everything else resolving in the same tick -- typically the
    tick seed, a resolver name, and the id of the entity being rolled for.
    """
    return random.Random(derive_seed(*parts))


def tick_seed(universe_seed: int, tick_number: int) -> int:
    """Derive the root seed for a single tick.

    Every roll made while resolving tick ``tick_number`` descends from this, so
    re-running the tick against the same starting state reproduces it exactly.
    """
    return derive_seed("tick", universe_seed, tick_number)
