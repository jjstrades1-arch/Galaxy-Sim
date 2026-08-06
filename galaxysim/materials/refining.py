"""Running the refining chains against a colony's stockpile.

:mod:`galaxysim.materials.recipes` says what conversions exist. This says how
much of them a colony actually runs in an hour, given what it holds and how many
people it has in industry.

Two design points worth stating, because both are load-bearing:

**Refining competes with construction for the same labour.** A colony's industry
sector is one pool. Spend it on processing and buildings go up slowly; spend it
on buildings and the ore piles up unrefined. That is the trade-off that makes
the industry sector a decision rather than a throughput number, and it is why
:data:`galaxysim.engine.rates.Rates.refining_share_of_industry` exists.

**The default plan is deliberately even, not clever.** With no instructions a
colony spreads its industry across every chain it can currently run. That is a
mediocre plan on purpose: it keeps a colony alive and slowly productive, and a
player who names priorities will beat it, which is the same bargain the governor
makes. Automation should never be the optimum.

Runs are continuous rather than whole. A colony is thousands of separate
installations, not one furnace, and quantising to whole batches at a five-minute
cadence would just mean nothing ever happens.
"""

from __future__ import annotations

from galaxysim.materials.recipes import RECIPES, Recipe

#: The chains a colony runs when nobody has said otherwise, all at equal weight.
#:
#: Read as an order -- survival first, then the bulk materials everything else is
#: built from, then the specialised chains -- but the order is documentation now
#: rather than mechanism. It used to be load-bearing, because whoever came first
#: took the scarce input outright; :func:`refine` splits contested inputs by
#: weight, so an unconfigured colony spreads itself evenly across every chain it
#: can run. That is the intended default: mediocre, and beatable by a player who
#: names the two or three chains their world is actually good at.
DEFAULT_PLAN: tuple[str, ...] = (
    "water_processing",
    "fuel_synthesis",
    "smelting",
    "construction",
    "ceramics",
    "silicate_ceramics",
    "polymers",
    "light_alloys",
    "alloy_steel",
    "electronics",
    "fertiliser",
    "enrichment",
    "thorium_cycle",
)

assert set(DEFAULT_PLAN) == set(RECIPES), "every recipe needs a place in the default plan"

#: A colony will not draw an input below this fraction of what it holds in one
#: hour of refining. Without it a single chain can strip a shared input -- every
#: silicate on the world into ceramics, leaving nothing for electronics -- and a
#: player would have to babysit priorities just to stop the economy eating
#: itself. It also means a colony always has *something* left to ship.
MAX_INPUT_DRAW_PER_HOUR = 0.35


def _allowance(stock: dict[str, float], hours: float) -> dict[str, float]:
    """How much of each material may be drawn during this tick.

    Computed once, up front, and spent down across both passes. Measuring
    against the *current* stock inside each pass would let the limit compound --
    two passes at 35% would take 58% between them, and the constant would stop
    meaning what it says.
    """
    draw = MAX_INPUT_DRAW_PER_HOUR * max(hours, 0.0)
    return {key: max(0.0, amount) * draw for key, amount in stock.items()}


def _runnable(recipe: Recipe, allowance: dict[str, float]) -> float:
    """Runs of ``recipe`` the remaining allowance covers, ignoring work."""
    limit = float("inf")
    for key, amount in recipe.inputs.items():
        if amount <= 0:
            continue
        available = allowance.get(key, 0.0)
        if available <= 0:
            return 0.0
        limit = min(limit, available / amount)
    return 0.0 if limit == float("inf") else limit


def _contested(active: list[tuple[Recipe, float]]) -> dict[str, float]:
    """Total weight competing for each input across the chains that can run.

    The denominator of the split in :func:`refine`. A material only one chain
    wants comes back carrying that chain's own weight, so its share works out to
    the whole allowance and a sole claimant is never rationed.
    """
    contested: dict[str, float] = {}
    for recipe, weight in active:
        for key, amount in recipe.inputs.items():
            if amount > 0:
                contested[key] = contested.get(key, 0.0) + weight
    return contested


def plan_for(priorities: dict[str, float] | None) -> list[tuple[Recipe, float]]:
    """The recipes a colony will attempt, **most important first**.

    An empty or unrecognised plan falls back to :data:`DEFAULT_PLAN` at equal
    weight, so a colony nobody has configured still works -- and equal weight now
    means an equal share of every contested input, which is the "deliberately
    mediocre" default the design asks for rather than an accident of ordering.

    **Weight is the priority; order is only how it is read.** This used to sort
    by name, back when :func:`refine` handed each scarce input to whichever chain
    it reached first: ``polymers`` -- eight carbon a run -- drank the entire
    carbon supply before ``smelting`` was reached, and a capital sitting on
    **eight billion tonnes of iron produced no steel at all** while its plan
    listed smelting as the single highest priority. Expansion across eight
    civilizations stopped dead for two simulated months on that.

    Sorting by weight fixed the symptom and the same failure came back through
    governor-written plans; :func:`refine` now splits a contested material by
    weight instead, so being ranked first buys a bigger share and never the lot.
    The order still decides nothing that matters, which is the point -- but it is
    kept stable, ties breaking on the recipe key, because tick replay requires
    every list in the engine to be fully determined.
    """
    if priorities:
        chosen = sorted(
            (
                (RECIPES[key], float(weight))
                for key, weight in priorities.items()
                if key in RECIPES and float(weight) > 0
            ),
            key=lambda pair: (-pair[1], pair[0].key),
        )
        if chosen:
            return chosen
    return [(RECIPES[key], 1.0) for key in DEFAULT_PLAN]


def refine(
    stock: dict[str, float],
    work: float,
    hours: float,
    *,
    priorities: dict[str, float] | None = None,
    efficiency: float = 1.0,
) -> tuple[float, dict[str, float]]:
    """Run this colony's chains in place. Returns work spent and what was made.

    ``work`` is the industry-work available for refining this tick and ``hours``
    is how much wall-clock time that tick covers -- the second is what
    :data:`MAX_INPUT_DRAW_PER_HOUR` is measured against, so the draw limit is a
    real rate rather than something that tightens as ticks get shorter.

    Two passes. The first divides the budget by weight across every chain that
    can run, so no single recipe corners the industry sector. The second hands
    whatever the first could not spend -- because a chain ran out of inputs
    rather than out of labour -- to the chains that can still use it. Without the
    second pass a colony with one viable recipe would idle most of its workforce.

    **The scarce inputs are split the same way, and that is the correction.**
    This used to divide only the *work* by weight and let the draw allowance go
    first-come: whoever the plan ranked higher took as much of a shared material
    as it could use and the chain below found the cupboard bare, whatever weight
    it carried. :func:`plan_for` describes what that cost the first time -- eight
    billion tonnes of iron and no steel -- and the fix then was to reorder the
    default plan, which cured the one symptom and left the mechanism. So the
    moment governors began writing weighted plans it came back one layer up, and
    stayed: a capital ranking ``fuel_synthesis`` fourth behind two chains that
    also want carbon **made no fuel at all for a hundred and twenty days**, while
    the whole game's navy flew on the fuel its homeworld was seeded with.

    Now each contested material is divided across its claimants in proportion to
    weight, from the allowance as it stood when the pass began -- so the split
    does not depend on the order chains are visited, and a priority buys a larger
    share rather than the lot. A material only one chain wants is not rationed at
    all.
    """
    if work <= 0 or efficiency <= 0:
        return 0.0, {}

    candidates = plan_for(priorities)
    allowance = _allowance(stock, hours)
    produced: dict[str, float] = {}
    spent = 0.0
    remaining = work

    for _ in range(2):
        if remaining <= 1e-12:
            break
        active = [
            (recipe, weight)
            for recipe, weight in candidates
            if weight > 0 and recipe.work > 0 and _runnable(recipe, allowance) > 0
        ]
        total_weight = sum(weight for _, weight in active)
        if not active or total_weight <= 0:
            break

        budget = remaining
        # Both splits are measured against the pass's opening position, so no
        # chain's ration depends on how many ran before it.
        contested = _contested(active)
        opening = dict(allowance)
        for recipe, weight in active:
            share = budget * (weight / total_weight)
            quota = {
                key: opening.get(key, 0.0) * (weight / contested[key])
                for key in recipe.inputs
                if contested.get(key, 0.0) > 0
            }
            runs = min(share / recipe.work, _runnable(recipe, quota))
            if runs <= 0:
                continue
            for key, amount in recipe.inputs.items():
                drawn = amount * runs
                stock[key] = max(0.0, stock.get(key, 0.0) - drawn)
                allowance[key] = max(0.0, allowance.get(key, 0.0) - drawn)
            for key, amount in recipe.outputs.items():
                gained = amount * runs * efficiency
                stock[key] = stock.get(key, 0.0) + gained
                produced[key] = produced.get(key, 0.0) + gained
            # Output of one chain is not input to another until next tick. A
            # colony that smelted steel this hour cannot also have alloyed it,
            # and letting it would make the pass count a balance constant.
            used = runs * recipe.work
            spent += used
            remaining -= used

    return spent, produced


def shortfalls(stock: dict[str, float], wanted: dict[str, float]) -> dict[str, float]:
    """What ``stock`` is short of ``wanted``, and by how much.

    The question a player asks of a colony that cannot build what they ordered,
    and the one the logistics layer answers by shipping.
    """
    return {
        key: amount - stock.get(key, 0.0)
        for key, amount in sorted(wanted.items())
        if stock.get(key, 0.0) < amount
    }
