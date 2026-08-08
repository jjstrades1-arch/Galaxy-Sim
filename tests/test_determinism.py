"""Determinism: the load-bearing property of the whole simulation.

Players are asleep for most of the ticks that affect them. If a tick cannot be
reproduced, someone who loses a fleet overnight has no way to check what
happened and no reason to trust it. Every other feature is negotiable; this is
not.
"""

from __future__ import annotations

from sqlalchemy import select

from galaxysim.core.seeds import derive_seed, rng_for, tick_seed
from galaxysim.engine import intents
from galaxysim.engine.tick import run_ticks
from galaxysim.model.base import create_engine_for, open_session
from galaxysim.model.entities import Colony, Fleet, StarSystem, Universe
from tests.conftest import civ_by_name, new_universe, snapshot


def _play_a_game(engine, seed: int = 4242, seconds_per_tick: int = 300) -> int:
    """Build a universe and queue a representative mix of orders.

    Deliberately exercises every resolver: movement, a deferred colonization,
    construction, standing research and a declared war.
    """
    universe_id = new_universe(engine, seed=seed, seconds_per_tick=seconds_per_tick)

    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        assert universe is not None
        terrans = civ_by_name(session, universe_id, "Terrans")
        vex = civ_by_name(session, universe_id, "Vex")

        intents.research(session, terrans)
        intents.research(session, vex)

        terran_fleet = session.scalar(
            select(Fleet).where(Fleet.civ_id == terrans.id).order_by(Fleet.id)
        )
        assert terran_fleet is not None

        # Pick the farthest-listed system that nobody has settled and send the
        # fleet there, queueing the colonize order in the same sitting. The
        # colonize order has to wait for arrival -- that is the async pattern
        # the whole design rests on.
        target = session.scalars(
            select(StarSystem)
            .where(StarSystem.universe_id == universe_id)
            .order_by(StarSystem.id.desc())
        ).first()
        assert target is not None

        intents.move_fleet_to_system(session, terrans, terran_fleet.id, target)
        settleable = next(
            (w for w in sorted(target.worlds, key=lambda w: w.id) if w.habitability > 0),
            None,
        )
        if settleable is not None:
            intents.colonize(session, terrans, terran_fleet.id, settleable.id)

        vex_colony = session.scalar(
            select(Colony).where(Colony.civ_id == vex.id).order_by(Colony.id)
        )
        assert vex_colony is not None
        intents.build_fleet(session, vex, vex_colony.id, 2.0)
        intents.attack(session, vex, terrans.id)

    return universe_id


def test_same_seed_same_intents_produce_identical_state():
    """Two universes built and played identically must end identical."""
    engine_a = create_engine_for("sqlite://")
    engine_b = create_engine_for("sqlite://")

    universe_a = _play_a_game(engine_a)
    universe_b = _play_a_game(engine_b)

    run_ticks(engine_a, universe_a, 40)
    run_ticks(engine_b, universe_b, 40)

    assert snapshot(engine_a, universe_a) == snapshot(engine_b, universe_b)


def test_ticks_are_reproducible_one_at_a_time():
    """Resolving 40 ticks singly matches resolving them in one batch.

    Guards against a tick reading state left over from its predecessor in
    session memory rather than from the committed database.
    """
    engine_a = create_engine_for("sqlite://")
    engine_b = create_engine_for("sqlite://")

    universe_a = _play_a_game(engine_a)
    universe_b = _play_a_game(engine_b)

    run_ticks(engine_a, universe_a, 40)
    for _ in range(40):
        run_ticks(engine_b, universe_b, 1)

    assert snapshot(engine_a, universe_a) == snapshot(engine_b, universe_b)


def test_different_seeds_diverge():
    """Guards against the above passing because nothing is random at all."""
    engine_a = create_engine_for("sqlite://")
    engine_b = create_engine_for("sqlite://")

    universe_a = _play_a_game(engine_a, seed=1)
    universe_b = _play_a_game(engine_b, seed=999)

    run_ticks(engine_a, universe_a, 10)
    run_ticks(engine_b, universe_b, 10)

    assert snapshot(engine_a, universe_a) != snapshot(engine_b, universe_b)


def test_tick_seed_is_stable_across_processes():
    """Seeds must not depend on PYTHONHASHSEED.

    CPython randomizes str hashing per process. If any seed were derived from
    :func:`hash`, a restarted server would resolve ticks differently from the
    ones it had already written.
    """
    assert tick_seed(12345, 7) == derive_seed("tick", 12345, 7)
    assert derive_seed(1, 2) != derive_seed(2, 1)
    assert derive_seed("a", "b") != derive_seed("ab")


def test_rng_streams_are_independent():
    """Separately scoped streams must not shift each other.

    Resolvers take their own streams so that adding a fleet cannot change what
    combat rolls elsewhere in the same tick.
    """
    seed = tick_seed(999, 3)
    combat_stream = [rng_for(seed, "combat", 1, 2).random() for _ in range(3)]
    rng_for(seed, "movement", 5).random()  # unrelated draw in between
    assert [rng_for(seed, "combat", 1, 2).random() for _ in range(3)] == combat_stream


def test_seed_fits_signed_bigint():
    """Seeds live in BIGINT columns, which Postgres treats as signed."""
    for i in range(200):
        assert 0 <= derive_seed("probe", i) <= (1 << 63) - 1


def test_a_universe_saved_and_reloaded_is_the_same_universe(tmp_path):
    """A soak that can be resumed is a soak that can be finished.

    This project's economy is measured by 120-day soaks, and in the environment
    they run in nothing long-running survives an idle session -- four separate
    measurements were reaped part-way, one at day 70 of 120, costing tens of
    CPU-hours and producing partial tables. The answer is to checkpoint the
    universe and pick it up again, which only works if picking it up is
    genuinely free of consequence.

    So: run some ticks, save, reload into a fresh engine, and run the rest. The
    state has to be indistinguishable from never having stopped. A checkpoint
    that quietly perturbed the run would not look like a bug, it would look like
    whatever effect was being measured -- which is the most expensive kind of
    wrong this file exists to prevent.
    """
    from galaxysim.model.base import restore as restore_db
    from galaxysim.model.base import snapshot as snapshot_db

    path = str(tmp_path / "checkpoint.db")

    straight = create_engine_for("sqlite://")
    universe_id = new_universe(straight, seed=4242, civs=("Terrans", "Vex"))
    run_ticks(straight, universe_id, 12)
    expected = snapshot(straight, universe_id)

    broken = create_engine_for("sqlite://")
    assert new_universe(broken, seed=4242, civs=("Terrans", "Vex")) == universe_id
    run_ticks(broken, universe_id, 5)
    snapshot_db(broken, path)

    resumed = create_engine_for("sqlite://")
    restore_db(resumed, path)
    with open_session(resumed) as session:
        assert session.get(Universe, universe_id).tick_number == 5, (
            "the reloaded universe is not where the saved one stopped"
        )
    run_ticks(resumed, universe_id, 7)

    assert snapshot(resumed, universe_id) == expected, (
        "a universe stopped, saved and resumed diverged from one that never "
        "stopped; every soak measured through a checkpoint would be fiction"
    )
