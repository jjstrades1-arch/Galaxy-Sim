"""Terminal client for a solo game.

This is a thin shell over :mod:`galaxysim.engine.intents`. It reads state and
queues orders; it never mutates the world itself. When the HTTP layer arrives it
replaces this file's transport and nothing else, because the engine has no idea
the CLI exists.

Solo games advance on demand -- ``galaxysim tick`` is the "end turn" button --
while a shared universe would tick on a wall clock. Same engine either way; the
only difference is who calls it.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import Engine, select

from galaxysim.ai import take_all_turns
from galaxysim.ai.doctrine import DEFAULT_DOCTRINE, DOCTRINES, LADDER
from galaxysim.bootstrap import add_civ, create_universe
from galaxysim.colony import energy
from galaxysim.colony.buildings import BUILDING_TYPES, building_type
from galaxysim.colony.industry import binding_limit, levels_in_use, max_total_levels
from galaxysim.colony.population import capacity
from galaxysim.colony.expedition import (
    DEFAULT_COLONISTS,
    DEFAULT_EQUIPMENT,
    DEFAULT_STORES,
    Loadout,
    assess,
)
from galaxysim.colony.labor import (
    EXTRACTION,
    INDUSTRY,
    LIFE_SUPPORT,
    RESEARCH,
    SECTORS,
    normalize,
)
from galaxysim.materials import (
    MATERIALS,
    RECIPES,
    WATER,
    can_afford,
    extraction_rates,
    material,
    missing_inputs,
)
from galaxysim.materials.refining import DEFAULT_PLAN, plan_for, shortfalls
from galaxysim.terraform.apply import apply_project
from galaxysim.terraform.projects import PROJECTS
from galaxysim.terraform.projects import project as project_spec
from galaxysim.engine.resolvers.terraform import unmet_requirements
from galaxysim.core.space import Vec3, distance
from galaxysim.engine import intents
from galaxysim.engine.rates import DEFAULT_RATES, Cadence
from galaxysim.engine.resolvers import queries, siege
from galaxysim.engine.resolvers.siege import BLOCKADE_RANGE_LY
from galaxysim.engine.resolvers.governor import POLICIES
from galaxysim.engine.resolvers.production import colony_effects, effective_habitability
from galaxysim.cli.survey_view import format_count
from galaxysim.cli.survey_view import render as render_survey
from galaxysim.worldgen.galaxy import REGIONS, metallicity_at, systems_near
from galaxysim.worldgen.materialize import existing_system
from galaxysim.worldgen.serialize import has_surface_water, survey_from_json
from galaxysim.engine.tick import resolve_tick
from galaxysim.model.base import create_engine_for, new_session, open_session, transaction
from galaxysim.model.entities import (
    Civ,
    Colony,
    Event,
    Fleet,
    StarSystem,
    Universe,
    UniverseMode,
    World,
)

app = typer.Typer(
    add_completion=False,
    help="Galaxy-Sim: a procedurally generated, tick-based universe.",
)
console = Console()

DEFAULT_DB = "galaxysim.db"

#: Colonies a civilization starts with. Subtracted before working out how often
#: it founds a new world, since the homeworld was not founded.
HOMEWORLDS = 1


def _db_url() -> str:
    """Database location, overridable with GALAXYSIM_DB."""
    path = os.environ.get("GALAXYSIM_DB", DEFAULT_DB)
    return path if "://" in path else f"sqlite:///{Path(path).expanduser()}"


def _engine() -> Engine:
    return create_engine_for(_db_url())


def _require_universe(session) -> Universe:
    universe = session.scalars(select(Universe).order_by(Universe.id.desc())).first()
    if universe is None:
        console.print("[red]No universe here. Run [bold]galaxysim new[/bold] first.[/red]")
        raise typer.Exit(1)
    return universe


def _require_player(session, universe: Universe) -> Civ:
    civ = session.scalar(
        select(Civ)
        .where(Civ.universe_id == universe.id, Civ.is_ai.is_(False))
        .order_by(Civ.id)
    )
    if civ is None:
        console.print("[red]This universe has no player civilization.[/red]")
        raise typer.Exit(1)
    return civ


@app.command()
def new(
    name: str = typer.Argument("Frontier", help="Name for the universe."),
    civ: str = typer.Option("Humanity", "--civ", help="Your civilization's name."),
    ai: int = typer.Option(3, "--ai", help="Number of AI opponents."),
    seed: int | None = typer.Option(None, "--seed", help="Universe seed; random if omitted."),
    minutes_per_tick: int = typer.Option(
        60, "--minutes-per-tick", help="Tick resolution. Does not change how fast anyone grows."
    ),
    region: str = typer.Option(
        "",
        "--region",
        help="Where in the galaxy everyone starts: core, arm or rim. "
        "Core is crowded and metal-rich; rim is empty and poor. "
        "Defaults to whatever the difficulty implies.",
    ),
    difficulty: str = typer.Option(
        DEFAULT_DOCTRINE.key,
        "--difficulty",
        help="How well the AI plays: " + ", ".join(d.key for d in LADDER) + ".",
    ),
    species: str = typer.Option(
        "", "--species", help="Describe your species. Shapes generation in a later build step."
    ),
) -> None:
    """Create a new solo universe."""
    if region and region not in REGIONS:
        console.print(
            f"[red]Unknown region {region!r}.[/red] Choose one of: "
            + ", ".join(sorted(REGIONS))
        )
        raise typer.Exit(1)
    if difficulty not in DOCTRINES:
        console.print(
            f"[red]Unknown difficulty {difficulty!r}.[/red] Choose one of: "
            + ", ".join(d.key for d in LADDER)
        )
        raise typer.Exit(1)
    path = _db_url()
    if path.startswith("sqlite:///") and Path(path[10:]).exists():
        console.print(
            f"[red]{path[10:]} already exists.[/red] Delete it or set GALAXYSIM_DB "
            "to start somewhere else."
        )
        raise typer.Exit(1)

    engine = _engine()
    universe_id = create_universe(
        engine,
        name,
        seed=seed,
        seconds_per_tick=minutes_per_tick * 60,
        mode=UniverseMode.SOLO,
        region=region or None,
        difficulty=difficulty,
    )

    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        add_civ(session, universe, civ, species_description=species)
        for index in range(ai):
            add_civ(session, universe, f"AI-{index + 1}", is_ai=True)

    standard = DOCTRINES[difficulty]
    with open_session(engine) as session:
        spec = REGIONS[session.get(Universe, universe_id).region]
    console.print(f"[green]Created[/green] [bold]{name}[/bold] with {ai} AI opponents.")
    console.print(f"Opponents are [bold]{standard.name}[/bold] — {standard.description}")
    console.print(f"Seated in [bold]{spec.name}[/bold] — {spec.description}")
    console.print(
        f"Ticking every {minutes_per_tick} minutes of simulated time. "
        "Run [bold]galaxysim status[/bold] to look around, "
        "[bold]galaxysim tick[/bold] to advance."
    )


@app.command()
def status() -> None:
    """Show your civilization: resources, colonies, fleets."""
    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)
        cadence = Cadence(universe.seconds_per_tick)

        elapsed_hours = universe.tick_number * cadence.hours_per_tick
        console.print(
            f"[bold]{universe.name}[/bold] - tick {universe.tick_number} "
            f"({elapsed_hours:.1f}h elapsed) - [bold]{civ.name}[/bold]"
        )

        totals = queries.total_stockpile(session, civ.id)
        stock = ", ".join(f"{k} {format_count(v)}t" for k, v in sorted(totals.items())) or "nothing"
        console.print(
            f"Held across all colonies: {stock}\n"
            f"Research: {format_count(civ.research_progress)} paid for and unspent, "
            f"{civ.techs_known} techs, {civ.research_invested:,.1f} invested"
        )
        console.print(
            "[dim]Stockpiles are local -- goods are spendable only where they sit.[/dim]"
        )

        colonies = session.scalars(
            select(Colony).where(Colony.civ_id == civ.id).order_by(Colony.id)
        ).all()
        if colonies:
            table = Table(
                "id", "colony", "world", "type", "pop", "infra", "stockpile", title="Colonies"
            )
            for colony in colonies:
                held = ", ".join(
                    f"{k} {format_count(v)}t"
                    for k, v in sorted(colony.stockpile.items())
                    if v >= 1
                )
                table.add_row(
                    str(colony.id),
                    colony.name,
                    colony.world.name,
                    colony.world.world_type,
                    format_count(colony.population),
                    f"{colony.infrastructure:.1f}",
                    held or "[dim]empty[/dim]",
                )
            console.print(table)

        # Who you are fighting, in either direction. A standing attack order is
        # the entire mechanical surface of "we are at war" and nothing displayed
        # it -- a player could be invaded without the game ever saying by whom.
        wars = _wars(session, universe, civ)
        if wars:
            names = ", ".join(
                sorted(
                    (session.get(Civ, other).name if session.get(Civ, other) else f"civ {other}")
                    for other in wars
                )
            )
            console.print(f"[red]At war with: {names}[/red]")

        # Loud and above the fleet list, because a blockade is the one thing in
        # this readout that is actively killing a colony while its owner reads.
        for besieged in (c for c in colonies if c.blockaded):
            console.print(
                f"[red]{besieged.name} is BLOCKADED -- no cargo and no settlers "
                f"can move there. {_resistance_note(besieged)}[/red]"
            )

        fleets = session.scalars(
            select(Fleet).where(Fleet.civ_id == civ.id).order_by(Fleet.id)
        ).all()
        if fleets:
            table = Table("id", "fleet", "strength", "pods", "position", "status", title="Fleets")
            for fleet in fleets:
                where = f"{fleet.x:.1f}, {fleet.y:.1f}, {fleet.z:.1f}"
                state = (
                    f"in transit, arrives t{fleet.arrival_tick}"
                    if fleet.in_transit
                    else "holding"
                )
                table.add_row(
                    str(fleet.id),
                    fleet.name,
                    f"{fleet.strength:.1f}",
                    str(fleet.colony_pods),
                    where,
                    state,
                )
            console.print(table)


@app.command()
def systems(
    limit: int = typer.Option(20, "--limit", help="How many to list."),
) -> None:
    """List known star systems, nearest first."""
    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)

        home = session.scalar(select(Colony).where(Colony.civ_id == civ.id).order_by(Colony.id))
        origin = home.world.system.position if home else Vec3(0.0, 0.0, 0.0)

        rows = session.scalars(
            select(StarSystem)
            .where(StarSystem.universe_id == universe.id)
            .order_by(StarSystem.id)
        ).all()
        rows.sort(key=lambda s: (distance(origin, s.position), s.id))

        names = {c.id: c.name for c in session.scalars(
            select(Civ).where(Civ.universe_id == universe.id)
        )}

        table = Table("id", "system", "class", "ly away", "worlds", title="Known systems")
        for system in rows[:limit]:
            worlds = sorted(system.worlds, key=lambda w: w.id)
            summary = ", ".join(
                f"[{'green' if w.colony is None else 'cyan' if w.colony.civ_id == civ.id else 'yellow'}]"
                f"{w.id}:{w.world_type}"
                f"{'' if w.habitability > 0 else '*'}"
                # Settled by *whom*. A colour said a world was taken and stopped
                # there, which is the one thing about it a player most needs.
                + (
                    ""
                    if w.colony is None
                    else " (yours)"
                    if w.colony.civ_id == civ.id
                    else f" ({names.get(w.colony.civ_id, '?')})"
                )
                + "[/]"
                for w in worlds
            )
            table.add_row(
                str(system.id),
                system.name,
                system.star_class,
                f"{distance(origin, system.position):.1f}",
                summary,
            )
        console.print(table)
        console.print("[dim]* uninhabitable; yellow already settled[/dim]")


@app.command()
def chart(
    radius: float = typer.Option(40.0, "--radius", help="Light-years to sweep."),
    limit: int = typer.Option(25, "--limit", help="How many to list."),
) -> None:
    """Every star within reach, charted or not.

    ``systems`` lists what your civilization has *been* to. This lists what is
    out there -- which is a different thing, because the galaxy is a function
    rather than a map and a star nobody has visited still exists.

    You get the star and where it is, because that is what a telescope gives
    you at forty light-years. You do not get its worlds: those appear when
    somebody arrives, and until then nobody knows whether that K dwarf has an
    ocean around it or six sterile rocks.
    """
    from galaxysim.core.seeds import rng_for
    from galaxysim.flavor.names import system_name
    from galaxysim.worldgen.star import roll_star

    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)

        home = session.scalar(select(Colony).where(Colony.civ_id == civ.id).order_by(Colony.id))
        if home is None:
            console.print("[red]You have no colonies to chart from.[/red]")
            raise typer.Exit(1)
        origin = home.world.system.position

        stubs = systems_near(universe.seed, origin, radius, limit=limit)
        table = Table(
            "ly away",
            "system",
            "class",
            "status",
            title=f"Stars within {radius:.0f} ly of {home.world.system.name}",
        )
        charted = 0
        for stub in stubs:
            row = existing_system(session, universe, stub)
            # The same seed and the same call order as materialization, so the
            # star you see through a telescope is the star you find on arrival.
            rng = rng_for(universe.seed, "system", *stub.key)
            star = roll_star(rng)
            name = row.name if row is not None else system_name(rng)
            if row is not None:
                charted += 1
                status = f"[yellow]charted #{row.id}[/yellow]"
            else:
                status = "[dim]uncharted[/dim]"
            table.add_row(
                f"{distance(origin, stub.position):.1f}",
                name,
                star.designation,
                status,
            )
        console.print(table)
        console.print(
            f"[dim]{charted} of {len(stubs)} visited. Metallicity here "
            f"[Fe/H] {metallicity_at(origin):+.2f}. Send a fleet to survey the rest.[/dim]"
        )


@app.command()
def orders() -> None:
    """Show your queued and standing orders."""
    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)
        pending = intents.pending(session, civ)

        if not pending:
            console.print("[dim]No orders queued.[/dim]")
            return

        table = Table("id", "order", "status", "detail", title="Orders")
        for intent in pending:
            # A terraforming order's payload is a work figure in the tens of
            # millions, which tells a player nothing they can act on. What they
            # want is how far along it is and when it lands.
            detail = None
            if intent.kind == "terraform":
                colony = session.get(Colony, intent.payload.get("colony_id", -1))
                if colony is not None:
                    detail = _terraform_progress(session, civ, colony)
            detail = detail or intent.result or ", ".join(
                f"{k}={v}" for k, v in sorted(intent.payload.items())
            )
            table.add_row(str(intent.id), intent.kind, intent.status, detail)
        console.print(table)


@app.command()
def move(
    fleet_id: int = typer.Argument(..., help="Fleet to move."),
    system_id: int = typer.Argument(..., help="Destination system."),
) -> None:
    """Send a fleet to a system."""
    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)

        system = session.get(StarSystem, system_id)
        if system is None or system.universe_id != universe.id:
            console.print("[red]No such system.[/red]")
            raise typer.Exit(1)

        fleet = session.get(Fleet, fleet_id)
        if fleet is None or fleet.civ_id != civ.id:
            console.print("[red]No such fleet.[/red]")
            raise typer.Exit(1)

        intents.move_fleet_to_system(session, civ, fleet_id, system)
        hours = distance(fleet.position, system.position) / fleet.speed_ly_per_hour
        console.print(
            f"[green]Ordered[/green] {fleet.name} to {system.name} "
            f"({distance(fleet.position, system.position):.1f} ly, about {hours:.1f}h)."
        )


@app.command()
def colonize(
    fleet_id: int = typer.Argument(..., help="Fleet carrying a colony pod."),
    world_id: int = typer.Argument(..., help="World to settle."),
    colonists: float = typer.Option(DEFAULT_COLONISTS, "--colonists", help="People to send."),
    equipment: float = typer.Option(
        DEFAULT_EQUIPMENT, "--equipment", help="Equipment; becomes starting infrastructure."
    ),
    stores: float = typer.Option(
        DEFAULT_STORES, "--stores", help="Life-support stores; the colony's survival clock."
    ),
    preview: bool = typer.Option(
        False, "--preview", help="Show the cost and survival estimate without ordering."
    ),
) -> None:
    """Settle a world.

    What you send is what you pay, and what the colony wakes up with. There is
    no surcharge for a hostile world -- it simply needs more stores, and running
    out of them kills the colony. Use --preview to see the arithmetic first.
    """
    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)

        world = session.get(World, world_id)
        if world is None:
            console.print("[red]No such world.[/red]")
            raise typer.Exit(1)
        if world.colony is not None:
            console.print(f"[red]{world.name} is already settled.[/red]")
            raise typer.Exit(1)

        loadout = Loadout(colonists=colonists, equipment=equipment, stores=stores)
        verdict = assess(loadout, world, DEFAULT_RATES)

        manifest = ", ".join(
            f"{format_count(v)}t {MATERIALS[k].name}" for k, v in sorted(verdict.cost.items())
        )
        console.print(
            f"[bold]{world.name}[/bold] ({world.world_type}, "
            f"habitability {world.habitability:.2f}, "
            f"{format_count(world.land_area_km2)} km2 of land)\n"
            f"Expedition: {format_count(colonists)} colonists, {equipment:.0f} equipment, "
            f"{format_count(stores)}t stores\n"
            f"Cost: {manifest}"
        )

        style = "green" if verdict.self_sufficient else "yellow"
        if verdict.survival_hours is not None and verdict.survival_hours <= 0:
            style = "red"
        console.print(f"[{style}]{verdict.summary()}[/{style}]")

        if preview:
            return

        intents.colonize(session, civ, fleet_id, world_id, loadout=loadout)
        console.print(
            "[green]Ordered.[/green] The expedition is charged to your nearest "
            "colony when it lands, and waits until the fleet arrives."
        )


@app.command()
def planet(world_id: int = typer.Argument(..., help="World to survey.")) -> None:
    """Full planetary survey: star, orbit, atmosphere, climate, geology, life.

    Most of what this prints does not drive a mechanic. It is there because a
    world should be worth reading.
    """
    with open_session(_engine()) as session:
        universe = _require_universe(session)
        world = session.get(World, world_id)
        if world is None or world.system.universe_id != universe.id:
            console.print("[red]No such world.[/red]")
            raise typer.Exit(1)
        if not world.survey:
            console.print("[red]That world has no survey data.[/red]")
            raise typer.Exit(1)

        render_survey(
            console,
            survey_from_json(world.survey),
            world.name,
            world.system.discovered_tick,
        )
        if world.colony is not None:
            # Whose it is, not merely that it is somebody's. The AI has always
            # read the owner off the star charts; naming it here is what lets a
            # player answer the same question -- and find the id that
            # ``galaxysim attack`` wants.
            owner = session.get(Civ, world.colony.civ_id)
            player = _require_player(session, universe)
            whose = (
                "yours"
                if world.colony.civ_id == player.id
                else f"[yellow]{owner.name}[/yellow] (civ {owner.id})"
                if owner
                else "unknown"
            )
            console.print(
                f"[dim]Settled: {world.colony.name} - {whose}, "
                f"population {format_count(world.colony.population)}[/dim]"
            )
        # Whether this world could ever be made somewhere people live, which is
        # the question that decides whether it is worth settling at all. Two
        # thirds of worlds cannot: a hopeless one still swallows nine real
        # projects, each looking like progress, before the ladder stalls.
        if world.terraform_next:
            console.print(
                "[green]Terraforming could finish this world.[/green]"
                if world.terraform_finishable
                else "[red]This world cannot be terraformed to habitability.[/red]"
            )


@app.command()
def colony(colony_id: int = typer.Argument(..., help="Colony to inspect.")) -> None:
    """Inspect one colony: labor, life support, buildings, stockpile."""
    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)

        colony = session.get(Colony, colony_id)
        if colony is None or colony.civ_id != civ.id:
            console.print("[red]No such colony.[/red]")
            raise typer.Exit(1)

        effects = colony_effects(colony)
        habitability = effective_habitability(colony, effects)
        world = colony.world
        mode = (
            f"governed ([bold]{colony.governor_policy}[/bold])"
            if colony.is_governed
            else "[bold]manual[/bold]"
        )

        console.print(
            f"[bold]{colony.name}[/bold] on {world.name} ({world.world_type}) - {mode}\n"
            f"Population {format_count(colony.population)} of "
            f"{format_count(capacity(world, colony.infrastructure, habitability))} "
            f"({colony.population / max(1.0, capacity(world, colony.infrastructure, habitability)):.0%} "
            f"of capacity)\n"
            f"Development {colony.development:.0%} - infrastructure "
            f"{colony.infrastructure:.1f} - standard of living "
            f"{colony.standard_of_living:.0%} - habitability {world.habitability:.2f}"
            + (
                f" (effectively {habitability:.2f} with structures)"
                if habitability > world.habitability
                else ""
            )
        )

        # Life support, the number that decides whether this place survives.
        need = colony.population * DEFAULT_RATES.life_support_per_pop_per_hour * (
            1.0 - habitability
        )
        if need <= 0:
            console.print("[green]Life support: not required.[/green]")
        elif has_surface_water(world.survey or {}):
            # Oceans. Life support is a labour cost here and nothing more, which
            # is the difference between a place and a supply liability.
            console.print(
                f"[green]Life support: {format_count(need)}t/hour, drawn from this world's "
                "own water. No supply line required.[/green]"
            )
        else:
            per_unit = DEFAULT_RATES.water_per_life_support * (
                1.0 - effects.life_support_recycling
            )
            burn = need * per_unit
            stock = colony.stockpile.get(WATER, 0.0)
            hours = (stock / burn) if burn > 0 else None
            colour = "red" if hours is not None and hours < 48 else "yellow"
            console.print(
                f"[{colour}]Life support: burning {format_count(burn)}t water/hour"
                + (f" - about {hours / 24:.1f} days of air left" if hours is not None else "")
                + "[/]"
            )

        _print_siege(colony)
        _print_terraform_progress(session, civ, colony)
        _print_power(colony, effects)

        allocation = normalize(colony.labor)
        table = Table("sector", "share", "workers", title="Labor")
        for sector in SECTORS:
            table.add_row(
                sector,
                f"{allocation[sector] * 100:.0f}%",
                format_count(colony.population * allocation[sector]),
            )
        console.print(table)

        used = levels_in_use(colony.buildings)
        ceiling = max_total_levels(colony.population, world.land_area_km2)
        bound = binding_limit(colony.population, world.land_area_km2)
        table = Table(
            "industry",
            "level",
            "state",
            title=f"Industry ({used}/{ceiling} levels -- limited by {bound})",
        )
        for building in sorted(colony.buildings, key=lambda b: b.id):
            spec = building_type(building.kind)
            table.add_row(
                spec.name,
                str(building.level),
                "running"
                if building.is_complete
                else f"expanding, {format_count(building.work_remaining)} work left",
            )
        if not colony.buildings:
            table.add_row("[dim]none[/dim]", "", "")
        console.print(table)

        # What the ground under this colony will actually give up. This is the
        # world talking, not the colony: an identical colony on a different rock
        # produces an entirely different list.
        rates = extraction_rates(survey_from_json(world.survey).deposits) if world.survey else {}
        if rates:
            table = Table("material", "t/worker-hour", "with structures", title="Extraction")
            for key, rate in sorted(rates.items(), key=lambda kv: -kv[1]):
                bonus = effects.resource(key)
                table.add_row(
                    MATERIALS[key].name,
                    f"{rate:,.3f}",
                    f"{rate * bonus:,.3f}" + (" [green]+[/green]" if bonus > 1.0 else ""),
                )
            console.print(table)
        else:
            console.print("[dim]Nothing in this crust is worth the shaft.[/dim]")

        # And what industry is doing with it.
        plan = colony.refining or None
        table = Table("chain", "state", title="Refining" + ("" if plan else " (automatic)"))
        for recipe, _weight in plan_for(plan):
            missing = missing_inputs(recipe, colony.stockpile)
            table.add_row(
                recipe.name,
                "[green]running[/green]"
                if not missing
                else "[yellow]short of " + ", ".join(MATERIALS[m].name for m in missing) + "[/]",
            )
        console.print(table)

        stock = ", ".join(
            f"{MATERIALS[k].name} {format_count(v)}t"
            for k, v in sorted(colony.stockpile.items())
            if k in MATERIALS and v >= 1
        )
        console.print(f"Stockpile: {stock or '[dim]empty[/dim]'}")


@app.command()
def refining(
    colony_id: int = typer.Argument(..., help="Colony to reprioritise."),
    chain: list[str] = typer.Option(
        None,
        "--chain",
        help="Chain as recipe or recipe:weight, repeatable. Omit to return to automatic.",
    ),
) -> None:
    """Choose which refining chains a colony runs, and in what proportion.

    A colony left alone works through everything it can, evenly. That keeps it
    alive and is deliberately mediocre: naming the two or three chains this
    world is actually good at will beat it, and on a world with one rich seam it
    will beat it by a lot.
    """
    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)

        target = session.get(Colony, colony_id)
        if target is None or target.civ_id != civ.id:
            console.print("[red]No such colony.[/red]")
            raise typer.Exit(1)

        if not chain:
            target.refining = {}
            console.print(
                f"[green]{target.name}:[/green] refining back on automatic "
                f"({len(DEFAULT_PLAN)} chains, evenly)."
            )
            return

        plan: dict[str, float] = {}
        for entry in chain:
            key, _, weight = entry.partition(":")
            if key not in RECIPES:
                console.print(
                    f"[red]No such chain {key!r}.[/red] Known: {', '.join(sorted(RECIPES))}"
                )
                raise typer.Exit(1)
            plan[key] = float(weight) if weight else 1.0

        target.refining = plan
        console.print(
            f"[green]{target.name}:[/green] "
            + ", ".join(f"{RECIPES[k].name} {v:g}" for k, v in sorted(plan.items()))
        )


@app.command()
def migrate(
    fleet_id: int = typer.Argument(..., help="Fleet to carry them."),
    origin: int = typer.Option(..., "--from", help="Colony they leave."),
    destination: int = typer.Option(..., "--to", help="Colony they settle."),
    people: float = typer.Option(..., "--people", help="How many to move."),
) -> None:
    """Ship a population from one of your colonies to another.

    Natural growth already carries a habitable colony on its own, so this is
    never required. It is how you decide *where your civilization's weight
    sits* -- a garden world with room and nobody on it, next to a capital that
    is full, is a situation only shipping fixes.

    People ride in the same hold as cargo, so a migration run is a supply run
    you did not make.
    """
    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)

        source = session.get(Colony, origin)
        target = session.get(Colony, destination)
        if source is None or target is None or civ.id not in (source.civ_id, target.civ_id):
            console.print("[red]Both ends must be your own colonies.[/red]")
            raise typer.Exit(1)
        if people > source.population:
            console.print(
                f"[red]{source.name} only has {format_count(source.population)} people.[/red]"
            )
            raise typer.Exit(1)

        intents.migrate(session, civ, fleet_id, origin, destination, people)
        headroom = capacity(target.world, target.infrastructure, target.world.habitability)
        console.print(
            f"[green]Ordered[/green] {format_count(people)} from {source.name} "
            f"to {target.name}."
        )
        console.print(
            f"[dim]{target.name} holds {format_count(target.population)} of "
            f"{format_count(headroom)}.[/dim]"
        )


@app.command()
def terraform(
    colony_id: int = typer.Argument(..., help="Colony whose world to reshape."),
    project: str = typer.Argument(None, help="Project to run. Omit to list what is possible."),
) -> None:
    """Reshape the planet a colony sits on.

    The only order that changes a world rather than what is on it, and the only
    sink big enough for a mature civilization's surplus. A dead world caps at
    whatever its habitats hold; lift habitability past the liveable line and the
    ceiling becomes the real land-and-density figure instead. That is four
    orders of magnitude, and it is what all the ore is ultimately for.
    """
    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)

        colony = session.get(Colony, colony_id)
        if colony is None or colony.civ_id != civ.id:
            console.print("[red]No such colony.[/red]")
            raise typer.Exit(1)

        survey = survey_from_json(colony.world.survey)

        if project is None:
            # What the *civilization* holds, not what this colony holds.
            #
            # Stockpiles are local and nothing may usually spend across them --
            # but terraforming is the documented exception, and the reason is on
            # ``terraform._reach``: work attenuates with distance because an
            # engineering corps far away is no help this month, while materials
            # do not, because freight goes anywhere given time. The resolver
            # pools every colony the civ owns. Checking this colony's warehouse
            # told a player they were short of electronics for a project their
            # empire could pay for twice over, and they would not order it.
            purse = queries.total_stockpile(session, civ.id)

            _print_campaign(colony, survey, purse)
            table = Table("project", "state", "cost", "work")
            for spec in PROJECTS.values():
                unmet = unmet_requirements(survey, spec)
                if unmet:
                    state = f"[yellow]needs {unmet[0]}[/]"
                elif not can_afford(purse, spec.cost):
                    short = ", ".join(
                        MATERIALS[k].name for k in sorted(shortfalls(purse, spec.cost))
                    )
                    state = f"[yellow]short of {short}[/]"
                else:
                    state = "[green]ready[/green]"
                table.add_row(
                    f"{spec.name}\n[dim]{spec.key}[/dim]",
                    state,
                    "\n".join(
                        f"{format_count(v)}t {MATERIALS[k].name}"
                        for k, v in sorted(spec.cost.items())
                    ),
                    format_count(spec.work),
                )
            console.print(table)
            console.print(
                "[dim]Order matters, and it is physics rather than a tech tree: "
                "there is no point releasing an atmosphere a world cannot hold "
                "down, or seeding life on one with no liquid water.[/dim]"
            )
            return

        try:
            spec = project_spec(project)
        except KeyError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from None

        unmet = unmet_requirements(survey, spec)
        if unmet:
            console.print(
                f"[red]{colony.world.name} has no {unmet[0]}.[/red] "
                "That has to be true before this project can start."
            )
            raise typer.Exit(1)

        intents.terraform(session, civ, colony_id, project)
        preview = apply_project(survey, spec)
        console.print(f"[green]Ordered[/green] {spec.name} on {colony.world.name}.")
        console.print(
            f"[dim]When it finishes: habitability {survey.habitability:.2f} -> "
            f"{preview.habitability:.2f}, capacity "
            f"{format_count(colony.world.carrying_capacity)} -> "
            f"{format_count(preview.carrying_capacity)}.[/dim]"
        )


@app.command()
def chains(
    material_key: str = typer.Argument(None, help="Optional: only chains making this material."),
) -> None:
    """The refining catalogue: what turns into what, and at what cost in work."""
    table = Table("chain", "inputs", "outputs", "work", "yield")
    for recipe in RECIPES.values():
        if material_key and material_key not in recipe.outputs:
            continue
        table.add_row(
            f"{recipe.name}\n[dim]{recipe.key}[/dim]",
            "\n".join(f"{a:g} {material(k).name}" for k, a in sorted(recipe.inputs.items())),
            "\n".join(f"{a:g} {material(k).name}" for k, a in sorted(recipe.outputs.items())),
            f"{recipe.work:g}",
            f"{recipe.yield_fraction * 100:.0f}%",
        )
    console.print(table)
    console.print(
        "[dim]Yield is below 100% everywhere: refining discards tailings, so a "
        "civilization can never hold more matter than it has dug up.[/dim]"
    )


@app.command()
def labor(
    colony_id: int = typer.Argument(..., help="Colony to reassign."),
    extraction: float = typer.Option(0.0, "--extraction"),
    industry: float = typer.Option(0.0, "--industry"),
    research_share: float = typer.Option(0.0, "--research"),
    life_support: float = typer.Option(0.0, "--life-support"),
) -> None:
    """Reassign a colony's population. Values are relative weights.

    Taking manual control of labor takes the colony off its governor.
    """
    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)

        colony = session.get(Colony, colony_id)
        if colony is None or colony.civ_id != civ.id:
            console.print("[red]No such colony.[/red]")
            raise typer.Exit(1)

        allocation = {
            EXTRACTION: extraction,
            INDUSTRY: industry,
            RESEARCH: research_share,
            LIFE_SUPPORT: life_support,
        }
        intents.set_labor(session, colony, allocation)
        shares = ", ".join(f"{k} {v * 100:.0f}%" for k, v in sorted(colony.labor.items()))
        console.print(f"[green]{colony.name}:[/green] {shares}")
        console.print("[dim]Now under manual control.[/dim]")


@app.command()
def structure(
    colony_id: int = typer.Argument(..., help="Colony to build at."),
    kind: str = typer.Argument("", help="Building kind; omit to list what is available."),
) -> None:
    """Build or deepen an industry. Levels are limited by people and ground."""
    if not kind:
        table = Table("kind", "name", "cost", "work", "what it does", title="Buildings")
        for spec in BUILDING_TYPES:
            table.add_row(
                spec.kind,
                spec.name,
                ", ".join(f"{v:.0f} {k}" for k, v in sorted(spec.cost.items())),
                f"{spec.work:.0f}",
                spec.description,
            )
        console.print(table)
        return

    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)

        colony = session.get(Colony, colony_id)
        if colony is None or colony.civ_id != civ.id:
            console.print("[red]No such colony.[/red]")
            raise typer.Exit(1)
        try:
            spec = building_type(kind)
        except KeyError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from None

        intents.build_structure(session, civ, colony_id, kind)
        console.print(
            f"[green]Queued[/green] a {spec.name} at {colony.name}. "
            "It is paid for in industry-work, so assign people to industry."
        )


@app.command()
def govern(
    colony_id: int = typer.Argument(..., help="Colony to hand over or take back."),
    policy: str = typer.Option("balanced", "--policy", help=f"One of: {', '.join(POLICIES)}"),
    manual: bool = typer.Option(False, "--manual", help="Take back manual control instead."),
) -> None:
    """Hand a colony to a governor, or take it back.

    A governor runs the colony to a policy using the same orders you would --
    it gets no bonus you could not get yourself. Delegate the colonies you do
    not want to think about.
    """
    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)

        colony = session.get(Colony, colony_id)
        if colony is None or colony.civ_id != civ.id:
            console.print("[red]No such colony.[/red]")
            raise typer.Exit(1)

        if not manual and policy not in POLICIES:
            console.print(f"[red]Unknown policy.[/red] Try one of: {', '.join(POLICIES)}")
            raise typer.Exit(1)

        intents.set_management(session, colony, governed=not manual, policy=policy)
        if manual:
            console.print(f"[green]{colony.name}[/green] is now under manual control.")
        else:
            console.print(f"[green]{colony.name}[/green] handed to a {policy} governor.")


@app.command()
def route(
    fleet_id: int = typer.Argument(..., help="Freighter to run the route."),
    origin: int = typer.Option(..., "--from", help="Colony to load at."),
    destination: int = typer.Option(..., "--to", help="Colony to deliver to."),
    carry: list[str] = typer.Option(
        ..., "--carry", help="Cargo as material:amount, repeatable. e.g. water:50"
    ),
) -> None:
    """Set up a standing supply route between two of your colonies.

    It runs forever: load, fly, unload, return, repeat. This is how an outpost
    on a world that cannot feed itself stays alive while you are offline.
    """
    manifest: dict[str, float] = {}
    for item in carry:
        resource, _, amount = item.partition(":")
        if not amount:
            console.print(f"[red]Bad cargo spec {item!r}.[/red] Use resource:amount.")
            raise typer.Exit(1)
        manifest[resource.strip()] = float(amount)

    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)

        try:
            intents.supply_route(session, civ, fleet_id, origin, destination, manifest)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from None

        cargo = ", ".join(f"{v:,.0f} {k}" for k, v in sorted(manifest.items()))
        console.print(f"[green]Route established:[/green] {cargo} per run.")


@app.command()
def build(
    colony_id: int = typer.Argument(..., help="Colony to build at."),
    strength: float = typer.Argument(1.0, help="Fleet strength to build."),
    pods: int = typer.Option(0, "--pods", help="Colony pods to include."),
) -> None:
    """Build a fleet. Resources are charged when work begins."""
    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)
        intents.build_fleet(session, civ, colony_id, strength, colony_pods=pods)
        console.print(f"[green]Queued[/green] a {strength:.1f}-strength fleet.")


@app.command()
def scrap(
    fleet_id: int = typer.Argument(..., help="Fleet to break up."),
) -> None:
    """Break up a fleet at the colony it is parked at, recovering materials.

    Upkeep is charged every hour a hull exists, so a ship you have no use for is
    a bill with no benefit. Scrapping returns a third of what it cost to the
    colony below it and stops the bill.
    """
    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)
        fleet = session.get(Fleet, fleet_id)
        if fleet is None or fleet.civ_id != civ.id:
            console.print("[red]No such fleet.[/red]")
            raise typer.Exit(1)
        intents.decommission_fleet(session, civ, fleet_id)
        console.print(f"[green]Queued:[/green] {fleet.name} will be broken up.")


@app.command()
def research() -> None:
    """Begin a standing research programme; it runs while you are away."""
    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)
        intents.research(session, civ)
        console.print("[green]Research programme underway.[/green]")


@app.command()
def civs() -> None:
    """Who else is out there, and what you can see of them.

    Exactly what the AI opponent reads about you -- and that symmetry is the
    point rather than a nicety. The AI has always walked the star charts with
    each world's owner attached, and it now picks the worlds it attacks by the
    fleet strength standing over them. None of that was visible from this side:
    ``attack`` wanted a civilization id and nothing in the game would tell you
    one, so the whole of conflict was unreachable by a player while the opponent
    used it freely.

    Ownership and where ships are standing is what anybody in a system can see.
    What a rival holds in its warehouses, what it is researching and what it
    intends are not here, and should not arrive here.
    """
    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)

        charted = queries.charted_systems(session, universe.id)
        rivals = queries.visible_rivals(charted, civ.id)
        if not rivals:
            console.print(
                "[dim]Nobody else has been found yet. Scout further out.[/dim]"
            )
            return

        home = session.scalar(
            select(Colony).where(Colony.civ_id == civ.id).order_by(Colony.id)
        )
        origin = home.world.system.position if home else Vec3(0.0, 0.0, 0.0)
        garrisons = [
            (fleet.position, fleet.civ_id, fleet.strength)
            for fleet in queries.fleets(session, universe.id)
            if not fleet.in_transit and fleet.strength > 0
        ]
        wars = _wars(session, universe, civ)

        seen: dict[int, list[Colony]] = {}
        for colony in rivals:
            seen.setdefault(colony.civ_id, []).append(colony)

        table = Table(
            "id", "civilization", "colonies seen", "nearest", "fleets seen", "standing",
            title="Known civilizations",
        )
        for civ_id, colonies in sorted(seen.items()):
            them = session.get(Civ, civ_id)
            nearest = min(
                colonies, key=lambda c: distance(origin, c.world.system.position)
            )
            gap = distance(origin, nearest.world.system.position)
            strength = sum(
                queries.strength_at(
                    garrisons, c.world.system.position, civ_id, BLOCKADE_RANGE_LY
                )
                for c in colonies
            )
            standing = "[red]at war[/red]" if civ_id in wars else "no quarrel"
            table.add_row(
                str(civ_id),
                them.name if them else f"civ {civ_id}",
                str(len(colonies)),
                f"{nearest.name} ({gap:.1f} ly)",
                f"{strength:.0f}",
                standing,
            )
        console.print(table)
        console.print(
            "[dim]Only what you could see: systems somebody has visited, and "
            "ships standing in them. Use the id with [bold]galaxysim attack[/bold]."
            "[/dim]"
        )


def _wars(session, universe, civ) -> set[int]:
    """Civilizations you have declared on, and any who have declared on you.

    A standing attack order is the entire mechanical surface of "we are at war",
    and until now nothing displayed it in either direction -- a player could be
    invaded without the interface ever saying by whom.
    """
    from galaxysim.model.entities import IntentKind

    at_war: set[int] = set()
    for intent in queries.active_intents(session, universe.id, IntentKind.ATTACK.value):
        target = intent.payload.get("target_civ_id")
        if intent.civ_id == civ.id and isinstance(target, int):
            at_war.add(target)
        elif target == civ.id:
            at_war.add(intent.civ_id)
    return at_war


@app.command()
def attack(
    civ_id: int = typer.Argument(..., help="Civilization to declare war on."),
) -> None:
    """Declare standing hostility. Your fleets will engage theirs on sight."""
    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)
        target = session.get(Civ, civ_id)
        if target is None or target.universe_id != universe.id:
            console.print("[red]No such civilization.[/red]")
            raise typer.Exit(1)
        intents.attack(session, civ, civ_id)
        console.print(f"[red]War declared on {target.name}.[/red]")


@app.command()
def cancel(intent_id: int = typer.Argument(..., help="Order to cancel.")) -> None:
    """Cancel an order. Resources already spent are not refunded."""
    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)
        for intent in intents.pending(session, civ):
            if intent.id == intent_id:
                intents.cancel(session, intent)
                console.print(f"[green]Cancelled[/green] order {intent_id}.")
                return
        console.print("[red]No such pending order.[/red]")
        raise typer.Exit(1)


@app.command()
def tick(
    count: int = typer.Argument(1, help="How many ticks to resolve."),
    quiet: bool = typer.Option(False, "--quiet", help="Do not print the event log."),
) -> None:
    """Advance the simulation. AI opponents queue their orders first."""
    engine = _engine()
    first_tick = None

    for _ in range(count):
        with open_session(engine) as session:
            universe = _require_universe(session)
            take_all_turns(session, universe)
            session.flush()
            result = resolve_tick(session, universe)
            first_tick = first_tick if first_tick is not None else result.tick

    with open_session(engine) as session:
        universe = _require_universe(session)
        console.print(f"[green]Advanced to tick {universe.tick_number}.[/green]")

    if not quiet and first_tick is not None:
        _print_log(engine, since=first_tick)


@app.command()
def soak(
    ai: int = typer.Option(8, "--ai", help="AI civilizations to simulate."),
    days: int = typer.Option(28, "--days", help="Days of simulated time to run."),
    minutes_per_tick: int = typer.Option(60, "--minutes-per-tick", help="Tick resolution."),
    seed: int = typer.Option(1, "--seed", help="Universe seed."),
    region: str = typer.Option("", "--region", help="core, arm or rim."),
    difficulty: str = typer.Option(
        DEFAULT_DOCTRINE.key, "--difficulty", help="How well the AI plays."
    ),
) -> None:
    """Run a throwaway universe of AI civs and chart how fast they grow.

    The pacing check from the plan: growth should look slow and compounding-
    resistant. If a civ reads as "big" within a few simulated days, the cost
    curves need steepening. Also prints per-tick wall time -- at a five-minute
    cadence a shared universe resolves ~288 ticks a day, so a tick has to stay
    cheap.

    Uses its own in-memory database and never touches your save.
    """
    import time

    engine = create_engine_for("sqlite://")
    universe_id = create_universe(
        engine,
        "Soak",
        seed=seed,
        seconds_per_tick=minutes_per_tick * 60,
        mode=UniverseMode.SOLO,
        region=region or None,
        difficulty=difficulty,
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        for index in range(ai):
            add_civ(session, universe, f"AI-{index + 1}", is_ai=True)

    cadence = Cadence(minutes_per_tick * 60)
    ticks_per_day = cadence.ticks_for_hours(24)

    table = Table(
        "day",
        "colonies",
        # The number this table exists to show, and it was the one column
        # missing. A running count only ever goes up, so a perfectly steady
        # expansion reads as a runaway one -- which is exactly how a soak got
        # reported as "out of hand" when it was settling a world every five
        # days, dead on the design target. Rate is the pace; the total is not.
        "days/world",
        "pop",
        "fleet str",
        "techs",
        "spread",
        title="Pacing soak (per civilization, medians)",
    )
    started = time.perf_counter()
    total_ticks = 0

    # One session for the whole soak, one transaction per tick -- see
    # :func:`galaxysim.engine.tick.run_ticks` for why those are different
    # questions. A session per tick re-materialized the entire universe every
    # simulated hour, which at scale was most of what a soak measured.
    ticking = new_session(engine)
    universe = ticking.get(Universe, universe_id)

    for day in range(1, days + 1):
        for _ in range(ticks_per_day):
            with transaction(ticking):
                take_all_turns(ticking, universe)
                ticking.flush()
                resolve_tick(ticking, universe)
            total_ticks += 1

        if day % max(1, days // 14) and day != days:
            continue

        with open_session(engine) as session:
            civs = session.scalars(
                select(Civ).where(Civ.universe_id == universe_id).order_by(Civ.id)
            ).all()
            colonies = [len(c.colonies) for c in civs]
            pops = [sum(col.population for col in c.colonies) for c in civs]
            strengths = [sum(f.strength for f in c.fleets) for c in civs]
            techs = [c.techs_known for c in civs]
            invested = sorted(c.research_invested for c in civs)

            # Spread between the strongest and weakest research programme. The
            # balance invariant ties power to research paid, so a widening gap
            # here is the early warning that someone is snowballing.
            spread = (invested[-1] / invested[0]) if invested and invested[0] > 0 else 0.0

            # Cumulative rather than per-interval. A median steps in whole
            # colonies, so an interval rate flickers between "-" and a number
            # and reads as noise; measured since the opening position it is the
            # plain answer to "how often does a world appear", which is the
            # question the design target is written in.
            held = _median(colonies)
            founded = held - HOMEWORLDS
            rate = f"{day / founded:.1f}" if founded > 0 else "-"

            table.add_row(
                str(day),
                f"{held:.0f}",
                rate,
                f"{_median(pops):,.0f}",
                f"{_median(strengths):.0f}",
                f"{_median(techs):.0f}",
                f"{spread:.2f}x" if spread else "-",
            )

    ticking.close()
    console.print(table)
    elapsed = time.perf_counter() - started
    console.print(
        f"[dim]{total_ticks} ticks in {elapsed:.1f}s "
        f"({elapsed / max(total_ticks, 1) * 1000:.1f} ms/tick, {ai} civs)[/dim]"
    )


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


@app.command()
def log(
    since: int = typer.Option(0, "--since", help="Earliest tick to show."),
    limit: int = typer.Option(30, "--limit", help="How many entries."),
) -> None:
    """Show what happened while you were away."""
    _print_log(_engine(), since=since, limit=limit)


def _terraform_progress(session, civ, colony) -> str | None:
    """How far a running project on this colony has got, and when it lands.

    The estimate is the resolver's own arithmetic --
    :func:`terraform.pooled_construction_per_hour` -- so the day it names is the
    day the engine is actually working toward, rather than a second guess that
    can drift away from it.

    Honest about what it cannot know: the true per-tick figure runs through the
    brownout and the refining share, so this is what the workers could do. It
    reads as "about", because it is.
    """
    from galaxysim.engine.resolvers.terraform import pooled_construction_per_hour

    running = [
        intent
        for intent in intents.pending(session, civ)
        if intent.kind == "terraform"
        and intent.payload.get("colony_id") == colony.id
        and intent.payload.get("work_remaining") is not None
    ]
    if not running:
        return None

    intent = running[0]
    spec = PROJECTS.get(str(intent.payload.get("project", "")))
    left = float(intent.payload["work_remaining"])
    done = (1.0 - left / spec.work) if spec and spec.work > 0 else 0.0

    rate = pooled_construction_per_hour(
        queries.colonies_of(session, civ.id), colony.world.system.position
    )
    name = spec.name if spec else intent.payload.get("project", "a project")
    if rate <= 0:
        return f"{name}: {done:.0%} done, and nothing is working on it"
    return f"{name}: {done:.0%} done, about {left / rate / 24:.1f} days left"


def _print_terraform_progress(session, civ, colony) -> None:
    note = _terraform_progress(session, civ, colony)
    if note:
        console.print(f"[cyan]Terraforming - {note}[/cyan]")


def _print_campaign(colony, survey, purse: dict) -> None:
    """What it would take to make this world liveable, and whether it can be.

    The interface used to show what was possible *right now* and nothing else,
    which answers the small question. A campaign is fifteen projects; the one
    worth asking is whether the whole sequence is worth a civilization's surplus,
    and that needs the total and the destination.

    And whether there is a destination at all. Two thirds of generated worlds
    cannot be finished -- their own air holds them above the growing band and an
    orbital shade saturates -- and a hopeless one still swallows nine real
    projects, every one of which looks like progress, before the ladder stalls.
    """
    from galaxysim.terraform.plan import campaign

    world = colony.world
    console.print(
        f"[bold]{world.name}[/bold] - habitability {survey.habitability:.2f}, "
        f"capacity {format_count(world.carrying_capacity)}"
    )

    steps, finished = campaign(survey)
    if not steps:
        console.print("[green]Nothing left worth doing to this world.[/green]\n")
        return

    if not world.terraform_finishable:
        console.print(
            f"[red]This world cannot be finished.[/red] {len(steps)} project(s) "
            "would still leave it uninhabitable: what stops it is physics rather "
            "than money, and no amount of industry changes that.\n"
            "[dim]Usually a thick atmosphere holding the surface above the "
            "growing band -- an orbital shade reflects only so much light, and "
            "past that the heat stays.[/dim]\n"
        )
        return

    work = sum(step.work for step in steps)
    materials: dict[str, float] = {}
    for step in steps:
        for name, amount in step.cost.items():
            materials[name] = materials.get(name, 0.0) + amount
    short = sorted(shortfalls(purse, materials))

    console.print(
        f"[bold]{len(steps)} projects[/bold] to a liveable world: "
        + " -> ".join(step.name for step in steps)
    )
    console.print(
        f"Total {format_count(work)} work and "
        + ", ".join(
            f"{format_count(v)}t {MATERIALS[k].name}" for k, v in sorted(materials.items())
        )
    )
    console.print(
        f"Ends at habitability [green]{finished.habitability:.2f}[/green], "
        f"capacity [green]{format_count(finished.carrying_capacity)}[/green]"
        + (
            f" [yellow](short of {', '.join(MATERIALS[k].name for k in short)} "
            "for the whole campaign)[/]"
            if short
            else ""
        )
        + "\n"
    )


def _resistance_note(colony) -> str:
    """How much of this colony's ability to hold out is left, in plain words.

    A fraction rather than the raw number, because the number is meaningless on
    its own -- what a player needs is "am I about to lose this place".
    """
    if colony.resistance < 0:
        return "It can still resist in full."
    standing = siege.standing_resistance(colony)
    if colony.resistance <= 0:
        return "It can no longer resist: a colony pod is all they need now."
    return f"About {colony.resistance / max(standing, 1e-9):.0%} of its resistance is left."


def _print_siege(colony) -> None:
    """Show a war being fought against this colony, if there is one."""
    if colony.blockaded:
        console.print(
            f"[red]BLOCKADED -- supply routes and migration are stopped here. "
            f"{_resistance_note(colony)}[/red]"
        )
    elif colony.resistance >= 0:
        console.print(
            f"[yellow]Recovering from a siege. {_resistance_note(colony)}[/yellow]"
        )


def _print_power(colony, effects) -> None:
    """The grid: what this colony makes, from what, and whether it is enough.

    Worth its own block rather than a number in the header, because a shortfall
    is the single most consequential thing that can be wrong with a colony --
    it multiplies mining, refining, construction, shipbuilding and terraforming
    all at once -- and because *which* route a world can use is a fact about the
    planet that a player should be able to see at a glance.
    """
    world = colony.world
    routes = {
        "solar": energy.solar_output(effects.generation.get("solar", 0.0), world.stellar_flux),
        "geothermal": energy.geothermal_output(
            effects.generation.get("geothermal", 0.0), world.tectonic_activity
        ),
    }
    fuelled = energy.fuelled_capacity(
        effects.generation.get("fission", 0.0), effects.generation.get("fusion", 0.0)
    )

    met = float(colony.power_satisfaction or 1.0)
    colour = "green" if met > 0.95 else ("yellow" if met > 0.6 else "red")
    parts = [f"{name} {format_count(output)}" for name, output in routes.items() if output > 0]
    if fuelled > 0:
        parts.append(f"fuelled capacity {format_count(fuelled)}")
    if not parts:
        parts.append("no generating plant")

    console.print(
        f"[{colour}]Power: {met:.0%} of demand met[/] - "
        + ", ".join(parts)
        + f" - baseline {format_count(energy.baseline_output(colony.population))}"
    )
    if met <= 0.95:
        console.print(
            "[dim]Industry and extraction are throttled by this. Build generating "
            "plant, or ship in fuel for the plant you have.[/dim]"
        )


def _print_log(engine: Engine, *, since: int = 0, limit: int = 30) -> None:
    with open_session(engine) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)

        events = session.scalars(
            select(Event)
            .where(
                Event.universe_id == universe.id,
                Event.tick >= since,
                # Your own events plus universe-wide ones. Other civs' business
                # is not yours to read.
                (Event.civ_id == civ.id) | (Event.civ_id.is_(None)),
            )
            .order_by(Event.id.desc())
            .limit(limit)
        ).all()

        if not events:
            console.print("[dim]Nothing to report.[/dim]")
            return

        table = Table("tick", "event", "detail", title="Dispatches")
        for event in reversed(events):
            table.add_row(str(event.tick), event.kind, event.message)
        console.print(table)


if __name__ == "__main__":
    app()
