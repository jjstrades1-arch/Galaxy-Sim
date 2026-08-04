"""Running terraforming projects.

Shaped like construction, because it is construction -- charged up front at the
colony that ordered it, then fed industry-work until it finishes. The difference
is scale and what it touches: a building improves the colony, a project changes
the *planet*, and every other system reads the planet.

Two rules keep it honest:

* **Preconditions are physics, not prerequisites.** A project states what has to
  be true of the world before it can start -- air to work on, a field to hold it
  down, water that is liquid -- and the failure message says which. Nothing is
  gated on a tech or a build order; the sequence falls out of what is possible.
* **A project's work comes out of the same industry pool as everything else.**
  A colony terraforming its world is not building ships or deepening its mines
  while it does so, which is what makes committing to one a real decision rather
  than something you set going in the background and forget.

And one rule about who does the work, which is what makes the whole tree
reachable: **a project draws on every colony within supply range**, not just the
one on the planet. See :func:`_advance` for why that is not a convenience.
"""

from __future__ import annotations

import math

from galaxysim.core.space import distance
from galaxysim.materials import can_afford
from galaxysim.engine.context import TickContext
from galaxysim.colony.labor import INDUSTRY, normalize
from galaxysim.engine.rates import DEFAULT_RATES
from galaxysim.engine.resolvers import queries
from galaxysim.engine.resolvers.production import SUPPLY_RANGE_LY, construction_output
from galaxysim.model.entities import Colony, IntentKind, IntentStatus
from galaxysim.terraform.apply import apply_project
from galaxysim.terraform.projects import (
    NEEDS_ATMOSPHERE,
    NEEDS_BREATHABLE,
    NEEDS_LIQUID_WATER,
    NEEDS_SHIELD,
    NEEDS_TOLERABLE_TEMPERATURE,
    PROJECTS,
    Project,
)
from galaxysim.worldgen.planet import WATER_FREEZE_K
from galaxysim.worldgen.serialize import promoted_fields, survey_from_json, survey_to_json

#: Temperature band in which seeded life and falling comets both survive.
LIFE_TEMPERATURE_RANGE = (WATER_FREEZE_K - 20.0, WATER_FREEZE_K + 60.0)


def resolve(ctx: TickContext) -> None:
    _start(ctx)
    _advance(ctx)


def unmet_requirements(survey, project: Project) -> tuple[str, ...]:
    """Which of a project's physical preconditions this world fails.

    Returned as the plain-language strings the catalogue states, so the player
    is told *what is not true of the planet* rather than which rule fired.
    """
    unmet: list[str] = []
    for requirement in project.requires:
        if requirement == NEEDS_SHIELD and not survey.body.is_shielded:
            unmet.append(requirement)
        elif requirement == NEEDS_ATMOSPHERE and survey.atmosphere.pressure_bar <= 0.01:
            unmet.append(requirement)
        elif requirement == NEEDS_LIQUID_WATER and not survey.hydrosphere.liquid_water:
            unmet.append(requirement)
        elif requirement == NEEDS_BREATHABLE and not survey.atmosphere.is_breathable:
            unmet.append(requirement)
        elif requirement == NEEDS_TOLERABLE_TEMPERATURE:
            low, high = LIFE_TEMPERATURE_RANGE
            if not low <= survey.climate.surface_temp_k <= high:
                unmet.append(requirement)
    return tuple(unmet)


def _start(ctx: TickContext) -> None:
    for intent in queries.active_intents(
        ctx.session, ctx.universe.id, IntentKind.TERRAFORM.value
    ):
        if intent.status != IntentStatus.QUEUED.value:
            continue

        colony = queries.colonies_by_id(ctx).get(intent.payload.get("colony_id", -1))
        if colony is None or colony.civ_id != intent.civ_id:
            _fail(ctx, intent, "no such colony")
            continue

        project = PROJECTS.get(str(intent.payload.get("project", "")))
        if project is None:
            _fail(ctx, intent, f"no such project {intent.payload.get('project')!r}")
            continue

        survey = survey_from_json(colony.world.survey)
        unmet = unmet_requirements(survey, project)
        if unmet:
            _fail(
                ctx,
                intent,
                f"{colony.world.name} has no {unmet[0]}",
            )
            continue

        suppliers = _neighbourhood(ctx, colony)
        if not _draw_from(suppliers, project.cost):
            intent.result = _shortfall(suppliers, project.cost)
            continue

        intent.status = IntentStatus.IN_PROGRESS.value
        intent.payload["work_remaining"] = project.work
        intent.result = ""
        ctx.log(
            "terraform_started",
            f"{colony.name} began {project.name} on {colony.world.name}, "
            f"supplied by {len(suppliers)} colonies",
            civ_id=colony.civ_id,
            payload={"colony_id": colony.id, "project": project.key},
        )


def _shortfall(suppliers: list[Colony], cost: dict[str, float]) -> str:
    """Which material is actually missing, and how much of it.

    This used to read "insufficient resources within 25 ly of <colony>", which
    was wrong twice over: :func:`_neighbourhood` passes no radius, so the pool is
    the *whole civilization*, and naming a distance sent the reader looking for a
    logistics problem that does not exist. It cost a wrong diagnosis, and it
    would cost a player one -- the fix for "we own none of this anywhere" is
    nothing like the fix for "it is in the wrong warehouse".

    Names the worst shortage rather than listing all of them: a project stalls on
    the material it is furthest from, and that is the one to go and get.
    """
    available: dict[str, float] = {}
    for colony in suppliers:
        for material, amount in colony.stockpile.items():
            available[material] = available.get(material, 0.0) + amount

    short = sorted(
        (
            (available.get(material, 0.0) / wanted, material, wanted - available.get(material, 0.0))
            for material, wanted in cost.items()
            if available.get(material, 0.0) < wanted
        )
    )
    if not short:
        return "waiting on materials"
    _, material, missing = short[0]
    return (
        f"short {missing:,.0f}t of {material} across all "
        f"{len(suppliers)} of this civilization's colonies"
    )


def _neighbourhood(ctx: TickContext, colony: Colony) -> list[Colony]:
    """Every colony this civilization holds, nearest to the project first.

    **Every one, with no radius.** The name and this docstring both used to say
    "within supply range", and neither was true -- no radius is passed. That is
    the right behaviour and the wrong description: freight can go anywhere given
    time, so *materials* pool across the whole civilization, while the *work*
    attenuates with distance because an engineering corps a hundred light-years
    away is not helping you this month. See :data:`_reach`.

    Order still matters even without a cutoff: a project spends what is nearest
    before reaching further back down the line, the same rule fleet upkeep uses,
    so the warehouse next door is drawn down before the one across the empire.
    """
    colonies = queries.colonies_grouped(ctx).get(colony.civ_id, [])
    return queries.sorted_by_distance(colonies, colony.world.system.position)


#: How a distant world's contribution to a project attenuates.
#:
#: Weight is ``exp(-distance / SUPPLY_RANGE_LY)``: full next door, a third at
#: twenty-five light-years, two percent at a hundred. **Never zero**, which is
#: the change -- contribution used to be cut off flat at supply range, so an
#: empire's size stopped mattering the instant a target sat outside it, and a
#: civilization with forty developed worlds reshaped a planet no faster than one
#: with three.
#:
#: Why work attenuates at all, when materials do not: what crosses the gap for a
#: project is specialists and prefabricated plant, and those are made *by* a
#: workforce that has to stay where it is. Freight can be sent anywhere given
#: time; an engineering corps a hundred light-years away is not helping you this
#: hour.
#:
#: Exponential rather than the gentler ``1/(1+d)`` tried first, and a test caught
#: why: under the gentle curve a capital a hundred light-years off still handed
#: over a fifth of its output, which is more than every nearby outpost combined,
#: so proximity stopped deciding anything. The property to keep is that **a
#: project alone in the dark stays slow no matter how large the empire behind
#: it** -- distance decides, and size decides only once it is close enough to
#: reach.
def _reach(distance_ly: float) -> float:
    return math.exp(-max(0.0, distance_ly) / SUPPLY_RANGE_LY)


def construction_per_hour(colony: Colony) -> float:
    """A colony's construction output per real hour, estimated.

    An estimate on purpose, and the distinction matters. The tick's true figure
    is :func:`production.construction_output`, which needs a
    :class:`TickContext` because it runs through last hour's brownout and takes
    the refining share off the top -- neither of which anybody standing outside
    a tick can know. This is what the workers could do, which is the right shape
    for a plan and for a readout.

    Extracted so the estimate exists once. The AI was computing it inline while
    deciding what to reshape, and the moment a second caller wanted the same
    number the two could drift -- with the player being told one thing and the
    engine doing another.
    """
    workers = colony.population * normalize(colony.labor).get(INDUSTRY, 0.0)
    return workers * DEFAULT_RATES.industry_per_worker_per_hour


def pooled_construction_per_hour(colonies: list[Colony], target) -> float:
    """What a project at ``target`` would actually be fed, per real hour.

    The same distance weighting :func:`_advance` applies, over the same set --
    every colony the civilization owns, attenuated by :func:`_reach` rather than
    cut off. So a readout built on this says what the engine will really do,
    which is the whole point of showing it.
    """
    return sum(
        construction_per_hour(colony)
        * _reach(distance(target, colony.world.system.position))
        for colony in colonies
    )


def _draw_from(suppliers: list[Colony], cost: dict[str, float]) -> bool:
    """Take ``cost`` out of a neighbourhood's warehouses, or take nothing.

    **The materials pool exactly as the work does, and for the same reason.** A
    project on a dead world is charged to the colony standing on it, and a dead
    world is an outpost of fifty thousand people holding two tonnes of steel
    against a bill of four hundred million. Charging it there meant the AI could
    never start a project anywhere, ever -- which is precisely the circularity
    the work half already had: the only way to afford terraforming a world was
    to have already terraformed it.

    All or nothing, so a half-paid project cannot leave a neighbourhood stripped
    with nothing to show for it.
    """
    available: dict[str, float] = {}
    for colony in suppliers:
        for material, amount in colony.stockpile.items():
            available[material] = available.get(material, 0.0) + amount
    if not can_afford(available, cost):
        return False

    for material, wanted in sorted(cost.items()):
        outstanding = wanted
        for colony in suppliers:
            if outstanding <= 1e-9:
                break
            held = colony.stockpile.get(material, 0.0)
            paid = min(outstanding, held)
            if paid > 0:
                stock = dict(colony.stockpile)
                stock[material] = held - paid
                colony.stockpile = stock
                outstanding -= paid
    return True


def _advance(ctx: TickContext) -> None:
    """Feed each running project the industry a whole neighbourhood can spare.

    **A project draws on every colony of the same civ within
    :data:`SUPPLY_RANGE_LY`**, not only the one standing on the world.

    That is the difference between terraforming being possible and being a joke.
    The worlds worth terraforming are dead ones, a dead world caps at outpost
    scale, and an outpost of fifty thousand people produces about a quarter of a
    unit of construction an hour -- so the entity doing the work was always the
    one least able to do it, and the only way for it to get better at the job
    was to finish the job. Nothing could ever start.

    Pooling breaks that circle and does something better besides: an empire
    reshapes a planet faster because it has more *developed* worlds near the
    target, and since a world that has been terraformed then holds billions, it
    becomes the engine that terraforms its own neighbours. The reward for having
    expanded is compounding and nobody had to write it down. It also makes
    *where* you terraform a real decision -- a project alone in the dark stays
    slow no matter how large the empire behind it.

    Terraforming draws on construction capacity, so a colony feeding a project
    is not simultaneously building ships. That competition is the cost, and it
    is now paid by everyone in range rather than by one outpost.
    """
    running = [
        intent
        for intent in queries.active_intents(
            ctx.session, ctx.universe.id, IntentKind.TERRAFORM.value
        )
        if intent.status == IntentStatus.IN_PROGRESS.value
    ]
    if not running:
        return

    # One query for the whole universe's colonies, grouped by owner, however
    # many projects are running. A per-project query here would make a tick's
    # cost scale with how much terraforming is going on, which is exactly the
    # shape ``tests/test_tick_cost.py`` exists to forbid.
    colonies = queries.colonies_grouped(ctx)

    for intent in running:
        colony = queries.colonies_by_id(ctx).get(intent.payload.get("colony_id", -1))
        project = PROJECTS.get(str(intent.payload.get("project", "")))
        if colony is None or project is None:
            _fail(ctx, intent, "the project has no colony behind it any more")
            continue

        # The whole civilization contributes, attenuated by how far it has to
        # reach. A wide empire of outposts still gets very little -- fifty
        # mining camps make about twelve units of construction an hour between
        # them -- so what this rewards is *developed* worlds, and a world that
        # has been terraformed holds billions and becomes the engine that
        # reshapes its neighbours.
        target = colony.world.system.position
        contributors = queries.sorted_by_distance(colonies.get(colony.civ_id, []), target)
        effort = sum(
            construction_output(ctx, helper)
            * _reach(distance(target, helper.world.system.position))
            for helper in contributors
        )

        remaining = float(intent.payload.get("work_remaining", 0.0)) - effort
        if remaining > 0:
            intent.payload["work_remaining"] = remaining
            intent.result = (
                f"{remaining:,.0f} work remaining"
                f" ({len(contributors)} colonies contributing)"
            )
            continue

        _complete(ctx, intent, colony, project)


def _complete(ctx: TickContext, intent, colony: Colony, project: Project) -> None:
    """Write the finished project into the planet and re-derive it.

    Everything downstream -- breathability, habitability, carrying capacity,
    agricultural quality, how much life support costs -- reads the survey, so
    changing it here is the whole of the change. Nothing else needs telling.
    """
    world = colony.world
    before = world.habitability

    survey = apply_project(survey_from_json(world.survey), project)
    world.survey = survey_to_json(survey)
    world.habitability = survey.habitability
    world.land_area_km2 = round(survey.land_area_km2, 2)
    world.carrying_capacity = round(survey.carrying_capacity, 2)
    world.world_type = survey.world_class
    # The promoted columns are derived from the survey, so changing the survey
    # means recomputing them. This is the only place in the game that has to.
    for field, value in promoted_fields(survey).items():
        setattr(world, field, value)

    intent.status = IntentStatus.COMPLETED.value
    intent.resolved_tick = ctx.tick
    intent.result = f"habitability {before:.2f} -> {world.habitability:.2f}"

    ctx.log(
        "terraform_completed",
        f"{project.name} finished on {world.name}: habitability "
        f"{before:.2f} -> {world.habitability:.2f}, "
        f"capacity now {world.carrying_capacity:,.0f}",
        civ_id=colony.civ_id,
        payload={
            "colony_id": colony.id,
            "project": project.key,
            "habitability_before": round(before, 4),
            "habitability_after": round(world.habitability, 4),
        },
    )


def _fail(ctx: TickContext, intent, reason: str) -> None:
    intent.status = IntentStatus.FAILED.value
    intent.resolved_tick = ctx.tick
    intent.result = reason
    ctx.log("intent_failed", f"Terraforming failed: {reason}", civ_id=intent.civ_id)
