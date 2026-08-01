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
from galaxysim.bootstrap import add_civ, create_universe
from galaxysim.colony.buildings import BUILDING_TYPES, building_type
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
from galaxysim.core.resources import VOLATILES
from galaxysim.core.space import Vec3, distance
from galaxysim.engine import intents
from galaxysim.engine.rates import DEFAULT_RATES, Cadence
from galaxysim.engine.resolvers import queries
from galaxysim.engine.resolvers.governor import POLICIES
from galaxysim.engine.resolvers.production import colony_effects, effective_habitability
from galaxysim.cli.survey_view import format_count
from galaxysim.cli.survey_view import render as render_survey
from galaxysim.worldgen.serialize import survey_from_json
from galaxysim.engine.tick import resolve_tick
from galaxysim.model.base import create_engine_for, open_session
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
    systems: int = typer.Option(24, "--systems", help="Size of the starting region."),
    species: str = typer.Option(
        "", "--species", help="Describe your species. Shapes generation in a later build step."
    ),
) -> None:
    """Create a new solo universe."""
    path = _db_url()
    if path.startswith("sqlite:///") and Path(path[10:]).exists():
        console.print(
            f"[red]{path[10:]} already exists.[/red] Delete it or set GALAXYSIM_DB "
            "to start somewhere else."
        )
        raise typer.Exit(1)

    engine = _engine()
    # Each civ needs its own starting system, so the region must comfortably
    # outnumber the players in it.
    required = (ai + 1) * 2
    universe_id = create_universe(
        engine,
        name,
        seed=seed,
        seconds_per_tick=minutes_per_tick * 60,
        mode=UniverseMode.SOLO,
        system_count=max(systems, required),
    )

    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        add_civ(session, universe, civ, species_description=species)
        for index in range(ai):
            add_civ(session, universe, f"AI-{index + 1}", is_ai=True)

    console.print(f"[green]Created[/green] [bold]{name}[/bold] with {ai} AI opponents.")
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
        stock = ", ".join(f"{k} {v:,.0f}" for k, v in sorted(totals.items())) or "nothing"
        console.print(
            f"Held across all colonies: {stock}\n"
            f"Research: {civ.research_points:,.1f} banked, "
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
                    f"{k} {v:,.0f}" for k, v in sorted(colony.stockpile.items()) if v >= 1
                )
                table.add_row(
                    str(colony.id),
                    colony.name,
                    colony.world.name,
                    colony.world.world_type,
                    f"{colony.population:,.1f}",
                    f"{colony.infrastructure:.1f}",
                    held or "[dim]empty[/dim]",
                )
            console.print(table)

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

        table = Table("id", "system", "class", "ly away", "worlds", title="Known systems")
        for system in rows[:limit]:
            worlds = sorted(system.worlds, key=lambda w: w.id)
            summary = ", ".join(
                f"[{'green' if w.colony is None else 'yellow'}]{w.id}:{w.world_type}"
                f"{'' if w.habitability > 0 else '*'}[/]"
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
            detail = intent.result or ", ".join(f"{k}={v}" for k, v in sorted(intent.payload.items()))
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

        manifest = ", ".join(f"{v:,.0f} {k}" for k, v in sorted(verdict.cost.items()))
        console.print(
            f"[bold]{world.name}[/bold] ({world.world_type}, "
            f"habitability {world.habitability:.2f}, {world.slots} slots)\n"
            f"Expedition: {colonists:.0f} colonists, {equipment:.0f} equipment, "
            f"{stores:.0f} stores\n"
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
            console.print(
                f"[dim]Settled: {world.colony.name}, "
                f"population {format_count(world.colony.population)}[/dim]"
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
            f"Population {colony.population:,.1f} - infrastructure "
            f"{colony.infrastructure:.2f} - habitability {world.habitability:.2f}"
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
        if need > 0:
            per_unit = DEFAULT_RATES.volatiles_per_life_support * (
                1.0 - effects.life_support_recycling
            )
            burn = need * per_unit
            stock = colony.stockpile.get(VOLATILES, 0.0)
            hours = (stock / burn) if burn > 0 else None
            colour = "red" if hours is not None and hours < 48 else "yellow"
            console.print(
                f"[{colour}]Life support: burning {burn:.2f} volatiles/hour"
                + (f" - about {hours / 24:.1f} days of air left" if hours is not None else "")
                + "[/]"
            )
        else:
            console.print("[green]Life support: not required.[/green]")

        allocation = normalize(colony.labor)
        table = Table("sector", "share", "workers", title="Labor")
        for sector in SECTORS:
            table.add_row(
                sector,
                f"{allocation[sector] * 100:.0f}%",
                f"{colony.population * allocation[sector]:,.1f}",
            )
        console.print(table)

        table = Table("building", "state", title=f"Buildings ({len(colony.buildings)}/{world.slots})")
        for building in sorted(colony.buildings, key=lambda b: b.id):
            spec = building_type(building.kind)
            table.add_row(
                spec.name,
                "complete" if building.is_complete else f"{building.work_remaining:.1f} work left",
            )
        if not colony.buildings:
            table.add_row("[dim]none[/dim]", "")
        console.print(table)

        stock = ", ".join(f"{k} {v:,.1f}" for k, v in sorted(colony.stockpile.items()))
        console.print(f"Stockpile: {stock or '[dim]empty[/dim]'}")


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
    """Build a structure. Slots are limited by the world."""
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
        ..., "--carry", help="Cargo as resource:amount, repeatable. e.g. volatiles:50"
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
def research() -> None:
    """Begin a standing research programme; it runs while you are away."""
    with open_session(_engine()) as session:
        universe = _require_universe(session)
        civ = _require_player(session, universe)
        intents.research(session, civ)
        console.print("[green]Research programme underway.[/green]")


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
        system_count=max(40, ai * 6),
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        for index in range(ai):
            add_civ(session, universe, f"AI-{index + 1}", is_ai=True)

    cadence = Cadence(minutes_per_tick * 60)
    ticks_per_day = cadence.ticks_for_hours(24)

    table = Table(
        "day", "colonies", "pop", "fleet str", "techs", "spread", title="Pacing soak (medians)"
    )
    started = time.perf_counter()
    total_ticks = 0

    for day in range(1, days + 1):
        for _ in range(ticks_per_day):
            with open_session(engine) as session:
                universe = session.get(Universe, universe_id)
                take_all_turns(session, universe)
                session.flush()
                resolve_tick(session, universe)
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

            table.add_row(
                str(day),
                f"{_median(colonies):.0f}",
                f"{_median(pops):,.0f}",
                f"{_median(strengths):.0f}",
                f"{_median(techs):.0f}",
                f"{spread:.2f}x" if spread else "-",
            )

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
