"""The player's half of the game.

Everything else in this suite tests what the engine does. Nothing tested what a
player is *told*, and that turned out to matter: the terraforming readout decided
what a civilization could afford by looking at one colony's warehouse while the
engine pays out of every warehouse the civilization owns. So it announced "short
of Electronics" for projects that were ready to run, and a player who believed it
would never order the thing the whole mechanic exists for.

An interface that lies is a bug like any other. These tests treat it as one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from galaxysim.cli.main import app
from galaxysim.materials.catalogue import ALLOYS, ELECTRONICS, STEEL
from galaxysim.model.base import create_engine_for, open_session
from galaxysim.model.entities import Civ, Colony, Universe, World
from galaxysim.terraform.projects import project as project_spec
from tests.conftest import civ_by_name, home_colony


@pytest.fixture
def game(tmp_path, monkeypatch):
    """A one-civ universe in a throwaway database, driven through the CLI."""
    path = tmp_path / "cli.db"
    monkeypatch.setenv("GALAXYSIM_DB", str(path))
    runner = CliRunner()
    result = runner.invoke(app, ["new", "Test", "--civ", "Humanity", "--ai", "1", "--seed", "5150"])
    assert result.exit_code == 0, result.output
    return runner, create_engine_for(f"sqlite:///{path}")


def _run(runner, *args) -> str:
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_a_project_reads_as_ready_when_the_empire_can_pay_for_it(game):
    """The materials pool across the civilization, so the readout must too.

    Terraforming is the documented exception to local stockpiles -- ``_reach``
    explains why: work cannot cross the gap in useful time but freight can. The
    resolver pools every colony. The interface checked the one colony standing on
    the world, so an empire with the materials one system over was told it was
    short and would not place the order.
    """
    runner, engine = game
    shield = project_spec("magnetic_shield")

    with open_session(engine) as session:
        universe = session.scalar(select(Universe))
        civ = civ_by_name(session, universe.id, "Humanity")
        home = home_colony(session, civ)
        # The world being reshaped holds nothing at all...
        home.stockpile = {}
        # ...and a second colony one system over holds the whole bill.
        spare = session.scalars(
            select(World).where(World.colony == None).order_by(World.id)  # noqa: E711
        ).first()
        session.add(
            Colony(
                world=spare,
                civ_id=civ.id,
                name="Depot",
                population=1000.0,
                founded_tick=0,
                stockpile={k: v * 2 for k, v in shield.cost.items()},
                labor=dict(home.labor),
            )
        )
        colony_id = home.id

        # The distinction the bug turned on, stated so this test cannot quietly
        # stop testing it: the colony cannot pay, the civilization can, and the
        # engine spends the civilization's.
        from galaxysim.engine.resolvers import queries
        from galaxysim.materials import can_afford

        assert not can_afford(home.stockpile, shield.cost)
        assert can_afford(queries.total_stockpile(session, civ.id), shield.cost)

    output = _run(runner, "terraform", str(colony_id))
    assert "ready" in output, output
    assert "short of" not in output.split("Magnetic Shield")[1][:120], (
        "the empire holds twice the bill; nothing should read as short\n" + output
    )


def test_the_readout_says_what_the_whole_campaign_costs(game):
    """One project is not the question. Fifteen of them is.

    A player deciding whether to reshape a world is deciding about a campaign --
    what it totals, and where it ends up. Showing only what is possible this
    afternoon answers a much smaller question than the one being asked.
    """
    runner, engine = game
    with open_session(engine) as session:
        universe = session.scalar(select(Universe))
        civ = civ_by_name(session, universe.id, "Humanity")
        colony_id = home_colony(session, civ).id

    output = _run(runner, "terraform", str(colony_id))
    assert "projects to a liveable world" in output or "Nothing left worth doing" in output, output
    assert "Total" in output or "Nothing left" in output, output


def test_a_world_that_cannot_be_finished_says_so(game):
    """Two thirds of the galaxy is a trap and the interface never mentioned it.

    A hopeless world still swallows nine real projects while its albedo climbs,
    every one of which looks like progress. The player is now told before
    spending anything.
    """
    runner, engine = game
    with open_session(engine) as session:
        hopeless = session.scalars(
            select(World).where(
                World.terraform_finishable.is_(False), World.terraform_next != ""
            )
        ).first()
        assert hopeless is not None, "the seeded galaxy has no unfinishable world"
        world_id = hopeless.id

    output = _run(runner, "planet", str(world_id))
    assert "cannot be terraformed" in output, output


def test_the_ordinary_readouts_run(game):
    """Coverage the CLI simply did not have.

    Not a deep assertion -- these commands are mostly formatting -- but every one
    of them reads live model state, and a column renamed underneath them would
    have gone unnoticed until a player hit it.
    """
    runner, engine = game
    with open_session(engine) as session:
        universe = session.scalar(select(Universe))
        civ = civ_by_name(session, universe.id, "Humanity")
        colony_id = home_colony(session, civ).id

    for args in (("status",), ("orders",), ("colony", str(colony_id)), ("research",)):
        _run(runner, *args)


def test_an_order_in_progress_reports_how_far_along_it_is(game):
    """A work figure in the tens of millions tells a player nothing.

    What they can act on is the fraction done and the day it lands, computed
    from the same pooled construction the resolver actually feeds the project.
    """
    runner, engine = game
    with open_session(engine) as session:
        universe = session.scalar(select(Universe))
        civ = civ_by_name(session, universe.id, "Humanity")
        home = home_colony(session, civ)
        home.stockpile = {ALLOYS: 1e9, ELECTRONICS: 1e9, STEEL: 1e9}
        colony_id = home.id

    _run(runner, "terraform", str(colony_id), "magnetic_shield")
    _run(runner, "tick")

    output = _run(runner, "orders")
    assert "days left" in output or "% done" in output, output


def test_a_player_can_find_out_who_else_is_out_there_and_fight_them(game):
    """Conflict was unreachable from this side of the game.

    ``attack`` takes a civilization id and nothing in the interface would tell
    you one -- while the AI, reading the same star charts, has always known who
    owns what and now picks its targets by the fleet strength standing over
    them. That asymmetry was not granted by any difficulty setting; it was in
    what the other side had never been given.
    """
    runner, engine = game

    listing = _run(runner, "civs")
    assert "AI-1" in listing, listing

    with open_session(engine) as session:
        universe = session.scalar(select(Universe))
        rival = session.scalar(
            select(Civ).where(Civ.universe_id == universe.id, Civ.is_ai.is_(True))
        )
        rival_id, rival_name = rival.id, rival.name

    # The id the listing shows is the id the order takes.
    assert str(rival_id) in listing, listing
    _run(runner, "attack", str(rival_id))

    # And once declared, the game says so -- in either direction, which it
    # never did before: a player could be invaded and never be told by whom.
    assert rival_name in _run(runner, "status")


def test_a_settled_world_says_whose_it_is(game):
    """A colour said "taken" and stopped, which is the one thing you need."""
    runner, engine = game
    with open_session(engine) as session:
        universe = session.scalar(select(Universe))
        rival = session.scalar(
            select(Civ).where(Civ.universe_id == universe.id, Civ.is_ai.is_(True))
        )
        world_id = session.scalar(
            select(Colony).where(Colony.civ_id == rival.id).order_by(Colony.id)
        ).world_id
        rival_name = rival.name

    assert rival_name in _run(runner, "planet", str(world_id))
    assert rival_name in _run(runner, "systems", "--limit", "60")
