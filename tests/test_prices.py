"""What things cost, measured against what the economy actually produces.

This file exists because of a failure that took five phases to notice. When the
economy was rescaled to real tonnes and real people, terraforming and research
came with it and **buildings, ships, expeditions and fleet upkeep did not**. The
result was an economy where a civilization producing eighty-six million tonnes
an hour paid thirty-eight tonnes for a warship: nothing cost anything, no real
constraint bound, and the only brake left in the game was an artificial cap in
the AI -- which then froze expansion, because it was pinned to a population that
by design does not grow.

So the property under test is not a list of prices. Prices are allowed to move.
It is that **every price stays anchored to production**: measured in hours of a
real colony's output rather than in absolute tonnes, so the next time a rate
changes, this fails instead of quietly going slack.

Two reference economies, both generated rather than hand-built, because the
whole point is to span them:

* a **capital** -- eighteen billion people, deep industry, the thing a mature
  civilization builds from
* an **outpost** -- fifty thousand people on a dead rock, which has to be able
  to build *something* or a new colony is a permanent hole in the ground

They differ by about a factor of a million in construction output. Every number
in the game has to sit somewhere sensible against both.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from galaxysim.colony.buildings import BUILDING_TYPES, building_type
from galaxysim.colony.industry import cost_of_level, work_of_level
from galaxysim.colony.labor import balanced_allocation
from galaxysim.engine.context import TickContext
from galaxysim.engine.rates import DEFAULT_RATES, CADENCE_HOURLY
from galaxysim.engine.resolvers.production import construction_output, industry_output
from galaxysim.materials.costs import (
    COLONY_POD_COST,
    COLONY_POD_WORK,
    FLEET_COST_PER_STRENGTH,
    FLEET_UPKEEP_PER_STRENGTH,
)
from galaxysim.model.base import open_session
from galaxysim.model.entities import Colony, Universe, World
from galaxysim.terraform.projects import PROJECTS
from tests.conftest import OUTPOST_POPULATION, give_deposits, new_universe

#: Projects in a full outpost-to-world transformation. Measured, not guessed:
#: this is the length of the sequence that takes a cold rock to a breathable
#: world in ``test_terraform.py``, and it is what "terraforming takes three
#: weeks" has to be measured against. One project is not a terraformed planet.
PROJECTS_IN_A_TRANSFORMATION = 14

HOURS_PER_DAY = 24.0
HOURS_PER_WEEK = 24.0 * 7.0


def _hourly_context(session, universe) -> TickContext:
    """A context whose per-tick share is exactly one hour, so work reads as work."""
    return TickContext.build(session, universe, CADENCE_HOURLY, DEFAULT_RATES)


@pytest.fixture(scope="module")
def economies():
    """Construction output per hour for a developed capital and a bare outpost.

    Module-scoped: generating a universe is the slow part and nothing here
    mutates it.
    """
    from galaxysim.model.base import create_engine_for

    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=1, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        ctx = _hourly_context(session, universe)

        capital = session.scalar(select(Colony).order_by(Colony.id))
        capital_work = construction_output(ctx, capital)
        capital_materials = industry_output(ctx, capital)

        # A fresh landing on a dead rock, built the way bootstrap would leave it.
        world = session.scalars(
            select(World).where(World.colony == None).order_by(World.id)  # noqa: E711
        ).first()
        give_deposits(world, iron=0.01, silicon=0.01)
        outpost = Colony(
            world_id=world.id,
            civ_id=capital.civ_id,
            name="Landing",
            population=OUTPOST_POPULATION,
            founded_tick=0,
            stockpile={},
            labor=balanced_allocation(),
        )
        session.add(outpost)
        session.flush()
        outpost_work = construction_output(ctx, outpost)

        return {
            "capital_work_per_hour": capital_work,
            "capital_industry_per_hour": capital_materials,
            "outpost_work_per_hour": outpost_work,
            "capital_population": capital.population,
        }


def _hours(work: float, per_hour: float) -> float:
    assert per_hour > 0, "a reference economy that produces nothing measures nothing"
    return work / per_hour


# --- the two ends of the ladder ----------------------------------------------


def test_the_two_reference_economies_are_a_million_apart(economies):
    """The span every price has to work across, stated so it cannot drift.

    If this narrows, the level curve stops needing to be as steep as it is and
    the constants below want revisiting. If it widens, the outpost stops being
    able to build anything.
    """
    ratio = economies["capital_work_per_hour"] / economies["outpost_work_per_hour"]
    assert 1e5 < ratio < 1e7, (
        f"capital produces {ratio:,.0f}x the outpost; the level curve is tuned "
        "for about a million"
    )


#: What a landing party has to be able to build to become a colony at all:
#: something to dig with, something to process it, somewhere to keep the stores
#: and something to breathe under. Deliberately not the whole catalogue -- a
#: shipyard on a mining outpost is exactly what the design does *not* want, and
#: pricing one out of reach is how that is enforced rather than by a rule.
BOOTSTRAP_INDUSTRIES = ("mine", "collector", "granary", "refinery", "hydroponics", "dome")


def test_a_landing_party_can_build_its_first_industry(economies):
    """The floor, and the one that must never break.

    A colony that cannot build its first mine is a permanent hole in the ground:
    it can never grow the industry that would let it grow. Weeks is right --
    long enough that founding something is a commitment, short enough that one
    supply run keeps the settlers alive through it.
    """
    per_hour = economies["outpost_work_per_hour"]

    cheapest = min(
        _hours(work_of_level(building_type(kind).work, 1), per_hour)
        for kind in BOOTSTRAP_INDUSTRIES
    )
    assert cheapest < 10 * HOURS_PER_DAY, (
        f"the cheapest industry takes an outpost {cheapest / 24:.0f} days; "
        "a landing party has to be able to start somewhere"
    )

    for kind in BOOTSTRAP_INDUSTRIES:
        spec = building_type(kind)
        days = _hours(work_of_level(spec.work, 1), per_hour) / HOURS_PER_DAY
        assert days < 25.0, (
            f"{spec.name} level 1 takes an outpost {days:.0f} days; "
            "a colony could never establish itself"
        )


def test_a_frontier_outpost_is_not_a_naval_base(economies):
    """It can build the slipway. It cannot build anything to put on it.

    The design gates fleet construction on a shipyard, but a gate is a rule and
    this project prefers a price. What actually stops a mining colony becoming a
    war machine is that a hull is an industrial undertaking and the colony has
    no industry.
    """
    per_hour = economies["outpost_work_per_hour"]
    yard = _hours(work_of_level(building_type("shipyard").work, 1), per_hour)
    assert yard / HOURS_PER_DAY < 60.0, "the slipway itself should be reachable"

    hull = _hours(DEFAULT_RATES.fleet_work_per_strength, per_hour)
    assert hull / (HOURS_PER_WEEK * 52) > 10.0, (
        f"an outpost builds a point of warship in {hull / HOURS_PER_WEEK / 52:.0f} "
        "years; that should be hopeless, not merely slow"
    )


def test_a_capital_cannot_max_out_its_world_in_an_afternoon(economies):
    """The ceiling. Deepening a world has to be a campaign, not a purchase.

    A capital's warehouses are never the constraint at real scale -- it produces
    tonnage faster than anything can consume it -- so *time* is what has to bind,
    and this is where that is asserted. Level 300 is roughly what a developed
    world actually reaches, so it is the level worth pinning.
    """
    per_hour = economies["capital_work_per_hour"]
    mine = building_type("mine")

    week = _hours(work_of_level(mine.work, 200), per_hour) / HOURS_PER_WEEK
    assert 1.0 < week < 8.0, f"level 200 takes {week:.1f} weeks"

    deep = _hours(work_of_level(mine.work, 320), per_hour) / HOURS_PER_WEEK
    assert deep > 8.0, (
        f"the deepest levels a world can staff take {deep:.1f} weeks; "
        "they should be a season's commitment"
    )


def test_a_capital_builds_a_warship_in_hours_and_an_outpost_does_not(economies):
    """A shipyard on the frontier is not a naval base.

    Deliberate: the design gates fleet construction on a shipyard precisely so
    that a mining outpost cannot quietly become a war machine. The price is what
    makes that true rather than the gate.
    """
    work = DEFAULT_RATES.fleet_work_per_strength * 2.0

    hours = _hours(work, economies["capital_work_per_hour"])
    assert 2.0 < hours < 48.0, f"a capital takes {hours:.1f} hours for a warship"

    frontier = _hours(work, economies["outpost_work_per_hour"]) / HOURS_PER_WEEK
    assert frontier > 4.0, (
        f"an outpost builds a warship in {frontier:.1f} weeks; it should be "
        "hopeless without a real industrial base behind it"
    )


# --- terraforming, which is what the whole ladder is anchored to -------------


def test_transforming_a_world_is_about_three_weeks_for_a_young_civilization(economies):
    """The anchor. Everything else is priced relative to this.

    Note what is being measured: a *whole transformation*, not one project.
    Turning a cold rock into somewhere people can breathe takes fourteen
    projects, so pricing a single project at three weeks would put a planet ten
    months away and nothing would ever finish one.

    Three weeks for a civilization with one developed world, and -- because
    projects pool construction across a neighbourhood -- a day or two for one
    with twenty. That gap is the largest single reward for expanding, and it is
    the reason to expand at all once the ore has stopped mattering.
    """
    average = sum(p.work for p in PROJECTS.values()) / len(PROJECTS)
    campaign = average * PROJECTS_IN_A_TRANSFORMATION

    weeks = _hours(campaign, economies["capital_work_per_hour"]) / HOURS_PER_WEEK
    assert 2.0 < weeks < 5.0, (
        f"a young civ needs {weeks:.1f} weeks to convert a dead world; "
        "the anchor is about three"
    )

    # And a single showpiece project reads as days rather than a season.
    days = _hours(average, economies["capital_work_per_hour"]) / HOURS_PER_DAY
    assert 0.5 < days < 4.0, f"one project takes {days:.1f} days"


def test_a_projects_materials_take_about_as_long_as_its_work(economies):
    """The half of the price nobody was checking, and it ran the show.

    A cost has two halves — the industry-work to do it and the materials to do
    it *with* — and a project is only as fast as the slower one. The test above
    has always pinned the work at three weeks for a young civilization. Nothing
    pinned the materials, and they came out at **ten times that**: 620 million
    tonnes of electronics across the catalogue against a capital that refines
    191,000 an hour, which is a hundred and thirty-five days of its entire
    output for the electronics alone.

    So terraforming never happened. Not once, in any game: six AI civilizations
    over forty-five simulated days with fifty-four colonies between them started
    **zero** projects, every order sitting for ever on insufficient resources
    while eighteen worlds waited part-way up the habitability ladder. And since
    population, compounding growth and half the difficulty ladder are all
    downstream of worlds becoming habitable, one unchecked number was holding
    down the whole game.

    What this asserts is the relationship rather than a figure: whatever the
    scarcest material is, making it must take roughly as long as doing the work.
    Either half may move; they may not drift apart.
    """
    from galaxysim.materials import MATERIALS
    from galaxysim.materials.refining import refine

    # What the reference capital can actually refine per hour, running the plan
    # it would run, with inputs freely available. Inputs *not* being free is a
    # separate problem and would only make this worse.
    stock = {key: 1e15 for key in MATERIALS}
    _, made = refine(stock, economies["capital_industry_per_hour"], 1.0)

    catalogue: dict[str, float] = {}
    for project in PROJECTS.values():
        for material, amount in project.cost.items():
            catalogue[material] = catalogue.get(material, 0.0) + amount

    work_days = _hours(
        sum(p.work for p in PROJECTS.values()), economies["capital_work_per_hour"]
    ) / HOURS_PER_DAY

    slowest, slowest_days = "", 0.0
    for material, amount in sorted(catalogue.items()):
        per_hour = made.get(material, 0.0)
        if per_hour <= 0:
            continue  # mined rather than refined; extraction prices it
        days = amount / per_hour / HOURS_PER_DAY
        if days > slowest_days:
            slowest, slowest_days = material, days

    assert slowest_days < work_days * 2.5, (
        f"the catalogue's work is {work_days:.1f} days of the capital's output "
        f"but its {slowest} is {slowest_days:.1f} days; the materials have "
        "become the real price and the work half stopped meaning anything"
    )
    assert slowest_days > work_days * 0.1, (
        f"materials are only {slowest_days:.1f} days against {work_days:.1f} of "
        "work; terraforming has stopped costing anything but time"
    )


def test_a_project_costs_a_civilization_rather_than_a_colony():
    """Terraforming is the sink at the end of the economy, and stays that way.

    This used to assert a project cost ten thousand times the dearest building.
    That ratio was true only because buildings were priced before the rescale;
    now that a deep industry is a real commitment, the honest comparison is
    against a *level 1*, which is what a colony builds when it is deciding
    between growing here and going somewhere else.
    """
    cheapest_project = min(sum(p.cost.values()) for p in PROJECTS.values())
    dearest_building = max(sum(b.cost.values()) for b in BUILDING_TYPES)
    assert cheapest_project > dearest_building * 100, (
        f"cheapest project {cheapest_project:,.0f} t against a level-1 industry "
        f"at {dearest_building:,.0f} t"
    )

    # But a deep industry is genuinely comparable, which is the decision the
    # design wants a mature colony to face: deepen this world, or change it.
    deep = max(sum(cost_of_level(b.cost, 200).values()) for b in BUILDING_TYPES)
    assert deep > cheapest_project


# --- expansion, which is the pace of the whole game --------------------------


def test_founding_a_world_is_the_decision_of_a_season(economies):
    """A colony pod is the most expensive thing a young civilization builds.

    **It was free.** ``Fleet.colony_pods`` was an integer nobody charged for, so
    the price of settling a planet was the price of the gunboat carrying the
    pod -- nine hours of a capital's construction. That one omission set the
    pace of the entire game: AI empires founded a world every ten hours forever,
    reached five hundred colonies in four weeks, and grew their population four
    percent doing it. Enormously wide, completely hollow.

    The intended shape is a new world every five or six days for a *fresh* civ,
    accelerating from there as its industry deepens -- so the frontier speeds up
    because the empire got stronger, never because a rule let go. A capital
    building a pod also has mines and reactors going, so it spends some fraction
    of its construction on the yard; the band below is wide enough to hold any
    reasonable split and narrow enough to catch this going free again.
    """
    per_hour = economies["capital_work_per_hour"]
    days_at_full_tilt = _hours(COLONY_POD_WORK, per_hour) / HOURS_PER_DAY

    assert 10.0 < days_at_full_tilt < 22.0, (
        f"a pod is {days_at_full_tilt:.2f} days of a *fresh* capital's entire "
        "construction output. Measured against a soak, that lands a new world "
        "about every five days early on and faster as the capital deepens -- "
        "which is the intended shape: the frontier speeds up because the empire "
        "got stronger."
    )

    # And the materials are a real bill without being the brake. A shipyard
    # cannot be hurried; a warehouse can be refilled.
    materials = sum(COLONY_POD_COST.values())
    hours_of_industry = _hours(materials, economies["capital_industry_per_hour"])
    assert 0.5 < hours_of_industry < 6.0, (
        f"pod materials are {hours_of_industry:.1f} hours of the capital's "
        "output; they should cost something and still leave work as the limit"
    )


def test_an_outpost_cannot_casually_found_another_outpost(economies):
    """Expansion comes from developed worlds, not from a hole in the ground.

    Fifty thousand people on a dead rock building the kit for fifty thousand
    more, unaided, is the compounding that made empires hollow. It should take a
    frontier colony most of a human lifetime -- which is to say, it should send
    for help instead.
    """
    years = _hours(COLONY_POD_WORK, economies["outpost_work_per_hour"]) / (
        HOURS_PER_DAY * 365.0
    )
    assert years > 50.0, (
        f"a bare outpost could build a colony pod in {years:.1f} years; "
        "expansion is supposed to come from somewhere that has industry"
    )


# --- upkeep, which is what bounds a navy now that nothing else does ----------


def test_fleet_upkeep_is_the_stated_fraction_of_what_a_ship_costs_to_build():
    """A ship costs about as much to keep for three days as it did to build.

    That *ratio* is what bounds a navy at any size -- strength grows with the
    civilization building it and so does the bill. It has been the documented
    intent since the constant was written; it has never been checked, and it was
    off by four orders of magnitude for two phases because the build cost moved
    and upkeep did not.
    """
    build = sum(FLEET_COST_PER_STRENGTH.values())
    hourly = sum(FLEET_UPKEEP_PER_STRENGTH.values())
    assert hourly / build == pytest.approx(0.015, rel=0.25), (
        f"upkeep is {hourly / build * 100:.3f}% of build cost per hour; "
        "the documented figure is 1.5%"
    )


def test_a_navy_a_capital_would_actually_field_is_a_real_bill(economies):
    """Upkeep has to be felt, or hoarding ships is strictly correct.

    A capital that spent a fortnight of its construction output on warships
    should be paying a noticeable share of its industry to keep them flying.
    Not crippling -- a tenth is the documented target -- but never the rounding
    error it was, where an entire navy cost seventeen tonnes an hour against
    eighty-six million.
    """
    fortnight = economies["capital_work_per_hour"] * HOURS_PER_WEEK * 2.0
    strength = fortnight / DEFAULT_RATES.fleet_work_per_strength

    upkeep = sum(FLEET_UPKEEP_PER_STRENGTH.values()) * strength
    industry = economies["capital_industry_per_hour"]
    share = upkeep / industry

    assert 0.02 < share < 0.6, (
        f"a fortnight's worth of navy costs {share * 100:.2f}% of the capital's "
        "hourly industry; it should be a real bill"
    )
