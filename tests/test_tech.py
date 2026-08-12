"""Tech as a generative lineage -- ``DESIGN.md`` §1, the game's core mechanism.

Three things have to hold, and the first is the one the design document calls
out by name as *"the test that proves infinite tech didn't break fairness"*.

The reason this file exists at all is worth stating: before it, research was a
counter. ``Civ.techs_known`` went up and **nothing in the simulation read it**,
while every civilization bought it with electronics, polymers, ceramics and fuel
taken out of real colony stockpiles. A number standing in for a thing that did
not exist -- which is the defect this project has caught in itself more often
than any other, here at the scale of a whole mechanic.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from galaxysim.engine import intents
from galaxysim.engine.rates import DEFAULT_RATES
from galaxysim.engine.tick import run_ticks
from galaxysim.model.base import create_engine_for, open_session
from galaxysim.model.entities import Civ, Colony, Tech
from galaxysim.tech import STATS, Effect, Known, TechEffects, budget, frontier_for
from tests.conftest import civ_by_name, home_colony, new_universe, rich_stockpile

BASE = DEFAULT_RATES.tech_effect_base
EXPONENT = DEFAULT_RATES.tech_effect_exponent


def _lineage(seed: int, steps: int) -> list:
    """Research ``steps`` techs from the root, always taking the first candidate."""
    known = [Known(1, ("propulsion", "gravitics"), ("coherence",))]
    taken = []
    for depth in range(steps):
        candidates = frontier_for(
            seed, known, depth, effect_base=BASE, effect_exponent=EXPONENT
        )
        chosen = candidates[0]
        known.append(Known(depth + 2, chosen.domains, chosen.concepts))
        taken.append(chosen)
    return taken


def _power(candidates) -> float:
    return TechEffects.from_effects(
        [Effect(c.effect.stat, c.effect.magnitude) for c in candidates]
    ).power()


# --- the invariant the whole mechanism rests on -------------------------------


def test_equal_research_buys_equal_power_whatever_shape_it_takes():
    """Spend buys power; the roll buys only shape.

    ``DESIGN.md`` §1 enforces fairness *at generation* rather than by rolling a
    candidate and rerolling it if it looks too strong: magnitude is a pure
    function of depth, so two civilizations that have paid the same research
    hold the same total magnitude, differently arranged. There is no lucky
    lineage because there is nothing for luck to act on.

    Checked across many seeds, which is the only way to see it: one seed shows a
    number, twenty show whether the number was an accident.

    The tolerance is not zero and the reason is the other half of the design.
    Same-stat effects stack on ``1 - Π(1 - xᵢ)``, so a civ that pours everything
    into one stat gets *less* total effect than one that spreads -- deep
    lineages self-limit, which is what keeps lateral expansion competitive.
    Shape therefore moves total power within a band. What must never happen is
    the band opening upward: concentrating must never *beat* spreading, or the
    fair-at-generation claim is empty.
    """
    steps = 12
    powers = [_power(_lineage(seed, steps)) for seed in range(1, 25)]

    additive = sum(budget(depth, base=BASE, exponent=EXPONENT) for depth in range(steps))
    assert max(powers) <= additive + 1e-9, (
        f"some shape reached {max(powers):.4f} against an additive ceiling of "
        f"{additive:.4f}; a lineage is gaining strength from how it was rolled"
    )
    # And the floor: even the most concentrated lineage is within a known band,
    # so shape is a real choice rather than a tax.
    assert min(powers) >= additive * 0.75, (
        f"the worst shape kept only {min(powers) / additive:.1%} of the additive "
        "total; concentration is being punished hard enough to be a trap"
    )


def test_power_only_ever_grows_with_what_was_paid():
    """Monotone in spend, on every seed. The other half of the invariant.

    A mechanism where researching more could leave a civilization weaker would
    make the whole economy incoherent -- and it is exactly what a magnitude
    rolled per candidate, rather than derived from depth, would eventually
    produce.
    """
    for seed in range(1, 13):
        running = [_power(_lineage(seed, steps)) for steps in range(1, 10)]
        for earlier, later in zip(running, running[1:]):
            assert later > earlier, (
                f"seed {seed}: power went {earlier:.4f} -> {later:.4f} after "
                "buying another tech"
            )


def test_the_same_civilization_always_sees_the_same_frontier():
    """A generative system has no licence to break replay.

    Everything else in this engine is seeded so a universe can be re-run tick
    for tick; ``tests/test_determinism.py`` is what holds it to that. Tech
    generates *new content* at runtime, which is exactly the kind of thing that
    reaches for a global RNG or a row id and quietly destroys it.
    """
    known = [Known(1, ("propulsion", "gravitics"), ("coherence",))]
    first = frontier_for(4242, known, 3, effect_base=BASE, effect_exponent=EXPONENT)
    again = frontier_for(4242, known, 3, effect_base=BASE, effect_exponent=EXPONENT)
    assert [c.name for c in first] == [c.name for c in again]
    assert [c.effect for c in first] == [c.effect for c in again]

    other = frontier_for(4243, known, 3, effect_base=BASE, effect_exponent=EXPONENT)
    assert [c.name for c in first] != [c.name for c in other], (
        "two different civilizations see an identical frontier; the space is "
        "not actually being generated per civ"
    )


def test_a_lineage_leaves_the_domain_it_started_in():
    """Infinite tech that only ever generates one thing is not infinite.

    Every civ starts from the same root, so without drift and synthesis every
    lineage in every game stays "gravitics, propulsion" for ever. The first
    version of this did exactly that -- it trimmed domains alphabetically, so
    the root's two always survived and every drift was silently discarded.
    """
    reached = set()
    stats = set()
    for seed in range(1, 12):
        for candidate in _lineage(seed, 14):
            reached.update(candidate.domains)
            stats.add(candidate.effect.stat)

    assert len(reached) >= 6, f"lineages only ever reached {sorted(reached)}"
    assert len(stats) >= 4, f"only {sorted(stats)} were ever moved"


# --- and it has to reach the engine -------------------------------------------


def test_research_reaches_the_economy():
    """The whole point, and the thing that was missing.

    ``techs_known`` was incremented by ``research.py`` and read by nothing. This
    asserts through ``industry_output`` -- the number the engine actually spends
    -- rather than by reading the tech row back, because reading it back is what
    a counter would also pass.
    """
    from galaxysim.engine.rates import DEFAULT_RATES, Cadence
    from galaxysim.engine.context import TickContext
    from galaxysim.engine.resolvers.production import industry_output
    from galaxysim.model.entities import Universe

    def _ctx(session, universe):
        return TickContext(
            session=session,
            universe=universe,
            cadence=Cadence(universe.seconds_per_tick),
            rates=DEFAULT_RATES,
            tick=universe.tick_number + 1,
            seed=universe.seed,
        )

    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=606, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        home = home_colony(session, civ)
        home.stockpile = rich_stockpile()
        session.flush()

        universe = session.get(Universe, universe_id)
        before = industry_output(_ctx(session, universe), home)

        session.add(
            Tech(
                civ_id=civ.id,
                name="Test Industry Lattice",
                domains=["energy"],
                concepts=["catalysis"],
                effect_stat="industry",
                effect_magnitude=0.25,
                depth=1,
                parents=[],
            )
        )
        session.flush()
        after = industry_output(_ctx(session, universe), home)

    assert before > 0, "the fixture produces no industry at all"
    assert after == pytest.approx(before * 1.25, rel=1e-6), (
        f"industry went {before:,.0f} -> {after:,.0f}; a 25% industry tech is "
        "not reaching the number the engine spends"
    )


def test_a_civilization_researching_for_a_month_actually_holds_techs():
    """End to end, through the real tick loop rather than a unit.

    Every stat a tech can move is one the engine already reads -- that is the
    selection rule for the grammar -- so a run that produces techs is a run
    where research has consequences.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=515, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        for colony in session.scalars(select(Colony).where(Colony.civ_id == civ.id)):
            colony.stockpile = rich_stockpile()
        # Research is an intent like everything else, and a standing one -- so
        # this is exactly what a player clicking "research" once would leave
        # behind, and the run has to do the rest on its own.
        intents.research(session, civ)

    run_ticks(engine, universe_id, 24 * 21)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        techs = session.scalars(select(Tech).where(Tech.civ_id == civ.id)).all()
        assert len(techs) > 1, "three weeks of a stocked civ bought no technology"
        assert civ.techs_known == len(techs) - 1, (
            f"techs_known is {civ.techs_known} against {len(techs)} rows; the "
            "count and the lineage have drifted apart"
        )
        assert all(tech.effect_stat in STATS for tech in techs), (
            "a tech moves a stat nothing in the engine reads"
        )


# --- steering it --------------------------------------------------------------


def _banked_civ(prefer: str | None):
    """A civ with a standing programme and enough banked to buy immediately."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=717, civs=("Terrans",), seconds_per_tick=3600)
    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        intents.research(session, civ, prefer=prefer)
        # Enough for one step, and no laboratories running, so exactly one tech
        # is bought and the choice under test is the only thing that moved.
        civ.research_progress = DEFAULT_RATES.research_cost(civ.techs_known) * 1.01
    run_ticks(engine, universe_id, 1)
    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        return session.scalars(
            select(Tech).where(Tech.civ_id == civ.id).order_by(Tech.id)
        ).all()


def test_a_preference_steers_the_lineage():
    """Naming what you are trying to become actually gets you it."""
    from galaxysim.engine.resolvers import research as resolver
    from galaxysim.engine.context import TickContext
    from galaxysim.engine.rates import Cadence
    from galaxysim.model.entities import Universe

    # Find a stat this civ's own depth-0 frontier actually offers, so the test
    # is about honouring a preference rather than about which stats exist.
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=717, civs=("Terrans",), seconds_per_tick=3600)
    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        universe = session.get(Universe, universe_id)
        ctx = TickContext(
            session=session,
            universe=universe,
            cadence=Cadence(universe.seconds_per_tick),
            rates=DEFAULT_RATES,
            tick=universe.tick_number + 1,
            seed=universe.seed,
        )
        offered = [c.effect.stat for c in resolver.frontier(ctx, civ)]
    wanted = next(stat for stat in offered if stat != offered[0])

    techs = _banked_civ(wanted)
    assert len(techs) == 2, "the fixture did not buy exactly one tech"
    assert techs[-1].effect_stat == wanted, (
        f"asked for {wanted}, got {techs[-1].effect_stat}; the preference is "
        "not reaching the choice"
    )


def test_a_preference_can_never_stall_research():
    """The half that matters, because failing it would be invisible.

    A preference that could refuse would stop a standing order dead -- and a
    standing order exists so an offline player keeps advancing, so the symptom
    would be a research programme silently frozen for weeks while everything
    else carried on. It has to be a bias, never a gate.
    """
    from galaxysim.engine.context import TickContext
    from galaxysim.engine.rates import Cadence
    from galaxysim.engine.resolvers import research as resolver
    from galaxysim.model.entities import Universe

    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=717, civs=("Terrans",), seconds_per_tick=3600)
    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        universe = session.get(Universe, universe_id)
        offered = {
            c.effect.stat
            for c in resolver.frontier(
                TickContext(
                    session=session,
                    universe=universe,
                    cadence=Cadence(universe.seconds_per_tick),
                    rates=DEFAULT_RATES,
                    tick=universe.tick_number + 1,
                    seed=universe.seed,
                ),
                civ,
            )
        }
    impossible = next(stat for stat in STATS if stat not in offered)
    assert impossible, "every stat is on offer; this guard would prove nothing"

    techs = _banked_civ(impossible)
    assert len(techs) == 2, (
        f"asking for {impossible!r}, which this frontier does not offer, stopped "
        "research entirely"
    )


def test_a_frontier_never_offers_the_same_thing_twice():
    """Five choices that list one thing twice is a menu with a bug in it.

    Collisions are common at depth 0: every candidate derives from the one root,
    so the name is drawn from the same small pool of domains and concepts. Seen
    in the real CLI readout before this existed.
    """
    known = [Known(1, ("propulsion", "gravitics"), ("coherence",))]
    for seed in range(1, 30):
        names = [
            c.name
            for c in frontier_for(
                seed, known, 0, effect_base=BASE, effect_exponent=EXPONENT
            )
        ]
        assert len(names) == len(set(names)), f"seed {seed} offered {names}"
