"""A baseline AI opponent.

The rule this file exists to honour: **the AI has no privileged access.** It
submits the same intents through the same :mod:`galaxysim.engine.intents` API a
human uses, reads only what a human could read, and waits for the same ticks.
Nothing here reaches into world state directly.

That is a deliberate constraint rather than a stylistic one, and the reason is
narrower than it used to be written: this is a single-player game, so "solo must
match multiplayer" is not the argument. The argument is that **the soak is the
only instrument for telling whether the economy works.** Every economic failure
this project has found surfaced by watching these civilizations run under
exactly the constraints a player faces; hand them a production multiplier and
the soak stops measuring the game and starts measuring the multiplier.

*How well* it plays is not fixed. Every standard of play -- how often it thinks,
how many worlds it industrialises, whether it settles the nearest rock or the
best one -- comes from the universe's :class:`~galaxysim.ai.doctrine.Doctrine`,
so difficulty is composed of attention, competence and circumstance rather than
of gifts.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from galaxysim.ai.doctrine import doctrine as doctrine_for
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
from galaxysim.materials.catalogue import RAW_MATERIALS
from galaxysim.core.seeds import rng_for
from galaxysim.core.space import Vec3, distance
from galaxysim.colony.buildings import FLEET_CONSTRUCTION
from galaxysim.engine import intents
from galaxysim.engine.resolvers import governor, queries
from galaxysim.engine.resolvers.siege import BLOCKADE_RANGE_LY
from galaxysim.engine.rates import DEFAULT_RATES
from galaxysim.engine.resolvers.production import (
    DOCKING_TOLERANCE_LY,
    SUPPLY_RANGE_LY,
    colony_effects,
    effective_habitability,
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

#: One colony in this many may be run as an industrial centre, whatever the
#: doctrine asks for. Yards are fed by mines somewhere, and an empire that
#: industrialises half of itself has nothing left digging: asking for six
#: industrial worlds out of twelve measurably finished *behind* asking for three.
INDUSTRIAL_WORLD_SHARE = 3

#: Hours a terraforming order may sit unfunded before the AI gives up on it and
#: frees the campaign slot. A week: long enough that an order waiting on one more
#: freight run is not thrown away, short enough that a world the empire cannot
#: supply at all does not cost it a month of doing nothing.
TERRAFORM_PATIENCE_HOURS = 24 * 7

# Standards of play -- how often this opponent thinks, how many yards it runs,
# how far it scouts, how bold it is about terraforming and how thin it lets its
# reserves run -- all live on its :class:`~galaxysim.ai.doctrine.Doctrine`, which
# the universe names. They were module constants here, which meant every game
# had exactly one opponent.


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
        "session", "universe", "civ", "doctrine", "claimed",
        "_colonies", "_fleets", "_shared",
    )

    def __init__(
        self,
        session: Session,
        universe: Universe,
        civ: Civ,
        shared: dict | None = None,
    ) -> None:
        self.session = session
        self.universe = universe
        self.civ = civ
        # How well this opponent plays. Every standard of play the AI has --
        # how often it thinks, how many yards it runs, whether it settles the
        # nearest world or the best one -- reads off this rather than off
        # module constants, so difficulty is one object rather than a scatter
        # of numbers. See :mod:`galaxysim.ai.doctrine`.
        self.doctrine = doctrine_for(universe.ai_difficulty)
        # Fleets already given a job *this turn*.
        #
        # Every ``_maybe_*`` below works out which ships are busy from
        # ``pending``, and ``pending`` is read once before any of them run. So an
        # order issued by an earlier decision is invisible to a later one, and
        # two of them will happily send the same ship to two places. That is not
        # theoretical: expansion dispatches every idle colony ship to settle, a
        # queued move does not set ``in_transit``, and the raid then picks one of
        # those very ships as the pod it means to land on a besieged world. The
        # colonisation wins, the siege gets nobody, and no world has ever changed
        # hands in this game as a result.
        self.claimed: set[int] = set()
        self._colonies: list[Colony] | None = None
        self._fleets: list[Fleet] | None = None
        # The star charts and the list of charted systems are facts about the
        # *universe*, not about this civ, and they cannot change while the AIs
        # are deciding. Eight civilizations each re-reading the whole galaxy to
        # look at the identical picture was eight times the worst query in the
        # turn. Shared across a tick when the caller offers a place to share.
        self._shared: dict = {} if shared is None else shared

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
        if "charted" not in self._shared:
            self._shared["charted"] = queries.charted_key_set(
                self.session, self.universe.id
            )
        return self._shared["charted"]

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
        if "systems" not in self._shared:
            self._shared["systems"] = list(
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
        return self._shared["systems"]


def take_all_turns(session: Session, universe: Universe) -> int:
    """Let every AI civ due a decision queue its orders. Returns how many acted.

    **How often an opponent thinks is the first difficulty dial**, and in an
    asynchronous game it is the truest one: what actually differs between human
    opponents is how often they check in. A dormant rival re-plans once a day, a
    relentless one every hour.

    Expressed in *hours* of simulated time, never in ticks. Left in ticks, an
    opponent in a five-minute universe would think twelve times as often as one
    in an hourly universe -- exactly the class of bug
    :mod:`galaxysim.engine.rates` exists to prevent, and one that was latent
    here for the whole project: deciding once per tick is what the AI always
    did, so its rate of play silently depended on the cadence.

    Staggered by civ id so the galaxy's opponents do not all think on the same
    tick, which spreads the cost instead of paying it in lumps. Deterministic:
    same ids, same schedule, same game.
    """
    from galaxysim.engine.rates import Cadence

    ai_civs = session.scalars(
        select(Civ)
        .where(Civ.universe_id == universe.id, Civ.is_ai.is_(True))
        .order_by(Civ.id)
    ).all()
    if not ai_civs:
        return 0

    standard = doctrine_for(universe.ai_difficulty)
    every = max(
        1,
        Cadence(universe.seconds_per_tick).ticks_for_hours(
            standard.decision_interval_hours
        ),
    )

    # One place for the facts every civ reads identically this tick.
    shared: dict = {}
    acted = 0
    for civ in ai_civs:
        if (universe.tick_number + civ.id) % every:
            continue
        take_turn(session, universe, civ, shared=shared)
        acted += 1
    return acted


def take_turn(
    session: Session, universe: Universe, civ: Civ, *, shared: dict | None = None
) -> None:
    """Queue whatever this AI wants to do next.

    Called once per tick. Every decision is seeded from the civ and the tick, so
    a solo game replays identically -- the AI must not be the thing that breaks
    determinism.
    """
    rng = rng_for(civ.seed, "ai", universe.tick_number)
    pending = _pending_by_kind(session, civ)
    turn = _Turn(session, universe, civ, shared)

    if not pending.get(IntentKind.RESEARCH.value):
        intents.research(session, civ)

    _set_policies(turn)
    # War before settlement, and this order is load-bearing rather than
    # stylistic. The AI builds its warships *with a colony pod attached*, so
    # almost every fighting ship it owns is also a lander -- and expansion
    # dispatches every idle pod-carrier it can see. Run settlement first and
    # there is never a pod left for a siege, which is why in the entire history
    # of this game not one world has ever changed hands. A civilization already
    # holding an enemy world under blockade commits to finishing that before it
    # sends the remainder off to plant flags on rocks.
    _maybe_annex(turn, pending)
    _maybe_raid(turn, pending)
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
    # Which worlds are run as industrial centres, and therefore where the
    # shipyards are.
    #
    # **This used to be "colony index 0", full stop** -- and since only the
    # industry policy's build order contains a shipyard, every AI civilization
    # in the game had exactly one yard, permanently, however large it grew. All
    # expansion funnelled through a single world. A competent player turns
    # several developed worlds into yards and builds in parallel, which is
    # difficulty earned rather than granted: not one price changes.
    #
    # Chosen by *development* rather than by id, because a yard on a
    # fifty-thousand-person outpost is a yard that can never pay for what it
    # would build. Ties break on id so the choice stays determined.
    # Bounded by a *share* of the empire as well as by the doctrine, because a
    # shipyard with nothing mining behind it is worse than no shipyard. Measured:
    # a doctrine asking for six industrial worlds out of twelve held put half
    # the empire on an industry policy, starved the other half of extraction,
    # and finished behind the doctrine asking for three. More aggressive is not
    # automatically better, and this is where that stops being true.
    wanted = max(1, turn.doctrine.industrial_worlds)
    affordable = max(1, len(turn.colonies) // INDUSTRIAL_WORLD_SHARE)
    industrial = {
        colony.id
        for colony in sorted(
            turn.colonies, key=lambda c: (-c.development, c.id)
        )[: min(wanted, affordable)]
    }

    for index, colony in enumerate(turn.colonies):
        if not colony.is_governed:
            continue
        # **Effective** habitability, not the world's raw number: what a colony
        # experiences is what it was born with *plus the domes it has built*,
        # which is exactly how the governor decides the same question.
        #
        # Judging on the raw figure condemned a world to survival for ever, no
        # matter how much was built on it -- and since a settled world is nearly
        # always a barren rock, that meant every colony but the homeworld. Four
        # civilizations across thirty days held between them a hundred colonies
        # and *four* industrial worlds: one each, at every difficulty. The whole
        # several-shipyards lever was inert, and this line was why.
        if effective_habitability(colony, colony_effects(colony)) < 0.4:
            policy = governor.SURVIVAL
        elif colony.id in industrial:
            # Carries the war effort and a shipyard.
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
    busy_fleets = {i.payload.get("fleet_id") for i in ordered} | turn.claimed
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
        # Deliberately *not* checking whether the warehouse can cover the
        # manifest right now. It usually cannot -- a capital's governor spends
        # steel on its own industry the hour it is refined, so the shelves are
        # bare most hours of most days -- and an expedition loads over time out
        # of whatever the colony can spare. Refusing to place the order because
        # today is a lean day is how a civilization with somewhere to go, a ship
        # to go in and a healthy income sat still for two simulated months.

        if distance(fleet.position, system.position) > 0.01:
            intents.move_fleet_to_system(session, civ, fleet.id, system)
        intents.colonize(session, civ, fleet.id, world.id, loadout=loadout)
        claimed.add(world.id)
        turn.claimed.add(fleet.id)


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
                turn,
                colonies,
                turn.fleets,
                FREIGHTER_STRENGTH,
            ):
                return
            _order_freighter(session, civ, source, pending)
            return
        intents.supply_route(session, civ, fleet.id, source.id, colony.id, dict(ROUTE_MANIFEST))
        turn.claimed.add(fleet.id)
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
    turn.claimed.add(fleet.id)


def _all_routes(session: Session, universe: Universe, civ: Civ) -> list[Intent]:
    return [
        intent
        for intent in queries.active_intents(
            session, universe.id, IntentKind.SUPPLY_ROUTE.value
        )
        if intent.civ_id == civ.id
    ]


def _maybe_raid(turn: "_Turn", pending: dict[str, list[Intent]]) -> None:
    """Blockade and take a rival's frontier world, if there is a fleet to spare.

    The whole of the AI's warmaking, and deliberately small. It picks one
    target, sends what it can spare, and declares. Everything that makes the
    result interesting -- the siege grinding down a world's population and
    infrastructure, the colony pod that has to land to finish the job, the
    supply lines that stop moving on both sides -- already lives in
    :mod:`galaxysim.engine.resolvers.siege`, and none of it is available to this
    civ on any terms a player could not also get.

    Three constraints, and each one is the reason a raid reads as a decision
    rather than as a die roll:

    **It only fights within supply range of its own worlds.** Fleet upkeep is
    billed to the colonies near a fleet, and there is nothing to draw on past
    :data:`SUPPLY_RANGE_LY` -- a squadron parked deep in someone else's space
    deserts within days. So a war is fought along a border, which is where wars
    are, and holding a distant world means settling toward it first.

    **It only goes after a frontier.** Resistance is a colony's people and its
    infrastructure, so a capital cannot be taken from orbit by any fleet worth
    building. Sending a raid at one is a fleet thrown away, and an opponent that
    does it reads as stupid rather than as difficult.

    **It keeps a navy at home.** It will not go to war unless it holds at least
    twice the strength the raid asks for, so what it sends is a surplus rather
    than everything it has. One number, and it is the doctrine's own -- an
    opponent that empties its home systems to take an outpost has not become
    harder to play against.
    """
    doctrine = turn.doctrine
    if doctrine.raid_strength <= 0:
        return
    # One war at a time. The attack order is standing, so a second declaration
    # would not add anything except more enemies to be blockaded by.
    if pending.get(IntentKind.ATTACK.value):
        return

    session, civ = turn.session, turn.civ
    colonies = turn.colonies
    if not colonies:
        return

    busy = {
        intent.payload.get("fleet_id") for group in pending.values() for intent in group
    } | turn.claimed
    warships = [
        fleet
        for fleet in turn.fleets
        if fleet.cargo_capacity <= 100.0
        and not fleet.in_transit
        and fleet.id not in busy
        and fleet.strength > 0
    ]
    if sum(fleet.strength for fleet in warships) < doctrine.raid_strength * 2.0:
        return

    committed: list[Fleet] = []
    strength = 0.0
    for fleet in sorted(warships, key=lambda f: (-f.strength, f.id)):
        committed.append(fleet)
        strength += fleet.strength
        if strength >= doctrine.raid_strength:
            break
    if strength < doctrine.raid_strength:
        return

    target = _raidable_colony(turn)
    if target is None:
        return

    # A pod goes along if one is idle. Without it the raid is still worth
    # running -- a blockade starves an outpost whether or not anybody lands --
    # but with it the world changes hands, which is the only way this
    # civilization ever takes a *developed* place rather than founding one.
    # Often one of the warships already going: the AI builds its hulls with a
    # pod attached, so most of its navy can land an administration. Only look
    # for a separate ship if none of the fighting force can.
    if not any(fleet.colony_pods > 0 for fleet in committed):
        going = {fleet.id for fleet in committed}
        for lander in _idle_colony_fleets(turn):
            if lander.id not in busy and lander.id not in going:
                committed.append(lander)
                break

    system = target.world.system
    intents.attack(session, civ, target.civ_id)
    for fleet in committed:
        turn.claimed.add(fleet.id)
        if distance(fleet.position, system.position) > DOCKING_TOLERANCE_LY:
            intents.move_fleet_to_system(session, civ, fleet.id, system)


def _maybe_annex(turn: "_Turn", pending: dict[str, list[Intent]]) -> None:
    """Land somebody to run a world this civilization is already besieging.

    The half of conflict that never happened. Blockade and siege both worked --
    measured, two colonies were cut off and ground to zero resistance -- and
    then nothing, because a colony pod was dispatched exactly once, at the
    moment war was declared, and never again. One of those worlds sat subdued
    and blockaded for three hundred and thirteen hours with nobody coming for
    it. Grinding a world down is not taking it; somebody has to land an
    administration, and that has to be a standing intention rather than a
    gesture made at the outbreak.

    The rule is deliberately something the besieger can *see*: my warships are
    holding station over a colony belonging to somebody I am at war with, and no
    lander of mine is there or on the way. It never reads the defender's
    resistance -- which would be looking at a number no player is shown -- and
    it does not need to. The pod sits in orbit and
    :func:`galaxysim.engine.resolvers.siege._try_capture` spends it the moment
    the world stops resisting, whether that is this afternoon or next month.
    """
    session, civ = turn.session, turn.civ
    at_war = {
        intent.payload.get("target_civ_id")
        for intent in pending.get(IntentKind.ATTACK.value, [])
    }
    if not at_war:
        return

    on_station = [f for f in turn.fleets if not f.in_transit and f.strength > 0]
    if not on_station:
        return

    busy = {
        intent.payload.get("fleet_id") for group in pending.values() for intent in group
    } | turn.claimed

    for system in turn.systems:
        for world in system.worlds:
            colony = world.colony
            if colony is None or colony.civ_id not in at_war:
                continue

            here = [
                fleet
                for fleet in on_station
                if distance(fleet.position, system.position) <= BLOCKADE_RANGE_LY
            ]
            if not here:
                continue  # not besieging this one
            if any(fleet.colony_pods > 0 for fleet in here):
                continue  # a lander is already on station, waiting

            # Or already on its way. Reading our own ships' destinations, which
            # is why this needs no order bookkeeping to stay idempotent -- the
            # decision is re-made every turn and answers itself.
            if any(
                fleet.colony_pods > 0
                and fleet.in_transit
                and _destination(fleet) is not None
                and distance(_destination(fleet), system.position) <= BLOCKADE_RANGE_LY
                for fleet in turn.fleets
            ):
                continue

            lander = next(
                (f for f in _idle_colony_fleets(turn) if f.id not in busy), None
            )
            if lander is None:
                return  # nothing to send anywhere

            intents.move_fleet_to_system(session, civ, lander.id, system)
            turn.claimed.add(lander.id)
            return


def _destination(fleet: Fleet) -> Vec3 | None:
    """Where a fleet in transit is headed, if it is going anywhere."""
    if fleet.dest_x is None or fleet.dest_y is None or fleet.dest_z is None:
        return None
    return Vec3(fleet.dest_x, fleet.dest_y, fleet.dest_z)


def _raidable_colony(turn: "_Turn") -> Colony | None:
    """A rival's frontier world this civ could plausibly besiege and hold.

    Read off the star charts the AI already has loaded, so a war costs the tick
    no queries at all. Those charts are exactly what a player can see -- systems
    somebody has actually visited -- which is what keeps this an opponent
    playing the game rather than one reading the database.
    """
    ceiling = turn.doctrine.raid_population_ceiling
    candidates = [
        (system.position, world.colony)
        for system in turn.systems
        for world in system.worlds
        if world.colony is not None
        and world.colony.civ_id != turn.civ.id
        and world.colony.population <= ceiling
    ]
    if not candidates:
        return None

    # Rivals first, then distance -- rather than a distance for every world in
    # the charted galaxy. By the second week the charts are hundreds of systems
    # and a handful of them have anybody living on them, and this is the
    # difference between a decision that costs nothing and one that shows up in
    # the tick.
    mine = [colony.world.system.position for colony in turn.colonies]
    best: tuple[float, int] | None = None
    chosen: Colony | None = None
    for position, colony in candidates:
        reach = min(distance(home, position) for home in mine)
        if reach > SUPPLY_RANGE_LY:
            continue
        # Closest first, ties by id: a determined choice, and the one a player
        # would recognise as the obvious target.
        key = (reach, colony.id)
        if best is None or key < best:
            best, chosen = key, colony

    return chosen


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
    garrison = len(colonies) * turn.doctrine.garrison_per_colony
    over = sum(f.strength for f in warships) - garrison
    if over <= 0 and civ.upkeep_paid >= 1.0 - 1e-9:
        return

    # Out of the orders already in hand rather than a fresh query: a decision
    # this cheap should not cost the tick a round trip.
    busy = {
        intent.payload.get("fleet_id") for group in pending.values() for intent in group
    } | turn.claimed
    docked = [
        fleet
        for fleet in warships
        if not fleet.in_transit and fleet.id not in busy and _at_a_colony(fleet, colonies)
    ]
    if not docked:
        return

    # The smallest hull that is surplus to requirements, so the empire sheds the
    # least capability it can while still shedding the bill.
    scrapped = min(docked, key=lambda f: (f.strength, f.id))
    intents.decommission_fleet(session, civ, scrapped.id)
    turn.claimed.add(scrapped.id)


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
    busy |= turn.claimed
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
    if turn.doctrine.settles_by_quality:
        return _best_settleable_world(turn, fleet, claimed)

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


#: What settling for quality is actually looking for, and why each one.
#:
#: Habitability decides how much of the colony's population is occupied merely
#: staying alive; land decides how many there can ever be. **Geology is the
#: interesting one.** A capital was once found holding eight billion tonnes of
#: iron and no steel, because smelting also wants carbon and nobody had settled
#: anywhere carbon-bearing -- so a competent player facing a shortage goes and
#: takes a world that ends it. Weighing the crust is how the AI does the same.
SETTLE_HABITABILITY_WEIGHT = 60.0
SETTLE_LAND_WEIGHT = 8.0
SETTLE_GEOLOGY_WEIGHT = 30.0
SETTLE_DISTANCE_PENALTY = 1.0
#: Reference land area, so the land term is a ratio rather than a raw km².
SETTLE_REFERENCE_LAND_KM2 = 1.5e8


def _best_settleable_world(
    turn: "_Turn", fleet: Fleet, claimed: set[int]
) -> tuple[World, StarSystem] | None:
    """The world most worth having, rather than the one that is closest.

    Distance still counts -- a rich world outside supply range is a liability,
    not a prize -- but it is a penalty on a score rather than the whole of it.

    Reads promoted columns only (``habitability``, ``land_area_km2``,
    ``extraction``), never the survey document: the AI must not pay to decode a
    planet per candidate, and it must not learn anything a player's charts would
    not already show.
    """
    wanted = _scarce_inputs(turn)
    best: tuple[float, World, StarSystem] | None = None

    for system in turn.systems:
        span = distance(fleet.position, system.position)
        for world in sorted(system.worlds, key=lambda w: w.id):
            if world.colony is not None or world.id in claimed:
                continue

            yields = world.extraction or {}
            geology = sum(yields.get(material, 0.0) for material in wanted)
            score = (
                world.habitability * SETTLE_HABITABILITY_WEIGHT
                + min(1.0, (world.land_area_km2 or 0.0) / SETTLE_REFERENCE_LAND_KM2)
                * SETTLE_LAND_WEIGHT
                + geology * SETTLE_GEOLOGY_WEIGHT
                - span * SETTLE_DISTANCE_PENALTY
            )
            # Ties break on world id, so two equally good rocks always resolve
            # the same way and the turn stays replayable.
            if best is None or (score, -world.id) > (best[0], -best[1].id):
                best = (score, world, system)

    return (best[1], best[2]) if best else None


def _scarce_inputs(turn: "_Turn") -> tuple[str, ...]:
    """Raw materials this empire is shortest of, relative to what it holds.

    The same "make what you are short of" rule the governor refines under, asked
    about the ground instead of the warehouse. Deliberately coarse: it wants to
    know whether to prefer a carbon world over another iron one, not to rank
    twenty-nine elements.
    """
    held: dict[str, float] = {}
    for colony in turn.colonies:
        for material, amount in colony.stockpile.items():
            if material in RAW_MATERIALS:
                held[material] = held.get(material, 0.0) + max(0.0, amount)
    if not held:
        return RAW_MATERIALS[:6]
    ordered = sorted(RAW_MATERIALS, key=lambda m: (held.get(m, 0.0), m))
    return tuple(ordered[:6])


def _can_carry_more_upkeep(turn: "_Turn", colonies, fleets, extra_strength: float) -> bool:
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
    if turn.civ.upkeep_paid < 1.0 - 1e-9:
        return False

    strength = sum(f.strength for f in fleets) + extra_strength
    if strength <= 0:
        return True

    banked: dict[str, float] = {}
    for colony in colonies:
        for material in FLEET_UPKEEP_PER_STRENGTH:
            banked[material] = banked.get(material, 0.0) + colony.stockpile.get(material, 0.0)

    return all(
        banked.get(material, 0.0)
        >= per_strength * strength * turn.doctrine.upkeep_reserve_hours
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
    # More than one planet at a time, if the doctrine says so. Strictly one was
    # why a world took four months even once projects were affordable: a full
    # transformation is fourteen of them, so a civ running them in sequence
    # waits on its own queue rather than on its industry.
    queued = pending.get(IntentKind.TERRAFORM.value, [])
    # A campaign is one that is *happening*. Counting orders that have never
    # started against the limit is how a civilization stopped terraforming
    # altogether: measured, fifteen of fifteen live orders had never begun,
    # waiting a median of thirty-two days and up to forty-nine for materials
    # nobody had. Two of those permanently filled a driven opponent's two slots,
    # so it could not begin a project on a world it could have paid for that
    # afternoon. A queued order standing in for an active campaign -- the same
    # substitution as free colony pods and alphabetical refining priorities.
    running = [
        intent for intent in queued if intent.status == IntentStatus.IN_PROGRESS.value
    ]
    _abandon_unfundable(turn, queued)
    if len(running) >= max(1, turn.doctrine.terraform_campaigns):
        return
    under_way = {intent.payload.get("colony_id") for intent in queued}

    colonies = turn.colonies
    for colony in colonies:
        if colony.id in under_way:
            continue  # already being reshaped
        if colony.world.habitability > turn.doctrine.terraform_habitability:
            continue
        # Worlds that can actually be finished, and only those. Two thirds of
        # the galaxy cannot be: their air holds them above the growing band and
        # no amount of orbital shade brings them down, because reflection
        # saturates. Nothing said so, so a civilization would commit to one,
        # spend nine projects' worth of industry watching the albedo climb, and
        # stop for ever a long way short of anywhere habitable. Spreading that
        # across a dozen rocks is why no world in a sixty-day soak ever became
        # somewhere people could live.
        if not colony.world.terraform_finishable:
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
        if muscle < turn.doctrine.terraform_minimum_neighbourhood_work:
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


def _abandon_unfundable(turn: "_Turn", queued: list[Intent]) -> None:
    """Give up on a terraforming order the empire has never been able to pay for.

    Not impatience -- a project is meant to be waited for, and one that starts a
    week late is a project that started. This is about an order that will *never*
    start: the ones measured here had been queued for a month and a half against
    materials the civilization held none of anywhere, and they were still sitting
    there at the end of the run.

    Letting go is what makes the empty slot mean something. The world is not lost
    -- nothing about it changed, it is still on the list, and the moment the
    warehouses can cover the bill the same order is placed again. What is gained
    is that in the meantime the civilization is allowed to reshape somewhere it
    *can* afford, instead of standing still holding a receipt.
    """
    session = turn.session
    for intent in queued:
        if intent.status != IntentStatus.QUEUED.value:
            continue
        if turn.universe.tick_number - intent.queued_tick < TERRAFORM_PATIENCE_HOURS:
            continue
        intents.cancel(session, intent)


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
            SUPPLY_RANGE_LY * turn.doctrine.scout_range_fraction,
            limit=turn.doctrine.scout_candidates,
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
                turn.claimed.add(scout.id)
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
    # How many hulls a civ will have on the slipways at once. One at a time is
    # a hard throttle now that a colony pod costs a fortnight of a capital's
    # yard: a finished ship is not replaced until the next decision, and a
    # competent player keeps the queue fed. The cap that matters stays economic
    # -- what a yard can pay for and what the fleet's upkeep will bear.
    if len(pending.get(IntentKind.BUILD_FLEET.value, [])) >= turn.doctrine.build_queue_depth:
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
    if not _can_carry_more_upkeep(turn, colonies, fleets, BUILD_STRENGTH):
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
    if warships + BUILD_STRENGTH > len(colonies) * turn.doctrine.garrison_per_colony:
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
