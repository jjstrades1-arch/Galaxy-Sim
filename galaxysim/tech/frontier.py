"""The candidates a civilization can research next.

A civ never sees a tree. It sees a handful of candidates generated at the edge
of what it already knows, and researching one regenerates them. That is what
makes the space infinite without ever materialising it: there is no graph, only
a rule for deriving the next step from the last ones.

**Derivation.** A candidate takes one or two known parents, inherits their
domains, drifts a concept, and rolls a stat conditioned on the resulting domain
mix. Two parents is a *synthesis* and reaches across domains, which is how a
lineage stops being a straight line.

Everything is rolled from :func:`galaxysim.core.seeds.rng_for` keyed on the civ
and the depth, so the same civilization at the same depth always sees the same
frontier. Replay is exact, which ``tests/test_determinism.py`` holds the whole
engine to and a generative system has no licence to break.
"""

from __future__ import annotations

from dataclasses import dataclass

from galaxysim.core.seeds import rng_for
from galaxysim.tech.genome import (
    CONCEPTS,
    DOMAIN_STATS,
    DOMAINS,
    Effect,
    budget,
)

#: How many candidates a civ chooses between. DESIGN says 4-8; the low end is
#: enough of a choice to matter and keeps the readout something a player can
#: hold in their head.
FRONTIER_WIDTH = 5


@dataclass(frozen=True, slots=True)
class Candidate:
    """A tech that could be researched next. Not persisted until it is bought."""

    name: str
    domains: tuple[str, ...]
    concepts: tuple[str, ...]
    effect: Effect
    depth: int
    parents: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Known:
    """The part of an owned tech that derivation reads."""

    id: int
    domains: tuple[str, ...]
    concepts: tuple[str, ...]


#: The single seed tech every civilization starts holding, at depth 0.
#: ``Rates.base_speed_ly_per_hour`` already describes the galaxy as though this
#: exists -- "every civ begins holding Lightspeed Travel" -- so this is the
#: claim being made good rather than a new one.
ROOT_NAME = "Lightspeed Travel"
ROOT_DOMAINS = ("propulsion", "gravitics")
ROOT_CONCEPTS = ("coherence",)


def _name(rng, domains: tuple[str, ...], concepts: tuple[str, ...]) -> str:
    """A readable label. Flavour only -- nothing mechanical reads this."""
    head = rng.choice(concepts if concepts else CONCEPTS).title()
    tail = rng.choice(domains if domains else DOMAINS).title()
    shape = rng.choice(("Theory", "Engineering", "Lattice", "Array", "Cycle", "Doctrine"))
    return f"{head} {tail} {shape}"


def derive(
    rng,
    known: list[Known],
    depth: int,
    *,
    effect_base: float,
    effect_exponent: float,
) -> Candidate:
    """One candidate at ``depth``, from one or two of ``known``."""
    parents = [rng.choice(known)]
    # A synthesis: reaching across two lineages is how a civ leaves the domain
    # it started in, and it is rarer than continuing one.
    if len(known) > 1 and rng.random() < 0.35:
        other = rng.choice([k for k in known if k.id != parents[0].id] or known)
        parents.append(other)

    domains = list(dict.fromkeys(d for parent in parents for d in parent.domains))
    # Drift: occasionally a lineage picks up a domain neither parent had, which
    # is what stops every descendant of the root looking like the root.
    if rng.random() < 0.4:
        domains.append(rng.choice(DOMAINS))
    # Trimmed by the rng rather than alphabetically. Sorting and slicing kept
    # whichever domains happened to sort first, which in practice meant every
    # descendant of the root stayed "gravitics, propulsion" for ever and drift
    # was silently discarded -- a generative system that generated one thing.
    domains = sorted(set(domains))
    if len(domains) > 3:
        domains = sorted(rng.sample(domains, 3))

    concepts = list(dict.fromkeys(c for parent in parents for c in parent.concepts))
    if rng.random() < 0.5 or not concepts:
        concepts.append(rng.choice(CONCEPTS))
    concepts = sorted(set(concepts))
    if len(concepts) > 3:
        concepts = sorted(rng.sample(concepts, 3))

    # Shape, and only shape: which stat this lineage happens to move. The
    # magnitude below is fixed by depth and cannot be rolled for.
    reachable = sorted({stat for domain in domains for stat in DOMAIN_STATS.get(domain, ())})
    stat = rng.choice(reachable) if reachable else rng.choice(sorted(DOMAIN_STATS))
    magnitude = budget(depth, base=effect_base, exponent=effect_exponent)

    return Candidate(
        name=_name(rng, tuple(domains), tuple(concepts)),
        domains=tuple(domains),
        concepts=tuple(concepts),
        effect=Effect(stat=stat, magnitude=magnitude),
        depth=depth,
        parents=tuple(parent.id for parent in parents),
    )


def frontier_for(
    civ_seed: int,
    known: list[Known],
    depth: int,
    *,
    effect_base: float,
    effect_exponent: float,
    width: int = FRONTIER_WIDTH,
) -> list[Candidate]:
    """The candidates this civ sees at ``depth``.

    Keyed on the civilization and the depth rather than on the tick, so the
    frontier is a property of where a civ has got to -- looking at it twice in
    one tick, or re-running the tick, gives the same list.
    """
    if not known:
        return []
    rng = rng_for(civ_seed, "frontier", depth)

    # Distinct names, because a menu of five that lists the same thing twice
    # reads as a bug rather than as a choice. Collisions are common early: with
    # only the root known, every candidate derives from the same parent and the
    # name is drawn from that parent's handful of domains and concepts.
    #
    # Re-derived rather than renamed, so a candidate is always a whole coherent
    # roll -- and deterministic either way, since the rng advances with each
    # attempt and is seeded from the civ and the depth.
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for _ in range(width):
        for _attempt in range(4):
            candidate = derive(
                rng, known, depth, effect_base=effect_base, effect_exponent=effect_exponent
            )
            if candidate.name not in seen:
                break
        seen.add(candidate.name)
        candidates.append(candidate)
    return candidates
