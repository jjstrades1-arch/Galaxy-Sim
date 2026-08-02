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

#: The order a colony works through its chains when nobody has said otherwise.
#:
#: Survival first, then the bulk materials everything else is built from, then
#: the specialised chains. Water processing leads because a colony that stops
#: making water stops breathing, and that should never wait behind a batch of
#: ceramics.
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


def plan_for(priorities: dict[str, float] | None) -> list[tuple[Recipe, float]]:
    """The recipes a colony will attempt, **most important first**.

    An empty or unrecognised plan falls back to :data:`DEFAULT_PLAN` at equal
    weight, so a colony nobody has configured still works. That fallback is a
    *tuple* and its order is the priority -- water before fuel before steel --
    which is why it has always behaved sensibly.

    **Order is not cosmetic here, it is the priority**, and this used to sort by
    name. :func:`refine` walks the plan spending a shared per-material draw
    allowance as it goes, so whoever comes first gets the scarce input and a
    chain further down finds the cupboard bare no matter what weight it carries.
    With the weights alphabetised, ``polymers`` -- eight carbon a run -- drank
    the entire carbon supply before ``smelting`` was reached, and a capital
    sitting on **eight billion tonnes of iron produced no steel at all** while
    its plan listed smelting as the single highest priority. Expansion across
    eight civilizations stopped dead for two simulated months on that.

    Ties break on the recipe key so the order is still fully determined, which
    tick replay requires.
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
        for recipe, weight in active:
            share = budget * (weight / total_weight)
            runs = min(share / recipe.work, _runnable(recipe, allowance))
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
