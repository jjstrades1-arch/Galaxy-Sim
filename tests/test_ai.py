"""What an opponent is allowed to be made of.

Difficulty in this game is **attention, competence and circumstance** — how
often it thinks, how well it plays, and what kind of galaxy it is playing in.
Never free materials, never hidden information.

The reason that matters is narrower than the usual one, because this is a
single-player game and "solo must match multiplayer" is not the argument: **the
soak is the only instrument for telling whether the economy works.** Every
economic failure this project has found — a fuel famine that emptied every fleet
on day twenty-six, a colony pod that cost nothing at all, a sixty-day wall in
front of expansion, a refining plan sorted by alphabet so a capital sitting on
eight billion tonnes of iron made no steel — surfaced by watching AI
civilizations run under exactly the constraints a player faces. Hand them a
production multiplier and the soak stops measuring the game and starts measuring
the multiplier.

So the first test here is not about balance. It is the guard on that rule.
"""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import select

from galaxysim.ai import simple
from galaxysim.ai.doctrine import (
    BEST,
    DEFAULT_DOCTRINE,
    DOCTRINES,
    DRIVEN,
    LADDER,
    NEAREST,
    RELENTLESS,
    STEADY,
    Doctrine,
    doctrine,
)
from galaxysim.engine.resolvers import governor
from galaxysim.model.base import create_engine_for, open_session
from galaxysim.model.entities import Colony, Universe
from tests.conftest import civ_by_name, new_universe


# --- the rule the whole design rests on --------------------------------------


#: Every field a doctrine is allowed to carry, and what kind of thing it is.
#: Adding a field means adding it here, which is the point: the test fails until
#: somebody has said out loud what sort of lever they just introduced.
PERMITTED_FIELDS = {
    "key": "identity",
    "name": "identity",
    "description": "identity",
    # attention: how often it acts
    "decision_interval_hours": "timing",
    # competence: how well it plays
    "industrial_worlds": "count",
    "settle_by": "choice",
    "build_queue_depth": "count",
    "scout_candidates": "count",
    "scout_range_fraction": "threshold",
    "terraform_habitability": "threshold",
    "terraform_minimum_neighbourhood_work": "threshold",
    "upkeep_reserve_hours": "timing",
    "garrison_per_colony": "threshold",
    # circumstance: what galaxy it is played in
    "preferred_region": "choice",
    "rivals": "count",
}


def test_no_doctrine_may_cheat():
    """A difficulty level may not hand the AI anything a player cannot have.

    Timings, counts, thresholds and choices between legal actions — that is the
    entire permitted vocabulary. A field called ``production_multiplier`` or
    ``cost_discount`` or ``sees_uncharted_systems`` would fail here, and should.
    """
    fields = {f.name for f in dataclasses.fields(Doctrine)}
    assert fields == set(PERMITTED_FIELDS), (
        "a doctrine field was added or removed without saying what kind of lever "
        f"it is: {fields ^ set(PERMITTED_FIELDS)}"
    )

    # And no field name may even suggest a subsidy.
    forbidden = ("multiplier", "bonus", "discount", "free", "cheat", "reveal", "sees")
    for name in fields:
        assert not any(word in name for word in forbidden), (
            f"{name!r} reads like a handicap; difficulty is made of attention, "
            "competence and circumstance"
        )


def test_the_ai_reads_nothing_a_player_could_not_see():
    """Choosing where to settle uses promoted columns, never the survey document.

    Two things at once: the AI must not learn a planet's interior without
    surveying it, and it must not pay to decode the largest JSON object in the
    game once per candidate world. The second is why this is cheap; the first is
    why it is fair.
    """
    import inspect
    import re

    source = inspect.getsource(simple)

    # It must explicitly decline to load the document...
    assert "defer(World.survey)" in source, (
        "the AI's star-chart query should defer the survey; it is the largest "
        "JSON object in the game and it is loaded once per world"
    )
    # ...and never actually read one. Anything of the form `x.survey` that is
    # not the deferral itself is a read.
    reads = [
        match.group(0)
        for match in re.finditer(r"\b\w+\.survey\b", source)
        if match.group(0) != "World.survey"
    ]
    assert not reads, (
        f"the AI is reading a world's survey document ({reads}); everything it "
        "needs has been promoted to columns beside it"
    )


# --- the ladder ---------------------------------------------------------------


def test_the_ladder_climbs():
    """Harder opponents are harder on every dial that has a direction."""
    rising = (
        "industrial_worlds",
        "build_queue_depth",
        "scout_candidates",
        "scout_range_fraction",
        "terraform_habitability",
        "garrison_per_colony",
        "rivals",
    )
    falling = (
        "decision_interval_hours",  # thinks more often
        "terraform_minimum_neighbourhood_work",  # commits on less
        "upkeep_reserve_hours",  # runs thinner
    )

    for easier, harder in zip(LADDER, LADDER[1:]):
        for field in rising:
            assert getattr(harder, field) >= getattr(easier, field), (
                f"{harder.key} should not be behind {easier.key} on {field}"
            )
        for field in falling:
            assert getattr(harder, field) <= getattr(easier, field), (
                f"{harder.key} should not be behind {easier.key} on {field}"
            )

    assert STEADY.settle_by == NEAREST
    assert RELENTLESS.settles_by_quality


def test_steady_is_the_behaviour_the_economy_was_calibrated_against():
    """The default must not quietly move.

    Every price in the game is measured against what a soak of Steady opponents
    actually does — six colonies by day twenty-eight. If these values drift, the
    calibration stops meaning what it measured and nobody finds out for a phase.
    """
    assert DEFAULT_DOCTRINE is STEADY
    assert STEADY.decision_interval_hours == 2.0
    assert STEADY.industrial_worlds == 1
    assert STEADY.settle_by == NEAREST
    assert STEADY.build_queue_depth == 1
    assert STEADY.scout_candidates == 40
    assert STEADY.scout_range_fraction == 0.8
    assert STEADY.terraform_habitability == 0.25
    assert STEADY.terraform_minimum_neighbourhood_work == 5.0e5
    assert STEADY.upkeep_reserve_hours == 24.0 * 3.0
    assert STEADY.garrison_per_colony == 2.0


def test_an_unknown_difficulty_falls_back_rather_than_crashing():
    """A save from before difficulty existed must keep playing."""
    assert doctrine(None) is DEFAULT_DOCTRINE
    assert doctrine("") is DEFAULT_DOCTRINE
    assert doctrine("magnificent") is DEFAULT_DOCTRINE
    assert doctrine("RELENTLESS") is RELENTLESS  # and it is case-insensitive


# --- the competence that has the most evidence behind it ----------------------


def test_a_driven_opponent_runs_more_than_one_shipyard():
    """The single biggest lever, and it was hard-coded to one.

    ``_set_policies`` used to give the industry policy to colony *index 0* and
    nothing else — and since only that policy's build order contains a shipyard,
    every AI civilization in the game had exactly one yard, permanently, however
    large it grew. All expansion in the game funnelled through a single world.

    Yards go to the most *developed* colonies rather than the oldest, because a
    yard on a fifty-thousand-person outpost is one that can never pay for what
    it would build.
    """
    engine = create_engine_for("sqlite://")

    def industrial_count(difficulty: str) -> int:
        universe_id = new_universe(
            engine, seed=4242, civs=(f"AI-{difficulty}",), seconds_per_tick=3600
        )
        with open_session(engine) as session:
            universe = session.get(Universe, universe_id)
            universe.ai_difficulty = difficulty
            civ = civ_by_name(session, universe_id, f"AI-{difficulty}")
            civ.is_ai = True
            home = session.scalar(
                select(Colony).where(Colony.civ_id == civ.id).order_by(Colony.id)
            )
            # Four more worlds of descending development, all habitable enough
            # to escape the survival policy.
            for index in range(4):
                extra = Colony(
                    world=_free_world(session),
                    civ_id=civ.id,
                    name=f"Second {index}",
                    population=1e8,
                    development=0.4 - index * 0.05,
                    founded_tick=0,
                    stockpile={},
                    labor=dict(home.labor),
                )
                extra.world.habitability = 0.8
                session.add(extra)
            session.flush()

            turn = simple._Turn(session, universe, civ)
            simple._set_policies(turn)
            return sum(
                1
                for colony in turn.colonies
                if colony.governor_policy == governor.INDUSTRY_POLICY
            )

    assert industrial_count("steady") == 1
    assert industrial_count("driven") == 3, "a driven opponent builds several yards"


def _free_world(session):
    from galaxysim.model.entities import World

    world = session.scalars(
        select(World).where(World.colony == None).order_by(World.id)  # noqa: E711
    ).first()
    assert world is not None, "the test universe ran out of unclaimed worlds"
    return world


def test_a_driven_opponent_settles_for_geology_not_proximity():
    """Competence that feeds straight back into the economy.

    A capital was once found holding eight billion tonnes of iron and no steel,
    because smelting also wants carbon and nobody had settled anywhere carbon
    bearing. A player facing that shortage goes and takes a carbon world. This
    is the AI doing the same, and it is the difference between ``nearest`` and
    ``best``.
    """
    assert DRIVEN.settles_by_quality
    assert not STEADY.settles_by_quality

    # The scorer prefers the richer world of two equally distant candidates.
    close_and_barren = _StubWorld(world_id=1, habitability=0.0, extraction={"iron": 0.4})
    close_and_useful = _StubWorld(
        world_id=2, habitability=0.0, extraction={"carbon": 0.4, "water_ice": 0.3}
    )
    wanted = ("carbon", "water_ice")

    assert _score(close_and_useful, wanted, span=1.0) > _score(
        close_and_barren, wanted, span=1.0
    ), "a world carrying what the empire lacks should win"


@dataclasses.dataclass
class _StubWorld:
    world_id: int
    habitability: float
    extraction: dict
    land_area_km2: float = 1.0e8


def _score(world, wanted, span: float) -> float:
    """The scoring rule under test, applied the way the AI applies it."""
    geology = sum(world.extraction.get(material, 0.0) for material in wanted)
    return (
        world.habitability * simple.SETTLE_HABITABILITY_WEIGHT
        + min(1.0, world.land_area_km2 / simple.SETTLE_REFERENCE_LAND_KM2)
        * simple.SETTLE_LAND_WEIGHT
        + geology * simple.SETTLE_GEOLOGY_WEIGHT
        - span * simple.SETTLE_DISTANCE_PENALTY
    )


# --- attention ----------------------------------------------------------------


def test_thinking_rate_is_the_same_at_any_cadence():
    """The guard that did not exist, for a bug that was latent for the project.

    The AI decided once per *tick*, so an opponent in a five-minute universe
    thought twelve times per simulated hour and one in an hourly universe once.
    Its rate of play silently depended on the cadence — exactly the class of bug
    :mod:`galaxysim.engine.rates` exists to prevent, and the reason the interval
    is authored in hours.
    """
    from galaxysim.engine.rates import Cadence

    for standard in LADDER:
        hourly = max(1, Cadence(3600).ticks_for_hours(standard.decision_interval_hours))
        fine = max(1, Cadence(300).ticks_for_hours(standard.decision_interval_hours))

        decisions_per_day_hourly = 24.0 / hourly
        decisions_per_day_fine = (24.0 * 12.0) / fine
        assert decisions_per_day_fine == pytest.approx(decisions_per_day_hourly, rel=0.05), (
            f"{standard.key} thinks {decisions_per_day_fine:.1f} times a day at "
            f"five-minute ticks and {decisions_per_day_hourly:.1f} at hourly"
        )


def test_every_named_difficulty_can_actually_run():
    """Cheap end-to-end: each level plays a day without falling over."""
    from galaxysim.ai import take_all_turns
    from galaxysim.engine.tick import resolve_tick

    for key in DOCTRINES:
        engine = create_engine_for("sqlite://")
        universe_id = new_universe(
            engine, seed=77, civs=("AI-1", "AI-2"), seconds_per_tick=3600, ai=True
        )
        with open_session(engine) as session:
            session.get(Universe, universe_id).ai_difficulty = key

        for _ in range(24):
            with open_session(engine) as session:
                universe = session.get(Universe, universe_id)
                take_all_turns(session, universe)
                session.flush()
                resolve_tick(session, universe)

        with open_session(engine) as session:
            assert session.get(Universe, universe_id).tick_number == 24
