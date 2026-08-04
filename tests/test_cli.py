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

import re

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from galaxysim.cli.main import app
from galaxysim.core.units import format_count
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

    for args in (
        ("status",),
        ("orders",),
        ("colony", str(colony_id)),
        ("research",),
        ("empire",),
    ):
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


def test_the_empire_view_says_where_the_industry_is(game):
    """The question the game could not answer: is expanding paying off?

    A capital opens with 55% of its world's industry already built and every
    world settled afterwards starts at zero, so an empire's centre of gravity
    barely moves for months. Nothing displayed that. ``status`` lists what you
    own, ``colony`` opens one world at a time, and working out which of them
    actually carries the economy meant reading every one and doing the
    arithmetic by hand.
    """
    runner, engine = game
    with open_session(engine) as session:
        universe = session.scalar(select(Universe))
        civ = civ_by_name(session, universe.id, "Humanity")
        home = home_colony(session, civ)
        # A second world with a tenth of the capital's industrial workforce.
        spare = session.scalars(
            select(World).where(World.colony == None).order_by(World.id)  # noqa: E711
        ).first()
        session.add(
            Colony(
                world=spare,
                civ_id=civ.id,
                name="Smallholding",
                population=home.population / 10.0,
                founded_tick=0,
                stockpile={},
                labor=dict(home.labor),
            )
        )
        capital_name = home.name

    output = _run(runner, "empire")

    # Asserted on the summary rather than the table: Rich truncates cells to the
    # terminal it is rendering into, so a column's *contents* are a fact about
    # the test's width and not about the game.
    assert capital_name.split()[0] in output, output
    assert "the other 1 colony holds" in output, output

    # The line the command exists for, and the direction it has to point: the
    # capital carries the empire, which is the fact being judged.
    share = int(re.search(r"is (\d+)% of your industry", output).group(1))
    assert 80 <= share <= 100, (
        f"the capital has ten times the industrial workforce; it read as {share}%"
    )


def test_the_empire_view_quotes_the_engine_rather_than_its_own_arithmetic(game):
    """A readout with a second implementation of a number drifts from the first.

    This is the lesson the terraforming readout taught, at cost: it decided what
    a civilization could afford by looking at one warehouse while the engine paid
    out of every warehouse, so it announced "short of Electronics" for projects
    that were ready to run. The industry figure here is the same
    ``construction_per_hour`` the opponent plans against and the terraforming
    progress readout quotes.
    """
    from galaxysim.engine.resolvers.terraform import construction_per_hour

    runner, engine = game
    with open_session(engine) as session:
        universe = session.scalar(select(Universe))
        civ = civ_by_name(session, universe.id, "Humanity")
        expected = construction_per_hour(home_colony(session, civ))

    output = _run(runner, "empire")
    assert format_count(expected) in output, (
        f"expected the engine's own figure ({format_count(expected)}) in:\n{output}"
    )
    # One colony is the whole empire, and the summary has to say so rather than
    # dividing by a total that does not include it.
    assert "100%" in output


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
