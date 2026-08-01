"""Pacing: tick rate is granularity, not speed.

The design commitment these tests defend: the cadence controls how often the
world updates and *nothing else*. Running at five-minute ticks rather than
hourly gives a player a finer-grained history, not a faster civilization.

The way that breaks in practice is someone writing a rate "per tick" instead of
per hour -- it looks harmless, it passes every other test, and it silently makes
the game ten times faster at a finer cadence. These tests catch that.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from galaxysim.engine import intents
from galaxysim.engine.rates import CADENCE_FIVE_MINUTE, CADENCE_HOURLY, DEFAULT_RATES, Cadence
from galaxysim.engine.tick import run_ticks
from galaxysim.model.base import create_engine_for, open_session
from galaxysim.model.entities import Civ, Colony, Event, Fleet, Universe
from tests.conftest import civ_by_name, held, home_colony, new_universe

SIMULATED_HOURS = 48


def _run_for_hours(seconds_per_tick: int, hours: int, seed: int = 777) -> dict[str, float]:
    """Play the same opening for ``hours`` of simulated time at a given cadence."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(
        engine, seed=seed, seconds_per_tick=seconds_per_tick, civs=("Terrans",)
    )

    with open_session(engine) as session:
        terrans = civ_by_name(session, universe_id, "Terrans")
        intents.research(session, terrans)

    cadence = Cadence(seconds_per_tick)
    run_ticks(engine, universe_id, cadence.ticks_for_hours(hours))

    with open_session(engine) as session:
        civ = session.scalar(select(Civ).where(Civ.universe_id == universe_id).order_by(Civ.id))
        assert civ is not None
        colony = session.scalar(select(Colony).where(Colony.civ_id == civ.id).order_by(Colony.id))
        assert colony is not None
        return {
            "population": colony.population,
            "metal": held(session, civ, "metal"),
            "research_invested": civ.research_invested,
            "techs_known": float(civ.techs_known),
        }


def test_growth_is_equivalent_across_cadences():
    """Two days of simulated time is two days of growth at any cadence.

    Tolerance is loose because rates compound at different granularities and
    research steps land on whole ticks. The property under test is that the
    numbers are the *same size*, not identical -- a per-tick rate leaking in
    would show up as a 12x gap, not a 2% one.
    """
    fine = _run_for_hours(CADENCE_FIVE_MINUTE.seconds_per_tick, SIMULATED_HOURS)
    coarse = _run_for_hours(CADENCE_HOURLY.seconds_per_tick, SIMULATED_HOURS)

    for key in ("population", "metal", "research_invested"):
        assert fine[key] == pytest.approx(coarse[key], rel=0.05), (
            f"{key} diverged across cadences: {fine[key]} vs {coarse[key]}. "
            "Something is almost certainly authored per tick instead of per hour."
        )


def test_cadence_converts_per_hour_rates():
    """The one place ticks are allowed to enter the arithmetic."""
    assert CADENCE_FIVE_MINUTE.per_tick(12.0) == pytest.approx(1.0)
    assert CADENCE_HOURLY.per_tick(12.0) == pytest.approx(12.0)
    assert CADENCE_FIVE_MINUTE.ticks_per_hour == 12.0


def test_ticks_for_hours_rounds_up():
    """A job needing part of a tick still occupies a whole one."""
    cadence = Cadence(300)
    assert cadence.ticks_for_hours(1.0) == 12
    assert cadence.ticks_for_hours(0.01) == 1  # 36 seconds -> one tick
    assert cadence.ticks_for_hours(0.0) == 0


def test_growth_is_slow_enough_to_take_real_time():
    """No get-big-quick path: a day of play must not finish the game.

    This is a floor, not a balance pass -- the real tuning happens in the pacing
    soak. It exists so that a well-meaning rate change that makes early growth
    ten times faster fails here instead of shipping.
    """
    after_one_day = _run_for_hours(CADENCE_FIVE_MINUTE.seconds_per_tick, 24)

    # A single starting colony should still be a single modest colony after a
    # day, nowhere near its habitability ceiling.
    assert after_one_day["population"] < 40.0
    # And research should be a handful of steps in, not dozens.
    assert after_one_day["techs_known"] < 12.0


def test_expansion_and_research_costs_are_superlinear():
    """The two curves that stop a civ from snowballing.

    Each is checked for accelerating cost, not just increasing cost -- linear
    growth in cost would still let a large civ compound indefinitely.
    """
    rates = DEFAULT_RATES

    research_steps = [rates.research_cost(depth) for depth in range(0, 40)]
    research_deltas = [b - a for a, b in zip(research_steps, research_steps[1:])]
    assert all(b > a for a, b in zip(research_deltas, research_deltas[1:]))

    overheads = [rates.colony_overhead(n) for n in range(1, 30)]
    overhead_deltas = [b - a for a, b in zip(overheads, overheads[1:])]
    assert all(b > a for a, b in zip(overhead_deltas, overhead_deltas[1:]))

    # Per-colony drag must actually rise, or "superlinear total" would just mean
    # "more colonies".
    assert rates.colony_overhead(20) / 20 > rates.colony_overhead(2) / 2


def test_fleet_upkeep_is_charged_and_unpaid_fleets_desert():
    """A navy is a standing bill, not a one-time purchase.

    Without upkeep, hoarding ships is strictly correct and military size stops
    being a decision.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=5150, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        fleet = session.scalar(select(Fleet).where(Fleet.civ_id == civ.id).order_by(Fleet.id))
        # A navy far beyond what one colony can support, and a bare stockpile at
        # the only colony that could supply it.
        fleet.strength = 500.0
        home_colony(session, civ).stockpile = {"metal": 0.0, "energy": 0.0, "volatiles": 0.0}
        fleet_id, strength_before = fleet.id, fleet.strength

    run_ticks(engine, universe_id, 24)

    with open_session(engine) as session:
        fleet = session.get(Fleet, fleet_id)
        assert fleet.strength < strength_before, "unpaid ships should desert"
        assert session.scalars(
            select(Event).where(Event.kind == "upkeep_shortfall")
        ).all()


def test_solvent_civ_keeps_its_fleet():
    """The counterpart: upkeep must not bleed a civ that can afford it."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=5151, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        fleet = session.scalar(select(Fleet).where(Fleet.civ_id == civ.id).order_by(Fleet.id))
        home_colony(session, civ).stockpile = {"metal": 1e6, "energy": 1e6, "volatiles": 1e6}
        fleet_id, strength_before = fleet.id, fleet.strength

    run_ticks(engine, universe_id, 48)

    with open_session(engine) as session:
        assert session.get(Fleet, fleet_id).strength == pytest.approx(strength_before)


def test_universe_cadence_is_per_universe():
    """A solo game and the shared universe can tick at different resolutions."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seconds_per_tick=3600, civs=("Solo",))
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        assert universe is not None
        assert Cadence(universe.seconds_per_tick).ticks_per_hour == 1.0
