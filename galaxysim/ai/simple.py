"""A baseline AI opponent.

The rule this file exists to honour: **the AI has no privileged access.** It
submits the same intents through the same :mod:`galaxysim.engine.intents` API a
human uses, reads only what a human could read, and waits for the same ticks.
Nothing here reaches into world state directly.

That is a deliberate constraint rather than a stylistic one. An AI with a back
door makes solo mode a different game from multiplayer, and it also stops the
AI from being usable as load generation for the real thing -- a hundred of these
running against a shared universe is the closest thing to a hundred players we
can get before there are a hundred players.

Its play is intentionally simple: research always, expand when it can, build
when it is rich. It is a sparring partner for testing the loop, not an opponent
worth fearing. Smarter behaviour is a later concern.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from galaxysim.colony.expedition import Loadout
from galaxysim.colony.labor import INDUSTRY, normalize
from galaxysim.colony.population import capacity
from galaxysim.materials import (
    FERTILISER,
    FLEET_COST_PER_STRENGTH,
    FLEET_UPKEEP_PER_STRENGTH,
    FOOD,
    FREIGHTER_COST_PER_CAPACITY,
    WATER,
    can_afford,
)
from galaxysim.core.seeds import rng_for
from galaxysim.core.space import distance
from galaxysim.colony.buildings import FLEET_CONSTRUCTION
from galaxysim.engine import intents
from galaxysim.engine.resolvers import governor, queries
from galaxysim.engine.rates import DEFAULT_RATES
from galaxysim.engine.resolvers.production import (
    DOCKING_TOLERANCE_LY,
    SUPPLY_RANGE_LY,
    colony_effects,
)
from galaxysim.worldgen.galaxy import systems_near
from galaxysim.model.entities import (
    Civ,
    Colony,
    Fleet,
    Intent,
    IntentKind,
    IntentStatus,
    StarSystem,
    Universe,
    World,
)

#: Strength the AI builds in one go, with a colony pod attached so the new fleet
#: can expand rather than only fight.
#: Colonists a landing carries, and the stores that keep them alive while a
#: supply line is arranged. Real people and real tonnes: fifty thousand settlers
#: drink fifty tonnes of water an hour on a world that supplies none, so a month
#: of independence is tens of thousands of tonnes.
SETTLERS = 50_000.0
STORES_FOR_A_MONTH = 40_000.0

#: What a standing route carries to a colony that cannot supply itself. Sized
#: against what the settlers actually consume rather than what the freighter
#: could hold, so the route tops the outpost up rather than burying it.
ROUTE_MANIFEST = {WATER: 12_000.0, FOOD: 1_500.0, FERTILISER: 500.0}

BUILD_STRENGTH = 2.0
BUILD_RESERVE = 2.0  # only build if it can afford this many such fleets

#: A freighter is a hull built with almost no weapons and a great deal of hold.
FREIGHTER_STRENGTH = 0.5
#: Manifests of slack in the hold, so one ship can run a route without the
#: destination drinking the delivery faster than the round trip.
FREIGHTER_TRIPS_OF_SLACK = 2.0

#: Settlers the AI moves in one convoy, and the people it will not draw its
#: capital below. The capital is a reservoir, not a resource to be emptied.
MIGRATION_BATCH = 500_000.0
MIGRATION_RESERVE = 1e8
TONNES_PER_SETTLER = 0.5

#: Warship strength the AI garrisons per colony it holds.
#:
#: **There is no longer a cap above this.** There used to be: a ceiling on total
#: fleet strength per billion people, which existed because upkeep was priced at
#: two hundred-thousandths of a percent of a civilization's output and therefore
#: bounded nothing at all. It was the only brake in the game, it was pinned to a
#: population that by design does not grow, and it was the entire reason AI
#: empires stopped expanding on day two and never started again.
#:
#: With ships priced as ships, "can I buy this and keep it flying" is a real
#: question with a real answer, and the AI is allowed to answer it. What stops
#: it now is what should: the yard's construction time, the materials, the
#: standing upkeep bill against what its colonies actually produce, and how far
#: from home it can supply.
DEFENSIVE_STRENGTH_PER_COLONY = 2.0

#: Hours of standing upkeep the AI keeps banked before it will buy another hull.
#:
#: The honest question, and the one an earlier version of this got wrong by
#: asking about industry output instead. Upkeep is paid in specific materials --
#: overwhelmingly *fuel*, which is synthesised from water ice and carbon and is
#: the scarcest refined good a young empire has. A civilization can be drowning
#: in steel and alloys, as the AI was with a hundred and fifteen million tonnes
#: banked, and still be unable to keep a single ship flying.
#:
#: So the test is against the fuel bunker rather than the smelters: hold this
#: many hours of what the whole fleet burns, including the ship being
#: considered, or do not build it. That makes the size of a navy a consequence
#: of fuel production without anything having to say so, and -- more importantly
#: -- it stops the AI buying ships it will then watch desert, which is what
#: turned a constraint into a death spiral.
#:
#: Three days rather than the fortnight this started at, and the difference
#: mattered more than it looks. A fortnight's reserve on a fleet of a hundred
#: and ten points wants forty million tonnes of fuel banked against the twelve
#: million a civ that size actually accumulates -- so the AI stopped expanding
#: at day twenty, not because it could not pay its bills but because it was
#: saving for a rainy fortnight. Prudence became the brake, which is exactly the
#: species of artificial ceiling this whole pass exists to remove. Three days is
#: enough to keep ships from deserting between deliveries, which is all the
#: reserve was ever for.
UPKEEP_RESERVE_HOURS = 24.0 * 3.0

#: How far the AI will send a scout, and how many candidate systems it weighs.
#:
#: The galaxy is a pure function and unvisited space costs nothing, but a system
#: only becomes somewhere you can *settle* once a ship has been there -- so an
#: empire that never scouts has a frontier exactly as large as whatever it was
#: charted at the start, which is how the AI's system count sat at seventy-two
#: for an entire twenty-eight day soak.
#:
#: Kept inside supply range on purpose, and that is not caution. Upkeep is
#: charged from the warehouses nearest a fleet and there is nothing to draw on
#: past :data:`SUPPLY_RANGE_LY`, so a scout sent further deserts before it
#: arrives -- exploration that consumes the explorer. Holding it inside the line
#: produces something better than a longer leash: the charted frontier grows
#: only as fast as the settled one, so scouting and settling leapfrog each
#: other outward and neither runs away from the other.
SCOUT_RANGE_LY = SUPPLY_RANGE_LY * 0.8
SCOUT_CANDIDATES = 40

#: Habitability at or below which a settled world is a terraforming candidate,
#: and the least construction a neighbourhood must be able to muster before the
#: AI commits to a campaign there. A project on a world with nothing around it
#: is a decade of work; the same project beside three developed colonies is
#: days, and the AI should be able to tell those apart.
TERRAFORM_CANDIDATE_HABITABILITY = 0.25
TERRAFORM_MINIMUM_NEIGHBOURHOOD_WORK = 5.0e5


class _Turn:
    """What one civilization knows while it is deciding, fetched once.

    Every ``_maybe_*`` below wants the same three things -- this civ's colonies,
    its fleets, and where anybody has already been -- and each used to ask the
    database for them separately. Six helpers asking twice each is twelve round
    trips per civilization per tick to look at a picture that cannot change
    while it is being looked at, because the AI queues intents rather than
    resolving them.

    That last part is what makes this safe: nothing an AI does during its turn
    alters what it can see. The orders it queues are resolved later, by the
    engine, on the tick.
    """

    __slots__ = (
        "session", "universe", "civ",
        "_colonies", "_fleets", "_charted", "_systems",
    )

    def __init__(self, session: Session, universe: Universe, civ: Civ) -> None:
        self.session = session
        self.universe = universe
        self.civ = civ
        self._colonies: list[Colony] | None = None
        self._fleets: list[Fleet] | None = None
        self._charted: set | None = None
        self._systems: list[StarSystem] | None = None

    @property
    def colonies(self) -> list[Colony]:
        if self._colonies is None:
            self._colonies = queries.colonies_of(self.session, self.civ.id)
        return self._colonies

    @property
    def fleets(self) -> list[Fleet]:
        if self._fleets is None:
            self._fleets = list(
                self.session.scalars(
                    select(Fleet).where(Fleet.civ_id == self.civ.id).order_by(Fleet.id)
                )
            )
        return self._fleets

    @property
    def charted(self) -> set:
        if self._charted is None:
            self._charted = queries.charted_key_set(self.session, self.universe.id)
        return self._charted

    @property
    def systems(self) -> list[StarSystem]:
        """Every charted system, with its worlds and their owners loaded.

        Looking for somewhere to settle means walking the star charts and asking
        each world whether anybody has it. Left to lazy loading that is two
        round trips per system -- once for the worlds, once per world for the
        colony -- across the *whole* charted galaxy, which by the second week is
        four hundred systems and climbing. It was the single worst thing the AI
        did, and it got worse precisely as exploration succeeded.
        """
        if self._systems is None:
            self._systems = list(
                self.session.scalars(
                    select(StarSystem)
                    .where(StarSystem.universe_id == self.universe.id)
                    .options(
                        selectinload(StarSystem.worlds)
                        .defer(World.survey)
                        .selectinload(World.colony)
                    )
                    .order_by(StarSystem.id)
                )
            )
        return self._systems


def take_all_turns(session: Session, universe: Universe) -> int:
    """Let every AI civ in ``universe`` queue its orders. Returns how many acted."""
    ai_civs = session.scalars(
        select(Civ)
        .where(Civ.universe_id == universe.id, Civ.is_ai.is_(True))
        .order_by(Civ.id)
    ).all()

    for civ in ai_civs:
        take_turn(session, universe, civ)
    return len(ai_civs)


def take_turn(session: Session, universe: Universe, civ: Civ) -> None:
    """Queue whatever this AI wants to do next.

    Called once per tick. Every decision is seeded from the civ and the tick, so
    a solo game replays identically -- the AI must not be the thing that breaks
    determinism.
    """
    rng = rng_for(civ.seed, "ai", universe.tick_number)
    pending = _pending_by_kind(session, civ)
    turn = _Turn(session, universe, civ)

    if not pending.get(IntentKind.RESEARCH.value):
        intents.research(session, civ)

    _set_policies(turn)
    _maybe_expand(turn, pending)
    _maybe_supply(turn, pending)
    _maybe_migrate(turn, pending)
    _maybe_terraform(turn, pending)
    _maybe_scout(turn, pending)
    _maybe_scrap(turn, pending)
    _maybe_build(turn, pending, rng)


def _set_policies(turn: "_Turn") -> None:
    """Leave colonies governed, and pick a sensible policy for each.

    The AI runs its empire the way a player with many colonies would: it does
    not micromanage labor or building queues, it delegates and chooses a
    posture. Everything that used to be duplicated here now lives in
    :mod:`galaxysim.engine.resolvers.governor`, so the AI and a player's
    governed colonies behave identically -- which is what makes solo play an
    honest rehearsal for the real thing.
    """
    for index, colony in enumerate(turn.colonies):
        if not colony.is_governed:
            continue
        if colony.world.habitability < 0.4:
            policy = governor.SURVIVAL
        elif index == 0:
            # The capital carries the war effort and the shipyard.
            policy = governor.INDUSTRY_POLICY
        elif index % 3 == 2:
            policy = governor.RESEARCH_POLICY
        else:
            policy = governor.EXTRACTION_POLICY
        colony.governor_policy = policy


def _pending_by_kind(session: Session, civ: Civ) -> dict[str, list[Intent]]:
    grouped: dict[str, list[Intent]] = {}
    for intent in intents.pending(session, civ):
        grouped.setdefault(intent.kind, []).append(intent)
    return grouped


def _maybe_expand(turn: "_Turn", pending: dict[str, list[Intent]]) -> None:
    """Send every idle colony ship at the nearest unclaimed world it can afford.

    Every, not one. A civ that has built fourteen colony ships has fourteen
    expeditions' worth of intent, and settling them one at a time means the
    other thirteen sit in orbit paying upkeep for a month. The bound on how fast
    it expands should be what its warehouses can outfit, which is a real
    constraint, rather than a queue of one, which is not.
    """
    session, civ = turn.session, turn.civ
    ordered = pending.get(IntentKind.COLONIZE.value, [])
    busy_fleets = {i.payload.get("fleet_id") for i in ordered}
    claimed = {i.payload.get("world_id") for i in ordered}

    for fleet in _idle_colony_fleets(turn):
        if fleet.id in busy_fleets:
            continue

        target = _nearest_settleable_world(turn, fleet, claimed)
        if target is None:
            return  # nothing left for this fleet is nothing left for any of them
        world, system = target

        # The expedition is loaded before it leaves, so affordability is a
        # question about the warehouse it is standing next to now.
        outfitter = queries.nearest_colony(session, civ.id, fleet.position)
        if outfitter is None:
            return

        loadout = _loadout_for(world)
        if not can_afford(outfitter.stockpile, loadout.cost()):
            return  # the next ship would ask the same warehouse the same thing

        if distance(fleet.position, system.position) > 0.01:
            intents.move_fleet_to_system(session, civ, fleet.id, system)
        intents.colonize(session, civ, fleet.id, world.id, loadout=loadout)
        claimed.add(world.id)


def _loadout_for(world) -> Loadout:
    """Size an expedition to the world it is going to.

    The AI reads hostility the way the pricing model intends: it does not pay a
    surcharge for a hard world, it packs more stores. A garden world gets a light
    landing; a bare rock gets a month of independence while a supply line is
    arranged.
    """
    hostility = 1.0 - world.habitability
    if hostility <= 0.2:
        return Loadout(colonists=SETTLERS, equipment=4.0, stores=STORES_FOR_A_MONTH * 0.25)
    if hostility <= 0.6:
        return Loadout(colonists=SETTLERS, equipment=4.0, stores=STORES_FOR_A_MONTH * 0.75)
    return Loadout(colonists=SETTLERS, equipment=5.0, stores=STORES_FOR_A_MONTH * 2.0)


def _maybe_supply(turn: "_Turn", pending: dict[str, list[Intent]]) -> None:
    """Keep a standing route running to every colony that cannot feed itself.

    This is the piece that makes AI expansion mean anything. Almost every world
    worth settling is a dead one, and a dead one lives or dies by its supply
    line -- so an AI that founds colonies without routing to them is not
    expanding, it is running a slow way to kill fifty thousand colonists at a
    time, which is exactly what it used to do.

    Ordinary orders through the ordinary API: same route intent a player issues,
    same throughput limits, no privileged access.
    """
    session, universe, civ = turn.session, turn.universe, turn.civ
    colonies = turn.colonies
    if len(colonies) < 2:
        return

    # The capital -- biggest population -- is the only place with the surplus to
    # supply anybody.
    source = max(colonies, key=lambda c: c.population)
    routed = {
        intent.payload.get("dest_colony_id")
        for intent in _all_routes(session, universe, civ)
    }

    for colony in colonies:
        if colony.id == source.id or colony.id in routed:
            continue
        # The promoted column, not the document. ``surface_water`` has existed
        # for exactly this since the columns were added; this call was reading
        # the whole survey to learn one boolean.
        if colony.world.surface_water:
            continue  # it draws its own water; it can wait
        fleet = _idle_freighter(turn)
        if fleet is None:
            # Freighters pay upkeep like anything else, and they used to slip
            # past the check that asks whether the civ can carry it -- so a
            # supply fleet quietly grew until the ships keeping the outposts
            # alive were themselves deserting.
            if not _can_carry_more_upkeep(
                civ,
                colonies,
                turn.fleets,
                FREIGHTER_STRENGTH,
            ):
                return
            _order_freighter(session, civ, source, pending)
            return
        intents.supply_route(session, civ, fleet.id, source.id, colony.id, dict(ROUTE_MANIFEST))
        return


def _order_freighter(
    session: Session, civ: Civ, yard: Colony, pending: dict[str, list[Intent]]
) -> None:
    """Build a hull that is mostly hold.

    A warship's incidental hold is forty tonnes; an outpost drinks that in an
    hour. Supplying anything at real scale needs a ship built for it, and hold
    is priced per tonne, so this is a real purchase rather than a free one.
    """
    if any(
        intent.payload.get("cargo_capacity") for intent in pending.get(IntentKind.BUILD_FLEET.value, [])
    ):
        return
    if FLEET_CONSTRUCTION not in colony_effects(yard).grants:
        return

    hold = sum(ROUTE_MANIFEST.values()) * FREIGHTER_TRIPS_OF_SLACK
    cost = {r: a * FREIGHTER_STRENGTH for r, a in FLEET_COST_PER_STRENGTH.items()}
    for resource, per_tonne in FREIGHTER_COST_PER_CAPACITY.items():
        cost[resource] = cost.get(resource, 0.0) + per_tonne * hold
    if not can_afford(yard.stockpile, cost):
        return

    intents.build_fleet(
        session,
        civ,
        yard.id,
        FREIGHTER_STRENGTH,
        cargo_capacity=hold,
        name=f"{civ.name} Freighter",
    )


def _maybe_migrate(turn: "_Turn", pending: dict[str, list[Intent]]) -> None:
    """Move settlers from a full world to one with room.

    The capital opens near capacity and barely grows, so its people are a
    *reservoir* rather than a surplus that keeps accumulating. A young colony
    with headroom is where they are worth more, and shipping them there is the
    difference between a colony that matures in weeks and one that takes months
    compounding from a landing party.

    Only one convoy at a time, and only where there is real room to fill --
    dumping people faster than a colony can build for them just spreads its
    industry thinner.
    """
    session, universe, civ = turn.session, turn.universe, turn.civ
    if pending.get(IntentKind.MIGRATE.value):
        return
    if any(
        intent.kind == IntentKind.MIGRATE.value
        for intent in queries.active_intents(session, universe.id, IntentKind.MIGRATE.value)
        if intent.civ_id == civ.id
    ):
        return

    colonies = turn.colonies
    if len(colonies) < 2:
        return

    source = max(colonies, key=lambda c: c.population)
    if source.population < MIGRATION_RESERVE:
        return

    # The colony with the most unused room, as a fraction of what it could hold.
    def headroom(colony: Colony) -> float:
        ceiling = capacity(colony.world, colony.infrastructure, colony.world.habitability)
        return max(0.0, ceiling - colony.population)

    target = max((c for c in colonies if c.id != source.id), key=headroom, default=None)
    if target is None or headroom(target) < MIGRATION_BATCH:
        return

    fleet = _idle_freighter(turn, hold=MIGRATION_BATCH * TONNES_PER_SETTLER)
    if fleet is None:
        return

    people = min(MIGRATION_BATCH, headroom(target), source.population - MIGRATION_RESERVE)
    if people <= 0:
        return
    intents.migrate(session, civ, fleet.id, source.id, target.id, people)


def _all_routes(session: Session, universe: Universe, civ: Civ) -> list[Intent]:
    return [
        intent
        for intent in queries.active_intents(
            session, universe.id, IntentKind.SUPPLY_ROUTE.value
        )
        if intent.civ_id == civ.id
    ]


def _maybe_scrap(turn: "_Turn", pending: dict[str, list[Intent]]) -> None:
    """Break up a hull the empire no longer has a use for, or cannot pay for.

    Half of the late-run fleet knock, and the half that is a *decision* rather
    than an accident. An expedition ship that has landed its pod is a warship
    with nothing to fight, and a soak used to end with two dozen of them: hulls
    with no purpose, drawing upkeep every hour, in a civilization that was by
    then missing payments and watching crews desert.

    Desertion and scrapping both end with fewer ships. The difference is that
    desertion returns nothing and happens to you, while scrapping returns a
    third of the materials and is something you chose -- and choosing it while
    still solvent is what stops the shortfall spreading to the ships that *are*
    doing something.
    """
    if pending.get(IntentKind.DECOMMISSION.value):
        return

    session, civ = turn.session, turn.civ
    colonies = turn.colonies
    if not colonies:
        return

    warships = [
        fleet
        for fleet in turn.fleets
        if fleet.colony_pods <= 0 and fleet.cargo_capacity <= 100.0
    ]
    if not warships:
        return

    # Freighters are never candidates: an outpost dies without its route, so a
    # civ short of fuel must not balance its books by cutting the supply line.
    over = sum(f.strength for f in warships) - len(colonies) * DEFENSIVE_STRENGTH_PER_COLONY
    if over <= 0 and civ.upkeep_paid >= 1.0 - 1e-9:
        return

    # Out of the orders already in hand rather than a fresh query: a decision
    # this cheap should not cost the tick a round trip.
    busy = {
        intent.payload.get("fleet_id") for group in pending.values() for intent in group
    }
    docked = [
        fleet
        for fleet in warships
        if not fleet.in_transit and fleet.id not in busy and _at_a_colony(fleet, colonies)
    ]
    if not docked:
        return

    # The smallest hull that is surplus to requirements, so the empire sheds the
    # least capability it can while still shedding the bill.
    intents.decommission_fleet(session, civ, min(docked, key=lambda f: (f.strength, f.id)).id)


def _at_a_colony(fleet: Fleet, colonies: list[Colony]) -> bool:
    nearest = queries.nearest_of(colonies, fleet.position)
    if nearest is None:
        return False
    return distance(fleet.position, nearest.world.system.position) <= DOCKING_TOLERANCE_LY


def _idle_freighter(turn: "_Turn", hold: float | None = None) -> Fleet | None:
    """A ship with a *useful* hold and nothing better to do.

    "Has any hold at all" is not the test: every warship carries forty tonnes
    incidentally, which an outpost drinks in an hour. Accepting one of those as
    a freighter is how the AI ended up running supply routes that delivered less
    than the destination consumed in transit -- routes that looked busy in the
    log and starved the colony anyway.
    """
    session, civ = turn.session, turn.civ
    busy = {intent.payload.get("fleet_id") for intent in intents.pending(session, civ)}
    busy |= {
        intent.payload.get("fleet_id")
        for intent in session.scalars(
            select(Intent).where(
                Intent.civ_id == civ.id,
                Intent.kind == IntentKind.SUPPLY_ROUTE.value,
                Intent.status == IntentStatus.IN_PROGRESS.value,
            )
        )
    }
    wanted = sum(ROUTE_MANIFEST.values()) if hold is None else hold
    for fleet in turn.fleets:
        if fleet.cargo_capacity >= wanted and not fleet.in_transit and fleet.id not in busy:
            return fleet
    return None


def _idle_colony_fleets(turn: "_Turn") -> list[Fleet]:
    return [f for f in turn.fleets if f.colony_pods > 0 and not f.in_transit]


def _nearest_settleable_world(
    turn: "_Turn",
    fleet: Fleet,
    claimed: set[int] | None = None,
) -> tuple[World, StarSystem] | None:
    """Closest unclaimed world, skipping any another expedition is already after.

    Only looks at systems that already have rows -- that is, ones somebody has
    visited. That is exactly a player's star charts, so the AI is working from
    the same information a player would have and no more.
    """
    claimed = claimed or set()
    best: tuple[float, World, StarSystem] | None = None

    for system in turn.systems:
        span = distance(fleet.position, system.position)
        if best is not None and span >= best[0]:
            continue
        for world in sorted(system.worlds, key=lambda w: w.id):
            if world.colony is not None or world.id in claimed:
                continue
            # Any unclaimed world, including dead ones. That is not recklessness:
            # genuinely habitable worlds are about one in six thousand and every
            # civ's homeworld is one of them, so in any ordinary neighbourhood
            # *every* remaining world is a rock. Expanding at all means settling
            # rocks and keeping them supplied -- see :func:`_maybe_supply`.
            best = (span, world, system)
            break

    return (best[1], best[2]) if best else None


def _can_carry_more_upkeep(civ: Civ, colonies, fleets, extra_strength: float) -> bool:
    """Whether this civ could still pay its bills with another hull flying.

    The only limit left on a navy's size, and it is an economic one rather than
    a rule. Grow the economy and the fleet it can carry grows with it, which is
    what makes a navy a consequence of prosperity rather than a permission.

    Asked against the **upkeep materials specifically**, not against output in
    general. That distinction is the whole point: upkeep is mostly fuel, fuel is
    synthesised from water ice and carbon, and an empire can hold a hundred
    million tonnes of steel while holding no fuel at all -- which is exactly
    what happened, and why a navy of ninety points of strength deserted down to
    five over the back half of a soak while its warehouses looked healthy.

    Two questions, and they are not the same question. **Is it keeping up?** --
    :attr:`Civ.upkeep_paid`, last tick's bill against what was actually paid.
    **Has it got a cushion?** -- the reserve below. A civ that is already
    missing payments has its answer regardless of what is banked, and that is
    the case the stock check alone walked straight past: three days of fuel in
    hand while draining is three days of fuel in hand, and the balance looks
    fine right up until the ships start deserting.
    """
    if civ.upkeep_paid < 1.0 - 1e-9:
        return False

    strength = sum(f.strength for f in fleets) + extra_strength
    if strength <= 0:
        return True

    banked: dict[str, float] = {}
    for colony in colonies:
        for material in FLEET_UPKEEP_PER_STRENGTH:
            banked[material] = banked.get(material, 0.0) + colony.stockpile.get(material, 0.0)

    return all(
        banked.get(material, 0.0) >= per_strength * strength * UPKEEP_RESERVE_HOURS
        for material, per_strength in FLEET_UPKEEP_PER_STRENGTH.items()
    )


def _maybe_terraform(turn: "_Turn", pending: dict[str, list[Intent]]) -> None:
    """Start reshaping a dead world the empire is actually placed to reshape.

    The single most valuable thing a mature civilization can do, and until now
    the AI did not know it existed. It matters more than any other order it
    issues: settling rocks adds almost no *people*, so an empire of thirty
    outposts has barely more population than one of six -- and population is
    what every other ceiling in the game is measured against. Terraforming is
    the only mechanism that moves it, by four orders of magnitude, and an AI
    that never terraforms is one that plateaus by construction.

    Two judgements, and both are about place rather than about the project.
    Pick a world worth converting, and only commit where the *neighbourhood* can
    do the work -- a project pools construction from every colony within supply
    range, so the same campaign is three weeks beside a developed cluster and a
    decade alone in the dark.
    """
    session, civ = turn.session, turn.civ
    if pending.get(IntentKind.TERRAFORM.value):
        return  # one planet at a time; they are enormous

    colonies = turn.colonies
    for colony in colonies:
        if colony.world.habitability > TERRAFORM_CANDIDATE_HABITABILITY:
            continue

        neighbourhood = queries.sorted_by_distance(
            colonies, colony.world.system.position, within_ly=SUPPLY_RANGE_LY
        )
        muscle = sum(
            helper.population
            * normalize(helper.labor).get(INDUSTRY, 0.0)
            * DEFAULT_RATES.industry_per_worker_per_hour
            for helper in neighbourhood
        )
        if muscle < TERRAFORM_MINIMUM_NEIGHBOURHOOD_WORK:
            continue

        # Read off the promoted column rather than parsing the survey. Working
        # this out from the document meant decoding the largest object in the
        # game per candidate world per turn -- the precise cost those columns
        # exist to avoid, reached from the AI's side where the guard was not
        # looking. The resolver re-checks the physics before it charges anybody,
        # so nothing is taken on trust here.
        step = colony.world.terraform_next
        if not step:
            continue

        intents.terraform(session, civ, colony.id, step)
        return


def _maybe_scout(turn: "_Turn", pending: dict[str, list[Intent]]) -> None:
    """Send a ship somewhere nobody has been.

    The galaxy is a pure function and a system exists as soon as the maths says
    it does -- but it only becomes somewhere you can *settle* once a ship has
    arrived and turned it into rows. So a civilization that never scouts has a
    frontier exactly as large as whatever it was charted at the start, and in a
    twenty-eight day soak the AI's system count never moved off seventy-two.

    This is what makes the frontier unbounded in practice rather than only in
    principle: pick the nearest star nobody has visited and go and look at it.
    """
    session, universe, civ = turn.session, turn.universe, turn.civ
    idle = [
        fleet
        for fleet in turn.fleets
        if not fleet.in_transit and fleet.colony_pods <= 0 and fleet.cargo_capacity <= 100.0
    ]
    if not idle:
        return  # warships only; freighters and settlers have jobs

    moving = {i.payload.get("fleet_id") for i in pending.get(IntentKind.MOVE_FLEET.value, [])}
    scout = next((f for f in idle if f.id not in moving), None)
    if scout is None:
        return

    # Measured from the *colonies*, not from the ship. Measuring from the ship
    # let a scout hop twenty light-years, then twenty more from wherever it had
    # got to, walking steadily out of supply range until it deserted somewhere
    # nobody would ever look. Anchoring the leash to the empire is what makes
    # the charted frontier expand only as fast as the settled one.
    # One query for everywhere anybody has been, then pure set membership.
    # Asking the database per candidate meant forty round trips per colony per
    # turn to decide a single move, which at thirty colonies is twelve hundred.
    charted = turn.charted

    colonies = turn.colonies
    for colony in queries.sorted_by_distance(colonies, scout.position):
        for stub in systems_near(
            universe.seed,
            colony.world.system.position,
            SCOUT_RANGE_LY,
            limit=SCOUT_CANDIDATES,
        ):
            if stub.key not in charted:
                intents.move_fleet(
                    session,
                    civ,
                    scout.id,
                    stub.position.x,
                    stub.position.y,
                    stub.position.z,
                )
                return


def _maybe_build(turn: "_Turn", pending: dict[str, list[Intent]], rng) -> None:
    """Build a fleet when there is a reason to and it can afford to keep it.

    What it builds is decided, not rolled. The version of this that flipped a
    coin between a warship and a settler filled the whole standing-navy budget
    with idle warships inside two days -- and since the budget scales with
    population, and population only grows by expanding, the AI then could not
    afford the colony ship that would have let it grow. Fifty hulls in orbit,
    three hundred empty worlds in range, and a civilization that never moved
    again.

    So: expansion first, and a warship only up to what the empire it actually
    holds would want to defend.
    """
    session, civ = turn.session, turn.civ
    if pending.get(IntentKind.BUILD_FLEET.value):
        return

    cost = {
        resource: amount * BUILD_STRENGTH * BUILD_RESERVE
        for resource, amount in FLEET_COST_PER_STRENGTH.items()
    }

    colonies = turn.colonies
    if not colonies:
        return

    # Build wherever the materials actually are, rather than always at the
    # capital -- with local stockpiles the capital is often not the richest.
    # A yard is required, so most colonies are not candidates at all.
    colony = next(
        (
            c
            for c in colonies
            if FLEET_CONSTRUCTION in colony_effects(c).grants and can_afford(c.stockpile, cost)
        ),
        None,
    )
    if colony is None:
        return

    fleets = turn.fleets

    # Affording the purchase is not the same as affording the standing bill.
    # This is the only limit left on how large a navy may get, and it is a real
    # one now that a ship costs a real fraction of what a colony makes: keep
    # adding hulls and the hourly upkeep eats the output that was building them.
    if not _can_carry_more_upkeep(civ, colonies, fleets, BUILD_STRENGTH):
        return

    # A settler if there is somewhere to send one and nothing to send.
    wants_settler = not _idle_colony_fleets(turn) and any(
        _nearest_settleable_world(turn, fleet) for fleet in fleets[:1]
    )
    if wants_settler:
        intents.build_fleet(
            session,
            civ,
            colony.id,
            BUILD_STRENGTH,
            colony_pods=1,
            name=f"{civ.name} Settler {rng.randrange(100, 999)}",
        )
        return

    # Otherwise a warship, and only up to what this many colonies is worth
    # garrisoning. Hulls with nothing to do still cost upkeep every hour.
    warships = sum(f.strength for f in fleets if f.colony_pods <= 0 and f.cargo_capacity <= 100.0)
    if warships + BUILD_STRENGTH > len(colonies) * DEFENSIVE_STRENGTH_PER_COLONY:
        return

    intents.build_fleet(
        session,
        civ,
        colony.id,
        BUILD_STRENGTH,
        colony_pods=0,
        name=f"{civ.name} Fleet {rng.randrange(100, 999)}",
    )


def cancel_all(session: Session, civ: Civ) -> None:
    """Clear a civ's standing orders. Used when handing an AI civ to a player."""
    for intent in intents.pending(session, civ):
        intent.status = IntentStatus.CANCELLED.value
