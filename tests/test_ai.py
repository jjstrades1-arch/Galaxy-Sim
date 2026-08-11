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
from galaxysim.engine.resolvers.production import SUPPLY_RANGE_LY
from galaxysim.model.base import create_engine_for, open_session
from galaxysim.model.entities import Civ, Colony, Universe
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
    "terraform_campaigns": "count",
    "upkeep_reserve_hours": "timing",
    "garrison_per_colony": "threshold",
    "raid_strength": "threshold",
    "raid_population_ceiling": "threshold",
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

    from sqlalchemy import inspect as sa_inspect

    from galaxysim.engine.resolvers import queries
    from galaxysim.model.entities import World

    source = inspect.getsource(simple)

    # The document must genuinely not be loaded -- asked of the query rather
    # than of the source that calls it.
    #
    # This used to grep the AI module for ``defer(World.survey)``, which stopped
    # meaning anything the moment the star-chart query moved into
    # ``queries.charted_systems`` to be shared with the interface: the deferral
    # was still there and still working, and the guard would have failed anyway.
    # A test that watches for a spelling rather than a fact is the same mistake
    # this codebase keeps finding in itself.
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=99, civs=("A",), seconds_per_tick=3600)
    with open_session(engine) as session:
        charted = queries.charted_systems(session, universe_id)
        loaded = [w for system in charted for w in system.worlds]
        assert loaded, "the fixture charted no worlds"
        assert all("survey" in sa_inspect(w).unloaded for w in loaded), (
            "the star-chart query is loading the survey document; it is the "
            "largest JSON object in the game and it loads once per world"
        )

    # ...and the AI never reads one either. Anything of the form `x.survey` that
    # is not a deferral is a read.
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
        "terraform_campaigns",
        "garrison_per_colony",
        "raid_strength",
        "raid_population_ceiling",
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


def test_the_ladder_seats_the_hard_settings_in_the_hard_sky():
    """``preferred_region`` is ordered, and it used to run backwards.

    The ladder seated Dormant on the rim and Relentless in the core, on the
    theory that the core meant close neighbours. Rivals sit 48-51 ly apart in
    *all three* regions, so the reason was never true -- and what region really
    changes turns out to run the other way. Over 60 days at eight opponents
    across two seeds, per empire:

        supply route length, median   2.4/2.0  3.6/3.0  7.3/5.6 ly
        routes the empire needs        38/51    56/73    68/87
        fleet payments missed            0/0   54/730   28,791/27,907

    The core is the forgiving sky and the rim is the demanding one, so the
    easiest setting belongs in the core and the hardest on the rim. The middle
    of the ladder stays in the arm because that is the sky every price in the
    game was calibrated against, and moving it would invalidate all of them.
    """
    harshness = {"core": 0, "arm": 1, "rim": 2}

    seated = [harshness[d.preferred_region] for d in LADDER]
    assert seated == sorted(seated), (
        "the ladder must run from the forgiving sky to the demanding one; got "
        + ", ".join(f"{d.key}={d.preferred_region}" for d in LADDER)
    )
    assert DEFAULT_DOCTRINE.preferred_region == "arm", (
        "the calibrated default must stay in the calibrated region"
    )


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
    assert STEADY.terraform_campaigns == 1
    assert STEADY.upkeep_reserve_hours == 24.0 * 3.0
    assert STEADY.garrison_per_colony == 2.0
    # And it does not go to war. Raiding is the one dial that can take a world
    # off the player, so it belongs above the calibrated default rather than in
    # it: a steady opponent expands into empty sky, a driven one comes for your
    # frontier.
    assert STEADY.raid_strength == 0.0


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
            # Eight more worlds of descending development, all habitable enough
            # to escape the survival policy. Nine in total, which is what a
            # doctrine asking for three yards needs before the share cap below
            # stops being the binding constraint.
            for index in range(8):
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


def test_yards_are_capped_by_the_empire_that_feeds_them():
    """More aggressive is not automatically better, and here is where it stops.

    A doctrine asking for six industrial worlds out of twelve held puts half the
    empire on an industry policy and leaves too few of them mining. Measured: it
    finished *behind* the doctrine asking for three — fewer colonies and less
    population over sixty days. A yard is fed by mines somewhere, so the count a
    doctrine asks for is bounded by a share of what it actually holds.
    """
    from galaxysim.ai.simple import INDUSTRIAL_WORLD_SHARE

    # The rule, stated directly: whatever is asked for, never more than a share.
    for held in (1, 3, 6, 12, 30):
        allowed = max(1, held // INDUSTRIAL_WORLD_SHARE)
        assert min(RELENTLESS.industrial_worlds, allowed) <= max(1, held // 2), (
            "an empire should never run more yards than half its worlds"
        )

    # And a small empire gets one yard however relentless it is.
    assert min(RELENTLESS.industrial_worlds, max(1, 4 // INDUSTRIAL_WORLD_SHARE)) == 1


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


# --- the dial that can take a world off you -----------------------------------


def _border_universe(
    difficulty: str, *, rival_population: float, warships: float, defenders: float = 0.0
):
    """An AI civ with a rival's colony one system over, and ships to spare.

    Staged rather than grown, because growing it is not possible in a test and
    barely possible in a game: civilizations are seated a hundred and thirty
    light-years apart and expand a few light-years a week, so a *border* is a
    months-long achievement. What is under test is what an opponent does when it
    has one, which is a different question from how long it takes to get one.
    """
    from galaxysim.core.space import distance
    from galaxysim.model.entities import Fleet

    engine = create_engine_for("sqlite://")
    universe_id = new_universe(
        engine, seed=515, civs=("AI-1", "Neighbour"), seconds_per_tick=3600
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        universe.ai_difficulty = difficulty
        raider = civ_by_name(session, universe_id, "AI-1")
        rival = civ_by_name(session, universe_id, "Neighbour")
        raider.is_ai = True
        home = session.scalar(
            select(Colony).where(Colony.civ_id == raider.id).order_by(Colony.id)
        )

        # The rival's world, close enough that the raider's own colonies could
        # supply a siege there. Past supply range a blockade deserts, so a war
        # is fought along a border or not at all.
        world = _free_world(session)
        target = Colony(
            world=world,
            civ_id=rival.id,
            name="Contested",
            population=rival_population,
            infrastructure=1.0,
            founded_tick=0,
            stockpile={},
            labor=dict(home.labor),
        )
        session.add(target)
        session.flush()
        assert distance(
            world.system.position, home.world.system.position
        ) < SUPPLY_RANGE_LY, "the fixture needs a rival inside supply range"

        session.add(
            Fleet(
                universe_id=universe_id,
                civ_id=raider.id,
                name="Line Squadron",
                strength=warships,
                colony_pods=1,
                speed_ly_per_hour=1.0,
                cargo={},
                cargo_capacity=40.0,
                x=home.world.system.x,
                y=home.world.system.y,
                z=home.world.system.z,
            )
        )
        if defenders:
            session.add(
                Fleet(
                    universe_id=universe_id,
                    civ_id=rival.id,
                    name="Home Guard",
                    strength=defenders,
                    colony_pods=0,
                    speed_ly_per_hour=1.0,
                    cargo={},
                    cargo_capacity=40.0,
                    x=world.system.x,
                    y=world.system.y,
                    z=world.system.z,
                )
            )
        session.flush()
        return engine, universe_id, raider.id, rival.id


def _declared_wars(session, civ_id: int):
    from galaxysim.model.entities import Intent, IntentKind

    return session.scalars(
        select(Intent).where(
            Intent.civ_id == civ_id, Intent.kind == IntentKind.ATTACK.value
        )
    ).all()


def test_a_driven_opponent_takes_a_rival_frontier_world():
    """The only difficulty dial that can cost the player something they hold.

    Everything else on the ladder is an opponent that grows faster. This is the
    one that arrives. It commits the surplus of its navy, declares, and brings a
    colony pod -- because grinding a world down is not the same as owning it,
    and without somebody to land there the blockade merely starves the place.
    """
    from galaxysim.ai.simple import take_turn

    engine, universe_id, raider_id, rival_id = _border_universe(
        "driven", rival_population=50_000.0, warships=40.0
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        take_turn(session, universe, session.get(Civ, raider_id))
        session.flush()

        wars = _declared_wars(session, raider_id)
        assert wars, "a driven opponent with ships to spare should have declared"
        assert wars[0].payload["target_civ_id"] == rival_id


def test_a_steady_opponent_expands_into_empty_sky_instead():
    """The default must stay peaceful, and not by accident.

    Every price in the game is calibrated against a soak of steady opponents. An
    opponent that starts taking worlds off its neighbours is measuring something
    else, so war belongs above the calibrated default rather than inside it.
    """
    from galaxysim.ai.simple import take_turn

    engine, universe_id, raider_id, _ = _border_universe(
        "steady", rival_population=50_000.0, warships=40.0
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        take_turn(session, universe, session.get(Civ, raider_id))
        session.flush()
        assert not _declared_wars(session, raider_id)


def test_it_does_not_throw_a_fleet_at_a_homeworld():
    """Resistance is a colony's people, so a capital cannot be taken at all.

    An opponent that sends a raid at eleven billion people is not a hard
    opponent, it is a stupid one -- the siege would run for centuries while the
    fleet went unsupplied and deserted.
    """
    from galaxysim.ai.simple import take_turn

    engine, universe_id, raider_id, _ = _border_universe(
        "driven", rival_population=11.0e9, warships=40.0
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        take_turn(session, universe, session.get(Civ, raider_id))
        session.flush()
        assert not _declared_wars(session, raider_id)


def test_it_will_not_raid_itself_defenceless():
    """What it commits is a surplus. A navy that is only just enough stays home."""
    from galaxysim.ai.simple import take_turn

    engine, universe_id, raider_id, _ = _border_universe(
        "driven", rival_population=50_000.0, warships=DRIVEN.raid_strength
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        take_turn(session, universe, session.get(Civ, raider_id))
        session.flush()
        assert not _declared_wars(session, raider_id), (
            "committing the entire navy is not a surplus"
        )


def test_it_does_not_send_a_raid_at_a_world_it_cannot_outweigh():
    """Picking a fight you lose is not difficulty, it is incompetence.

    A blockade is *more* strength over a world than its owner has. A raid that
    arrives outweighed never cuts a single supply line -- it just grinds itself
    down against the defenders until it is gone. The target used to be chosen on
    distance and population alone, so a driven opponent would send twelve points
    of strength at a world guarded by thirty and lose them.

    What it reads off is where ships are standing, which is what a scout is for
    and what anybody in the system can see. Not intentions, and not anything
    about the planet that a survey would be needed for.
    """
    from galaxysim.ai.simple import take_turn

    engine, universe_id, raider_id, _ = _border_universe(
        "driven", rival_population=50_000.0, warships=40.0, defenders=500.0
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        take_turn(session, universe, session.get(Civ, raider_id))
        session.flush()
        assert not _declared_wars(session, raider_id), (
            "it committed to a siege it could never have held"
        )


def test_it_still_attacks_a_world_it_can_take():
    """The other half, and the one that matters more.

    Caution that never attacks anything is the same bug as recklessness, wearing
    better clothes. A garrison it outweighs must not put it off.
    """
    from galaxysim.ai.simple import take_turn

    engine, universe_id, raider_id, rival_id = _border_universe(
        "driven", rival_population=50_000.0, warships=40.0, defenders=2.0
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        take_turn(session, universe, session.get(Civ, raider_id))
        session.flush()
        wars = _declared_wars(session, raider_id)
        assert wars and wars[0].payload["target_civ_id"] == rival_id


def _war_status(session, civ_id: int) -> list[str]:
    return [intent.status for intent in _declared_wars(session, civ_id)]


def test_a_war_nobody_is_fighting_any_more_is_dropped():
    """Every AI in every soak declared exactly one war, ever.

    Nothing in the engine completes an attack order -- deliberately, so a war
    does not lapse while somebody is offline -- and ``_maybe_raid`` refuses to
    declare while one is outstanding. Put together, the first war a civilization
    started was the last thing it ever did about conflict: a raid ground down in
    week one left it permanently hostile to a neighbour it was not fighting and
    unable to go anywhere else for the next hundred and ten days.

    What it now checks is the thing it can see about itself -- whether any ship
    of its own is standing over, or heading toward, anything of theirs.
    """
    from galaxysim.model.entities import Fleet, IntentStatus

    from galaxysim.ai.simple import take_turn

    engine, universe_id, raider_id, _ = _border_universe(
        "driven", rival_population=50_000.0, warships=40.0
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        raider = session.get(Civ, raider_id)

        take_turn(session, universe, raider)
        session.flush()
        assert _war_status(session, raider_id) == [IntentStatus.QUEUED.value]

        # The raid does not survive the crossing. Nothing else changes: the
        # rival's world is still there, still weakly held, still worth taking.
        for fleet in session.scalars(select(Fleet).where(Fleet.civ_id == raider_id)):
            session.delete(fleet)
        session.flush()

        take_turn(session, universe, raider)
        session.flush()
        assert _war_status(session, raider_id) == [IntentStatus.CANCELLED.value], (
            "the war outlived every ship that was prosecuting it"
        )


def test_a_civilization_that_lost_a_war_can_start_another():
    """The point of letting one end. Peace that leads nowhere is just defeat.

    ``_maybe_raid``'s guard used to mean "has ever declared a war". It now means
    "is currently fighting one", and this is the difference: rebuild a navy and
    the civilization goes again, at the same rival or a better target.
    """
    from galaxysim.model.entities import Fleet, IntentStatus

    from galaxysim.ai.simple import take_turn

    engine, universe_id, raider_id, rival_id = _border_universe(
        "driven", rival_population=50_000.0, warships=40.0
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        raider = session.get(Civ, raider_id)
        home = session.scalar(
            select(Colony).where(Colony.civ_id == raider_id).order_by(Colony.id)
        )

        take_turn(session, universe, raider)
        session.flush()
        for fleet in session.scalars(select(Fleet).where(Fleet.civ_id == raider_id)):
            session.delete(fleet)
        session.flush()
        take_turn(session, universe, raider)  # notices, and stands down
        session.flush()

        session.add(
            Fleet(
                universe_id=universe_id,
                civ_id=raider_id,
                name="Second Squadron",
                strength=40.0,
                colony_pods=1,
                speed_ly_per_hour=1.0,
                cargo={},
                cargo_capacity=40.0,
                x=home.world.system.x,
                y=home.world.system.y,
                z=home.world.system.z,
            )
        )
        session.flush()

        take_turn(session, universe, raider)
        session.flush()

        wars = _declared_wars(session, raider_id)
        assert len(wars) == 2, f"it never declared again: {_war_status(session, raider_id)}"
        assert wars[-1].status == IntentStatus.QUEUED.value
        assert wars[-1].payload["target_civ_id"] == rival_id


def test_taking_the_world_ends_the_war_it_was_declared_for():
    """The other way a war finishes, and it needs no separate rule.

    Once the colony is this civilization's own, its fleet is no longer standing
    over anything belonging to the rival -- so the same "am I prosecuting
    anything" question answers no, and the order goes. No objective has to be
    recorded anywhere, which matters because nothing records one.
    """
    from galaxysim.model.entities import Fleet, IntentStatus

    from galaxysim.ai.simple import take_turn

    engine, universe_id, raider_id, rival_id = _border_universe(
        "driven", rival_population=50_000.0, warships=40.0
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        raider = session.get(Civ, raider_id)

        take_turn(session, universe, raider)
        session.flush()
        assert _war_status(session, raider_id) == [IntentStatus.QUEUED.value]

        taken = session.scalar(
            select(Colony).where(Colony.civ_id == rival_id).order_by(Colony.id)
        )
        where = taken.world.system
        # The fleet arrives and holds station, the way the siege resolver leaves
        # it -- and then the capture lands.
        for fleet in session.scalars(select(Fleet).where(Fleet.civ_id == raider_id)):
            fleet.x, fleet.y, fleet.z = where.x, where.y, where.z
            fleet.origin_x = fleet.origin_y = fleet.origin_z = None
            fleet.dest_x = fleet.dest_y = fleet.dest_z = None
            fleet.departed_tick = fleet.arrival_tick = None
        session.flush()

        # Still at war while the world is theirs and the cordon is up.
        take_turn(session, universe, raider)
        session.flush()
        assert _war_status(session, raider_id) == [IntentStatus.QUEUED.value], (
            "it gave up while its fleet was sitting on the objective"
        )

        taken.civ_id = raider_id
        session.flush()

        take_turn(session, universe, raider)
        session.flush()
        assert _war_status(session, raider_id) == [IntentStatus.CANCELLED.value]


def _siege_underway(
    *,
    cordon: float,
    defenders: float,
    reserve,
    reserve_offset_ly: float = 5.0,
    strip_starting_fleet: bool = False,
):
    """A war already being fought: a cordon up, and hulls waiting behind it.

    Staged rather than played out, for the reason ``_border_universe`` gives --
    a border takes months. What is under test is the turn *after* the raid
    arrived, which is the turn that until now did nothing at all.

    ``reserve_offset_ly`` is load-bearing. A blockade is half a light-year wide
    and ``_border_universe`` stages the contested world in the raider's *own home
    system*, so anything parked at the colony is inside the cordon by accident.
    Pushing the rest of the navy a few light-years back is what makes the cordon
    exactly ``cordon`` and the reserve a genuine second wave. Pass ``0.0`` when
    the point is a ship idling at a colony instead.

    ``reserve`` is a strength or a list of them. More than one hull matters:
    what may be spent is what is free *minus a raid's worth kept at home*, so a
    civilization whose entire reserve is one ship can never send it -- correctly,
    and not what most of these tests are about.
    """
    from galaxysim.engine import intents as intent_api
    from galaxysim.model.entities import Fleet

    engine, universe_id, raider_id, rival_id = _border_universe(
        "driven", rival_population=50_000.0, warships=cordon, defenders=defenders
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        raider = session.get(Civ, raider_id)
        home = session.scalar(
            select(Colony).where(Colony.civ_id == raider_id).order_by(Colony.id)
        )
        target = session.scalar(select(Colony).where(Colony.name == "Contested"))
        where = target.world.system

        for fleet in session.scalars(select(Fleet).where(Fleet.civ_id == raider_id)):
            if fleet.name == "Line Squadron":
                # Arrived and holding station, the way the siege resolver leaves it.
                fleet.x, fleet.y, fleet.z = where.x, where.y, where.z
            elif strip_starting_fleet:
                # A civ whose whole navy is its garrison, which is the only way
                # to have nothing spare at all.
                session.delete(fleet)
            else:
                fleet.x = home.world.system.x + reserve_offset_ly
                fleet.y, fleet.z = home.world.system.y, home.world.system.z
        intent_api.attack(session, raider, rival_id)

        hulls = (reserve,) if isinstance(reserve, (int, float)) else tuple(reserve)
        for index, strength in enumerate(h for h in hulls if h):
            session.add(
                Fleet(
                    universe_id=universe_id,
                    civ_id=raider_id,
                    name=f"Reserve Squadron {index + 1}",
                    strength=strength,
                    colony_pods=0,  # a warship, so expansion leaves it alone
                    speed_ly_per_hour=1.0,
                    cargo={},
                    cargo_capacity=40.0,
                    x=home.world.system.x + reserve_offset_ly,
                    y=home.world.system.y,
                    z=home.world.system.z,
                )
            )
        session.flush()
    return engine, universe_id, raider_id, rival_id


def _reinforcements(session, civ_id: int, system) -> list:
    """Move orders issued this turn for ships heading to ``system``."""
    from galaxysim.model.entities import Intent, IntentKind

    return [
        intent
        for intent in session.scalars(
            select(Intent).where(
                Intent.civ_id == civ_id, Intent.kind == IntentKind.MOVE_FLEET.value
            )
        )
        if intent.payload.get("x") == system.x and intent.payload.get("y") == system.y
    ]


def test_a_cordon_being_outweighed_gets_a_second_wave():
    """A war used to be a coin flip resolved on the day it was declared.

    ``_maybe_raid`` sends everything it will ever send at the moment of the
    declaration, and then refuses to declare again while the order stands -- so
    nothing in this AI ever sent a warship at an enemy twice. A player who won
    the opening engagement had won the war, permanently, with nothing further to
    answer.
    """
    from galaxysim.ai.simple import take_turn

    engine, universe_id, raider_id, rival_id = _siege_underway(
        cordon=10.0, defenders=30.0, reserve=(40.0, 12.0)
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        target = session.scalar(select(Colony).where(Colony.name == "Contested"))
        take_turn(session, universe, session.get(Civ, raider_id))
        session.flush()

        assert _reinforcements(session, raider_id, target.world.system), (
            "the reserve sat at home while the cordon was ground down"
        )


def test_a_cordon_worn_thin_is_topped_back_up():
    """The case that actually happens, and the first version of this missed it.

    Nothing shoots at a blockade. What kills one is *time*: upkeep is billed to
    colonies near a fleet and there is nothing to draw on in somebody else's
    space, so a cordon deserts away hour by hour. Measured over 120 days of eight
    driven opponents, the cordon was outweighed by defenders exactly zero times
    -- a frontier outpost has no fleet at all -- while fifteen blockades were
    established and only eight ever ground their world down.

    So the question is not "am I losing" but "is there still enough here to
    finish", and the answer is the doctrine's ``raid_strength``: what this
    opponent thinks an objective is worth.
    """
    from galaxysim.ai.doctrine import DRIVEN
    from galaxysim.ai.simple import take_turn

    assert DRIVEN.raid_strength > 2.0, "the fixture below needs a thin cordon"
    engine, universe_id, raider_id, rival_id = _siege_underway(
        cordon=2.0, defenders=0.0, reserve=(40.0, 12.0)
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        target = session.scalar(select(Colony).where(Colony.name == "Contested"))
        take_turn(session, universe, session.get(Civ, raider_id))
        session.flush()

        assert _reinforcements(session, raider_id, target.world.system), (
            "a blockade whittled down to nothing was left to expire"
        )


def test_a_blockade_that_is_working_is_left_alone():
    """Reinforcement answers a question, and the answer here is no.

    A cordon that outweighs the garrison *and* is at the strength the doctrine
    commits to an objective is cutting the supply lines it was sent to cut.
    Sending more is not caution or aggression, it is a fleet doing nothing
    somewhere it could have been doing something.
    """
    from galaxysim.ai.simple import take_turn

    engine, universe_id, raider_id, rival_id = _siege_underway(
        cordon=40.0, defenders=2.0, reserve=40.0
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        target = session.scalar(select(Colony).where(Colony.name == "Contested"))
        take_turn(session, universe, session.get(Civ, raider_id))
        session.flush()

        assert not _reinforcements(session, raider_id, target.world.system)


def test_it_will_not_feed_a_war_it_cannot_win():
    """The failure mode this feature invents, and the one that has to be shut.

    An opponent that throws its navy away one hull at a time is not difficult,
    it is the same incompetence ``_raidable_colony`` already refuses -- arriving
    outweighed, cutting nothing, grinding down against the defenders until it is
    gone. If the reserve cannot take the cordon *past* the garrison, it stays
    home and the war is stood down instead.
    """
    from galaxysim.ai.simple import take_turn

    engine, universe_id, raider_id, rival_id = _siege_underway(
        cordon=10.0, defenders=300.0, reserve=(40.0, 12.0)
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        target = session.scalar(select(Colony).where(Colony.name == "Contested"))
        take_turn(session, universe, session.get(Civ, raider_id))
        session.flush()

        assert not _reinforcements(session, raider_id, target.world.system), (
            "it sent good hulls after bad"
        )


def test_it_will_not_empty_its_home_systems_into_a_war():
    """The same surplus rule the raid opens with, applied to the second wave.

    ``_maybe_raid``'s own docstring: an opponent that empties its home systems to
    take an outpost has not become harder to play against. It commits a raid only
    while holding twice one free, so it always leaves a raid's worth at home, and
    this leaves the same -- one answer to what a civilization will spend on a war
    rather than two.

    It is emphatically *not* the doctrine garrison, which is the trap the first
    version of this fell into. A driven civ's warship strength falls away from
    that line after about eighty days -- exactly the window its wars fall in --
    so reinforcement gated on it would never dispatch anything, while
    ``_maybe_raid``, reading a different number on the same turn, cheerfully
    declares a fresh war.
    """
    from galaxysim.ai.doctrine import DRIVEN
    from galaxysim.ai.simple import take_turn

    # Everything free is inside the raid it must keep at home, so there is
    # nothing spare -- even though the cordon is losing and one hull would win.
    engine, universe_id, raider_id, rival_id = _siege_underway(
        cordon=1.0,
        defenders=1.5,
        reserve=DRIVEN.raid_strength - 1.0,
        strip_starting_fleet=True,
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        target = session.scalar(select(Colony).where(Colony.name == "Contested"))
        take_turn(session, universe, session.get(Civ, raider_id))
        session.flush()

        assert not _reinforcements(session, raider_id, target.world.system)


def test_it_does_not_scrap_the_second_wave():
    """The reserve and the surplus were the same ships.

    ``_maybe_scrap`` counts strength above the garrison as surplus and breaks up
    the smallest of it -- which, during a war, is exactly the hull
    ``_maybe_reinforce`` is holding for the next wave. A civilization at war and
    still paying its crews has a reserve, not a surplus.
    """
    from galaxysim.model.entities import Intent, IntentKind

    def scrapped(session, civ_id) -> list:
        return session.scalars(
            select(Intent).where(
                Intent.civ_id == civ_id, Intent.kind == IntentKind.DECOMMISSION.value
            )
        ).all()

    # ``_maybe_scrap`` on its own, both ways round -- rather than a whole turn,
    # because a turn with no war on sends the same hull off to start one, and a
    # ship claimed for a raid was never scrappable anyway. What is under test is
    # this decision's own rule.
    #
    # A cordon that is winning, so nothing is reinforced and the reserve is idle
    # at a colony -- which is exactly when this used to sell it.
    for at_war in (True, False):
        engine, universe_id, raider_id, _ = _siege_underway(
            cordon=40.0, defenders=2.0, reserve=40.0, reserve_offset_ly=0.0
        )
        with open_session(engine) as session:
            universe = session.get(Universe, universe_id)
            raider = session.get(Civ, raider_id)
            pending = simple._pending_by_kind(session, raider)
            if not at_war:
                pending.pop("attack", None)

            simple._maybe_scrap(simple._Turn(session, universe, raider), pending)
            session.flush()

            if at_war:
                assert not scrapped(session, raider_id), (
                    "it sold the second wave while the war was on"
                )
            else:
                # The other half: with no war on, the same hull is surplus and
                # does get broken up -- so the guard is what is doing the work
                # here rather than the fixture.
                assert scrapped(session, raider_id), (
                    "in peacetime a hull above the garrison is still surplus"
                )


def test_a_bad_week_does_not_cost_a_civilization_its_navy():
    """Insolvency used to be a second licence to scrap, and it was a spiral.

    The argument for it was good: a hull you cannot pay for is lost either way,
    and scrapping returns a third of the materials while desertion returns
    nothing. Measured over 120 days, what it actually did was liquidate. Any
    shortfall at all made every docked warship a candidate at one hull per
    decision -- hourly, for a driven opponent -- so five of eight civilizations
    lost most of their navy to a dip. One shed 45.6 strength to desertion *and*
    sixteen hulls to scrapping inside a fortnight, then rebuilt twelve points
    eight days later, which is what says the shortage was weather rather than
    climate.

    The salvage does not even answer the shortage: upkeep is fuel and alloys,
    and breaking a hull returns alloys, steel and electronics. A civ short of
    fuel sells its fleet and is still short of fuel.

    So surplus is the only trigger now, and this is the case that used to be the
    exception: under the garrison line, unpaid, and nothing to sell.
    """
    from galaxysim.model.entities import Intent, IntentKind

    from galaxysim.ai.doctrine import DRIVEN

    def sold(session, civ_id) -> list:
        return session.scalars(
            select(Intent).where(
                Intent.civ_id == civ_id, Intent.kind == IntentKind.DECOMMISSION.value
            )
        ).all()

    # One pure warship, docked at the colony, and under the line: a driven civ
    # with one colony wants 2.5 points and this is 1.0 of it. The reserve fleet
    # is the pure warship -- the line squadron carries a pod, so it was never a
    # scrapping candidate, which is what made the first draft of this test pass
    # for the wrong reason.
    assert DRIVEN.garrison_per_colony > 1.0, "the fixture needs to sit under the line"
    for paid in (0.5, 1.0):
        engine, universe_id, raider_id, _ = _siege_underway(
            cordon=1.0,
            defenders=0.0,
            reserve=1.0,
            reserve_offset_ly=0.0,
            strip_starting_fleet=True,
        )
        with open_session(engine) as session:
            universe = session.get(Universe, universe_id)
            civ = session.get(Civ, raider_id)
            civ.upkeep_paid = paid
            session.flush()

            pending = simple._pending_by_kind(session, civ)
            pending.pop("attack", None)  # peacetime, so only solvency is in play
            simple._maybe_scrap(simple._Turn(session, universe, civ), pending)
            session.flush()

            assert not sold(session, raider_id), (
                f"at upkeep_paid={paid} it sold a hull it was trying to keep"
            )


def test_a_standing_route_grows_with_the_world_it_feeds():
    """A number that was right when it was written and wrong ever after.

    Route levels are decided when the order is placed, and an outpost is at its
    smallest exactly then -- fifty thousand settlers. Within weeks it is a
    quarter of a million people drinking four times as much, while the order
    goes on asking for what suited the landing party.

    Measured over sixty days, that stale number was the whole of the problem
    once routes learned to top up rather than dump: destinations sat at about
    seventy hours of cover against a round trip of about the same, so each ran
    down to nothing just as its next delivery finished. Twenty-one of sixty-three
    were under a day of water and two were at zero, with every freighter visibly
    loading, flying and unloading. Nothing was stuck; the target was simply out
    of date.
    """
    from galaxysim.ai.simple import _route_levels, take_turn
    from galaxysim.engine import intents as intent_api
    from galaxysim.materials import WATER
    from galaxysim.model.entities import Fleet, Intent, IntentKind

    engine = create_engine_for("sqlite://")
    universe_id = new_universe(
        engine, seed=833, civs=("Terrans",), seconds_per_tick=3600
    )

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        civ.is_ai = True
        home = session.scalar(
            select(Colony).where(Colony.civ_id == civ.id).order_by(Colony.id)
        )
        world = _free_world(session)
        world.habitability = 0.0
        outpost = Colony(
            world=world,
            civ_id=civ.id,
            name="Deep Rock",
            population=50_000.0,
            infrastructure=1.0,
            founded_tick=0,
            stockpile={},
            labor=dict(home.labor),
        )
        session.add(outpost)
        session.add(
            Fleet(
                universe_id=universe_id,
                civ_id=civ.id,
                name="Hauler",
                strength=0.5,
                colony_pods=0,
                speed_ly_per_hour=1.0,
                cargo={},
                cargo_capacity=50_000.0,
                x=home.world.system.x,
                y=home.world.system.y,
                z=home.world.system.z,
            )
        )
        session.flush()
        intent_api.supply_route(
            session, civ, session.scalars(select(Fleet)).first().id,
            home.id, outpost.id, _route_levels(outpost),
        )
        session.flush()
        opening = float(
            session.scalar(
                select(Intent).where(Intent.kind == IntentKind.SUPPLY_ROUTE.value)
            ).payload["manifest"][WATER]
        )
        outpost_id = outpost.id

    # The outpost fills out, as every outpost does.
    with open_session(engine) as session:
        session.get(Colony, outpost_id).population = 250_000.0

    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        take_turn(session, universe, civ_by_name(session, universe_id, "Terrans"))
        session.flush()
        now = float(
            session.scalar(
                select(Intent).where(Intent.kind == IntentKind.SUPPLY_ROUTE.value)
            ).payload["manifest"][WATER]
        )

    assert now > opening * 2, (
        f"the world it feeds grew fivefold and the order still asks for "
        f"{now:,.0f} against the original {opening:,.0f}"
    )


def test_the_player_and_the_opponent_see_the_same_galaxy():
    """The rule this whole module opens with, checked instead of asserted.

    "Reads only what a human could read" was not true for a while, and no test
    caught it. The guard above checks what a doctrine may *contain* and whether
    the survey is read — both about things the AI might be handed. This
    asymmetry was the opposite shape: the AI walked the star charts with every
    world's owner attached and every fleet's position and strength, and the
    interface had no way to show a player any of it. Nothing was granted; the
    other side was simply never given the same view.

    So both now read one function, and this is what says they still do.
    """
    from galaxysim.ai.simple import _Turn
    from galaxysim.engine.resolvers import queries

    engine = create_engine_for("sqlite://")
    universe_id = new_universe(
        engine, seed=8080, civs=("Player", "AI-1", "AI-2"), seconds_per_tick=3600
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        opponent = civ_by_name(session, universe_id, "AI-1")
        opponent.is_ai = True
        session.flush()

        turn = _Turn(session, universe, opponent)
        # What the opponent walks when it goes looking for somewhere to attack.
        theirs = {c.id for c in queries.visible_rivals(turn.systems, opponent.id)}
        # What the interface offers a player standing in the same galaxy.
        charted = queries.charted_systems(session, universe_id)
        mine = {c.id for c in queries.visible_rivals(charted, opponent.id)}

        assert theirs == mine, (
            "the opponent and the interface are looking at different galaxies; "
            "whichever one sees more is playing with privileged information"
        )
        assert theirs, "the fixture needs a rival colony to be visible at all"


# --- a starving fleet has somewhere to go ------------------------------------


def _move_orders(session, civ_id: int, fleet_id: int) -> list:
    """Move orders this civ has queued for one particular ship."""
    from galaxysim.model.entities import Intent, IntentKind

    return [
        intent
        for intent in session.scalars(
            select(Intent).where(
                Intent.civ_id == civ_id, Intent.kind == IntentKind.MOVE_FLEET.value
            )
        )
        if intent.payload.get("fleet_id") == fleet_id
    ]


def _stranded_fleet(*, supplied_where_it_stands: bool):
    """One civ, a stocked capital, and a warship far from any warehouse.

    ``supplied_where_it_stands`` parks the ship on the capital instead, which is
    the case the rule must leave alone.
    """
    from galaxysim.engine.resolvers.production import SUPPLY_RANGE_LY
    from galaxysim.materials import FLEET_UPKEEP_PER_STRENGTH
    from galaxysim.model.entities import Colony, Fleet

    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=606, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        civ.is_ai = True
        civ.difficulty = "driven"
        home = session.scalar(
            select(Colony).where(Colony.civ_id == civ.id).order_by(Colony.id)
        )
        # Enough to cover the fleet's hourly bill many times over. Not a token
        # amount: the rule compares against what the ship is actually owed, so a
        # stingy fixture would refuse for the right reason and prove nothing.
        home.stockpile = {material: 1e9 for material in FLEET_UPKEEP_PER_STRENGTH}
        system = home.world.system

        fleet = session.scalar(select(Fleet).where(Fleet.civ_id == civ.id).order_by(Fleet.id))
        fleet.colony_pods = 0
        fleet.cargo = {}
        fleet.cargo_capacity = 0.0
        fleet.strength = 10.0
        if not supplied_where_it_stands:
            # Well past supply range of everything this civ owns, which is
            # exactly where a scout ends up and where nothing could ever pay it.
            fleet.x = system.x + SUPPLY_RANGE_LY * 3.0
            fleet.y = system.y
            fleet.z = system.z
        civ_id, home_system_id, fleet_id = civ.id, system.id, fleet.id

    return engine, universe_id, civ_id, home_system_id, fleet_id


def test_a_fleet_dying_for_want_of_a_warehouse_is_brought_home():
    """Nothing in the AI reacted to a fleet going unsupplied. At all.

    ``_maybe_scout`` was the only decision that ever moved an idle warship, and
    it moves them to *uncharted* systems -- places with no colony by definition.
    ``_maybe_scrap`` needs a hull docked at a colony and surplus to garrison,
    which a ship dying in empty space is neither. Reinforce, raid and annex all
    send ships out. So a fleet that ended up somewhere with no supply had no way
    back and bled until it was gone.

    Measured, that was the whole late-run navy collapse: five fleets in two
    hundred and fifty with zero percent of their bill in reach, producing fifteen
    thousand shortfall events by day 120 inside empires running eight to thirty
    times solvent. Not an economy that could not carry a navy -- a navy with no
    retreat, which is a thing no player would ever suffer.
    """
    from galaxysim.ai.simple import take_turn
    from galaxysim.model.entities import Civ, StarSystem, Universe

    engine, universe_id, civ_id, home_system_id, fleet_id = _stranded_fleet(
        supplied_where_it_stands=False
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        take_turn(session, universe, session.get(Civ, civ_id))
        session.flush()
        home = session.get(StarSystem, home_system_id)
        orders = _move_orders(session, civ_id, fleet_id)
        assert orders, "a starving fleet was left to die where it stood"
        assert orders[0].payload.get("x") == pytest.approx(home.x), (
            "it was ordered somewhere, but not to the one place that could pay it"
        )


def test_a_fleet_holding_a_blockade_is_not_withdrawn():
    """The exclusion that matters more than the rule.

    A cordon deep in somebody else's space is *supposed* to starve -- that is how
    a siege ends, and it is the cost of projecting force. A withdrawal rule that
    could not tell a blockade from a stranded scout would dissolve every war the
    civilization is prosecuting and read, on a fleet-strength table, as a triumph.
    """
    from galaxysim.ai import simple
    from galaxysim.ai.simple import take_turn
    from galaxysim.model.entities import Civ, Universe

    # The *starving* case, which is the only one that tests anything. A cordon
    # inside supply range is fed, so withdrawal would not fire on it whether the
    # exclusion existed or not -- the first draft of this test asserted exactly
    # that and passed against an engine with the exclusion deleted.
    engine, universe_id, civ_id, _, fleet_id = _stranded_fleet(
        supplied_where_it_stands=False
    )

    besieging = simple._besieging
    simple._besieging = lambda turn, pending: {fleet_id}
    try:
        with open_session(engine) as session:
            universe = session.get(Universe, universe_id)
            take_turn(session, universe, session.get(Civ, civ_id))
            session.flush()
            assert not _move_orders(session, civ_id, fleet_id), (
                "a fleet holding a cordon was recalled for being unsupplied; a "
                "blockade deep in somebody else's space is meant to starve, and "
                "that is how a siege ends"
            )
    finally:
        simple._besieging = besieging


def test_a_supplied_fleet_is_left_where_it_is():
    """The guard against the rule churning a whole navy every turn.

    A withdrawal that fires on ships which are perfectly well fed would shuttle
    the fleet around the empire for ever, which costs nothing visible on a
    strength table and quietly stops the AI doing anything else with them.
    """
    from galaxysim.ai.simple import take_turn
    from galaxysim.model.entities import Civ, Universe

    from galaxysim.model.entities import StarSystem

    engine, universe_id, civ_id, home_system_id, fleet_id = _stranded_fleet(
        supplied_where_it_stands=True
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        take_turn(session, universe, session.get(Civ, civ_id))
        session.flush()
        home = session.get(StarSystem, home_system_id)
        # The ship may still be sent scouting -- that is a different decision,
        # and it goes to an *uncharted* system. A withdrawal is the only thing
        # that would order it to the system it is already parked in, so that is
        # what isolates the rule under test.
        recalls = [
            intent
            for intent in _move_orders(session, civ_id, fleet_id)
            if intent.payload.get("x") == pytest.approx(home.x)
            and intent.payload.get("y") == pytest.approx(home.y)
        ]
        assert not recalls, (
            "a fleet sitting on a full warehouse was recalled to the warehouse "
            "it is already sitting on; the rule is firing on ships that are fine"
        )


def test_every_order_the_ai_issues_says_why():
    """A dispatch that does not record its reason cannot be audited.

    Twelve attempts at the late-run navy collapse asked *what* was killing
    fleets and none could ask *which decision sent them there*, because a move
    order recorded a destination and nothing else. Scouting, raiding,
    reinforcing, annexing, expanding and withdrawing all produced the same
    anonymous row, so the biggest question about this AI -- where does it send a
    hull that then starves -- was unanswerable from its own history.

    Asserted across every order the AI issues rather than site by site: a
    seventh dispatch added later without a reason is exactly the case a
    per-function test would miss, and this module already carries a scar from
    filters written inline at five call sites with no name between them.
    """
    from galaxysim.bootstrap import add_civ, create_universe
    from galaxysim.engine.tick import resolve_tick
    from galaxysim.model.base import new_session, transaction
    from galaxysim.model.entities import Intent, IntentKind, UniverseMode

    engine = create_engine_for("sqlite://")
    universe_id = create_universe(
        engine,
        "Why",
        seed=99,
        seconds_per_tick=3600,
        mode=UniverseMode.SOLO,
        region="arm",
        difficulty="driven",
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        for index in range(3):
            add_civ(session, universe, f"AI-{index + 1}", is_ai=True)

    session = new_session(engine)
    universe = session.get(Universe, universe_id)
    for _ in range(24 * 3):
        with transaction(session):
            simple.take_all_turns(session, universe)
            session.flush()
            resolve_tick(session, universe)

    orders = session.scalars(
        select(Intent).where(Intent.kind == IntentKind.MOVE_FLEET.value)
    ).all()
    assert orders, "the fixture produced no movement at all, so it proves nothing"
    unlabelled = [order for order in orders if not order.payload.get("reason")]
    assert not unlabelled, (
        f"{len(unlabelled)} of {len(orders)} orders the AI issued do not say why; "
        "a hull that starves under one of these cannot be traced to the decision "
        "that sent it"
    )


def test_supply_is_whether_anything_in_reach_can_pay_not_whether_a_colony_is_near():
    """``_is_unsupplied`` is the one place that answers this, so it must answer it.

    ``_maybe_withdraw`` asks it before recalling a starving hull, and it has to
    ask the same question the biller does: pool the colonies within
    ``SUPPLY_RANGE_LY`` and compare against the whole upkeep basket. The cheaper
    question -- "is there a colony nearby" -- gives a different answer, and
    answering that one instead is what let a stranded fleet read as supplied: a
    two-week-old outpost is a colony in range that makes *none* of the basket,
    so a hull parked over one is as unfed as a hull in empty space.
    """
    from galaxysim.ai.simple import _hourly_bill, _is_unsupplied
    from galaxysim.engine.resolvers import queries
    from galaxysim.model.entities import Fleet
    from galaxysim.model.entities import Universe as U

    # Far from everything, and at home with a full warehouse: the easy poles.
    for stranded, expected in ((True, True), (False, False)):
        engine, universe_id, civ_id, _, fleet_id = _stranded_fleet(
            supplied_where_it_stands=not stranded
        )
        with open_session(engine) as session:
            turn = simple._Turn(session, session.get(U, universe_id), session.get(Civ, civ_id))
            fleet = session.get(Fleet, fleet_id)
            assert (
                _is_unsupplied(turn.colonies, fleet.position, _hourly_bill(fleet)) is expected
            ), f"a {'stranded' if stranded else 'supplied'} hull read the wrong way round"

    # The case that separates the two questions, and the only one that can: a
    # hull sitting *on* a colony that holds nothing.
    engine, universe_id, civ_id, _, fleet_id = _stranded_fleet(supplied_where_it_stands=True)
    with open_session(engine) as session:
        empty = session.scalar(select(Colony).where(Colony.civ_id == civ_id).order_by(Colony.id))
        empty.stockpile = {}
        session.flush()
        turn = simple._Turn(session, session.get(U, universe_id), session.get(Civ, civ_id))
        fleet = session.get(Fleet, fleet_id)
        assert queries.sorted_by_distance(
            turn.colonies, fleet.position, within_ly=SUPPLY_RANGE_LY
        ), "the fixture must put a colony in range, or it proves nothing"
        assert _is_unsupplied(turn.colonies, fleet.position, _hourly_bill(fleet)), (
            "a hull sitting on an empty warehouse read as supplied; this is "
            "asking whether a colony is near rather than whether it can pay"
        )
