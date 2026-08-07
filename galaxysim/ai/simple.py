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
from sqlalchemy.orm import Session

from galaxysim.ai.doctrine import doctrine as doctrine_for
from galaxysim.colony.expedition import Loadout
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
from galaxysim.engine.resolvers.terraform import construction_per_hour
from galaxysim.engine.resolvers.production import (
    DOCKING_TOLERANCE_LY,
    SUPPLY_RANGE_LY,
    colony_effects,
    effective_habitability,
    water_per_hour,
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

#: The level a standing route keeps a supplied colony topped up to -- a *stock*
#: to maintain, not a quantity to ship, which is what the manifest means since
#: routes learned to read the far end's warehouse.
#:
#: Water is sized per colony from what it actually drinks; these are the floors
#: for a colony that drinks nothing measurable, and the fixed levels for the two
#: goods whose consumption the AI does not model.
ROUTE_MANIFEST = {WATER: 12_000.0, FOOD: 1_500.0, FERTILISER: 500.0}

#: Hours of its own consumption a route tries to keep standing at a destination.
#:
#: A round trip was **measured at 140 hours** across sixty days of soak, against
#: a flat twelve-thousand-tonne water manifest and a typical outpost burning 210
#: tonnes an hour -- so a route delivered about forty percent of what its
#: destination drank between visits, and the shortfall was invisible because the
#: number shipped never had anything to do with the number consumed. Two round
#: trips of cover, so a single missed or slow trip is survivable rather than
#: fatal.
ROUTE_COVER_HOURS = 300.0

BUILD_STRENGTH = 2.0
BUILD_RESERVE = 2.0  # only build if it can afford this many such fleets

#: A freighter is a hull built with almost no weapons and a great deal of hold.
FREIGHTER_STRENGTH = 0.5
#: Hold above which a hull is a freighter rather than a fighting ship. Every
#: warship carries a few dozen tonnes incidentally; a route ship carries
#: thousands, so there is a wide gap and nothing sits in it.
FREIGHTER_HOLD_TONNES = 100.0
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
        "session", "universe", "civ", "doctrine", "claimed", "on_station",
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
        # Fleets holding a blockade. Filled once per turn by :func:`_besieging`.
        #
        # Kept beside ``claimed`` because it is the same idea reached from the
        # other direction: ``claimed`` is a job just given, this is a job already
        # being done that nothing wrote down. Every helper that looks for a spare
        # ship must consult both, or it will find one of these and re-task it --
        # scouting did, and so did supply, and each of them quietly ended a war.
        self.on_station: set[int] = set()
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

    def orders(self, kind: str) -> list[Intent]:
        """This civ's standing orders of one kind, off one read for the galaxy.

        Two decisions ask "have I already got one of these" by selecting the
        *whole universe's* orders of a kind and then filtering to themselves,
        which at eight opponents is eight reads of identical rows -- 422 a tick
        at 120 days. Shared like the star charts and the garrisons, and for the
        same reason.

        Safe because a civ only ever reads its own slice: an order another
        opponent queued later in the same tick was never in this answer anyway.
        """
        cache = self._shared.setdefault("orders", {})
        if kind not in cache:
            grouped: dict[int, list[Intent]] = {}
            for intent in queries.active_intents(self.session, self.universe.id, kind):
                grouped.setdefault(intent.civ_id, []).append(intent)
            cache[kind] = grouped
        return cache[kind].get(self.civ.id, [])

    @property
    def garrisons(self) -> list[tuple[Vec3, int, float]]:
        """Where every fleet in the galaxy is standing, and whose it is.

        Position, owner and strength -- nothing else, because nothing else is
        needed and a fleet's cargo is not the AI's business. Read once per tick
        and shared between civilizations, like the star charts and for the same
        reason: eight opponents each re-reading the same picture is eight times
        the same query.

        Fair game to look at. A fleet is a physical object sitting in a charted
        system, which is exactly what a scout is for; the guard in
        ``tests/test_ai.py`` is about a planet's *interior*, which cannot be seen
        without surveying it. What this is not allowed to become is a way to read
        somebody's intentions -- only where their ships are, which anyone
        standing there can see too.
        """
        if "garrisons" not in self._shared:
            self._shared["garrisons"] = [
                (fleet.position, fleet.civ_id, fleet.strength)
                for fleet in queries.fleets(self.session, self.universe.id)
                if not fleet.in_transit and fleet.strength > 0
            ]
        return self._shared["garrisons"]

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
            self._shared["systems"] = queries.charted_systems(
                self.session, self.universe.id
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

    # Before anything reads the war: is there still one? A standing attack order
    # outlives the fleet that was prosecuting it, and until this ran the answer
    # was always yes, for ever. See :func:`_maybe_make_peace`.
    _maybe_make_peace(turn, pending)

    # Which ships are already keeping a cordon, before anything decides they
    # look spare. See :func:`_besieging`.
    turn.on_station = _besieging(turn, pending)

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
    # And the warships, for the same reason and in the same place: a war already
    # being fought comes before one being started. See :func:`_maybe_reinforce`.
    _maybe_reinforce(turn, pending)
    _maybe_raid(turn, pending)
    # Then bring home anything that is dying for want of a warehouse. After the
    # war decisions, because a cordon is a job and this must not undo one; before
    # everything below, because a ship that is starving is worth rescuing ahead
    # of being sent to look at another star. See :func:`_maybe_withdraw`.
    _maybe_withdraw(turn, pending)
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
            intents.move_fleet_to_system(session, civ, fleet.id, system, reason="expand")
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
    existing = {
        intent.payload.get("dest_colony_id"): intent
        for intent in _all_routes(turn)
    }
    _refresh_route_levels(colonies, existing)

    for colony in colonies:
        if colony.id == source.id or colony.id in existing:
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
                at=source,
            ):
                return
            _order_freighter(session, civ, source, pending)
            return
        intents.supply_route(
            session, civ, fleet.id, source.id, colony.id, _route_levels(colony)
        )
        turn.claimed.add(fleet.id)
        return


def _refresh_route_levels(colonies: list[Colony], existing: dict) -> None:
    """Raise a standing route's target when the world at the end of it has grown.

    A route's levels are decided once, when the order is placed, and an outpost
    is smallest exactly then -- fifty thousand settlers. It grows to a quarter of
    a million within weeks and drinks four times as much, while the order goes on
    asking for the amount that suited the landing party.

    Measured, that was the whole of the remaining problem. With the target frozen
    at creation, destinations sat at roughly seventy hours of cover against a
    round trip of about the same, so every one of them ran down to nothing just
    as the next delivery finished -- twenty-one of sixty-three under a day of
    water, two at zero, while their freighters were all visibly loading, flying
    and unloading. Nothing was stuck. The number was simply stale.

    Only ever upward. A colony that shrinks does not need its supply cut on the
    same tick, and letting the target fall would make a dying world die faster.
    """
    by_id = {colony.id: colony for colony in colonies}
    for colony_id, intent in existing.items():
        colony = by_id.get(colony_id)
        if colony is None:
            continue
        manifest = dict(intent.payload.get("manifest") or {})
        wanted = _route_levels(colony)
        raised = {
            material: max(amount, manifest.get(material, 0.0))
            for material, amount in wanted.items()
        }
        if any(raised[m] > manifest.get(m, 0.0) * 1.1 for m in raised):
            intent.payload = {**intent.payload, "manifest": {**manifest, **raised}}


def _route_levels(destination: Colony) -> dict[str, float]:
    """What a route should keep standing at ``destination``.

    Water against what this particular colony drinks, because that is the term
    that varies by four orders of magnitude across an empire -- a fifty-thousand
    person outpost on a mild world and a ten-billion-person world sealed against
    vacuum are the same order in every other respect and nothing alike in this
    one. The flat twelve thousand tonnes the AI used to ask for was a number
    that had stopped meaning anything: measured, it covered about forty percent
    of what a destination drank between visits.
    """
    levels = dict(ROUTE_MANIFEST)
    burn = water_per_hour(destination, colony_effects(destination))
    if burn > 0:
        levels[WATER] = max(levels[WATER], burn * ROUTE_COVER_HOURS)
    return levels


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
    if turn.orders(IntentKind.MIGRATE.value):
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


def _all_routes(turn: "_Turn") -> list[Intent]:
    return turn.orders(IntentKind.SUPPLY_ROUTE.value)


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
    #
    # This reads "not currently prosecuting a war", not "has ever declared one".
    # The difference is :func:`_maybe_make_peace`, which runs first and drops the
    # order once nothing of this civ's is standing over anything of theirs --
    # without it this guard was true for the rest of the game the moment it first
    # came true, and every AI in every soak declared exactly one war, ever.
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
        if _is_fighting_hull(fleet)
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

    target = _raidable_colony(turn, strength)
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
            intents.move_fleet_to_system(session, civ, fleet.id, system, reason="raid")


def _defenders(turn: "_Turn", where: Vec3, owner: int) -> float:
    """Strength the owner of a world has standing over it.

    Only the owner's own ships. A third civilization's fleet passing through is
    not defending anything, and counting it would talk this opponent out of
    attacks it would have won.
    """
    return queries.strength_at(turn.garrisons, where, owner, BLOCKADE_RANGE_LY)


def _besieging(turn: "_Turn", pending: dict[str, list[Intent]]) -> set[int]:
    """This civ's fleets holding station over a colony it is at war with.

    They have a job -- the most important one the civilization currently has --
    and it is a job that exists nowhere in the order queue, because keeping a
    cordon is *staying put*. Every other helper here works out whether a ship is
    free by looking for an order attached to it, so a blockade is invisible to
    all of them and the ship reads as spare.
    """
    at_war = {
        intent.payload.get("target_civ_id")
        for intent in pending.get(IntentKind.ATTACK.value, [])
    }
    if not at_war:
        return set()

    besieged = [
        colony.world.system.position
        for colony in queries.visible_rivals(turn.systems, turn.civ.id)
        if colony.civ_id in at_war
    ]
    if not besieged:
        return set()

    return {
        fleet.id
        for fleet in turn.fleets
        if not fleet.in_transit
        and any(distance(fleet.position, where) <= BLOCKADE_RANGE_LY for where in besieged)
    }


def _maybe_make_peace(turn: "_Turn", pending: dict[str, list[Intent]]) -> None:
    """Drop a war nobody is fighting any more.

    An attack order is standing by design -- a war must not lapse because
    somebody was offline for a week -- and nothing in the engine ever completes
    one. That is right for the *order*. It was wrong for the opponent, because
    :func:`_maybe_raid` opens by refusing to declare while one is outstanding, so
    the first war an AI ever declared was also its last. A civilization whose
    raid was destroyed in the first week spent the remaining hundred and ten days
    permanently hostile to somebody it was not fighting and unable to fight
    anybody else. A 120-day soak produced two wars between eight civilizations,
    one of which took a world, and both orders were still standing at the end.

    So: **a war this civilization is not prosecuting is over.** Prosecuting means
    a warship of its own standing over a colony of that rival, or on its way to
    one -- the same cordon :func:`_besieging` reads and the same one the siege
    resolver charges for, rather than a memory of an objective nobody recorded.
    That one rule covers both endings without needing to tell them apart:

    - **The raid is spent.** Its ships are gone, or they went home. Nobody is
      standing over anything, and there is no fleet to reinforce with because the
      AI does not send second waves.
    - **The objective was taken.** The world its ships are parked over is *its
      own* now, so it is no longer standing over a rival's colony at all.

    Cancelling goes through :func:`galaxysim.engine.intents.cancel`, which is the
    call a player has and the only one -- there is no separate ending-a-war
    mechanism, and the README's claim that a standing attack order is the whole
    surface of war stays true. What changes is only that the AI can now notice
    the war is over, rebuild, and pick a fight it can win.

    The order list is edited in place because every decision after this one reads
    ``pending`` rather than the database: a raid cancelled here must be invisible
    to :func:`_besieging` and :func:`_maybe_annex` on the same turn, or ships stay
    pinned to a cordon for a war that has just ended.
    """
    orders = pending.get(IntentKind.ATTACK.value)
    if not orders:
        return

    theirs: dict[int, list[Vec3]] = {}
    for colony in queries.visible_rivals(turn.systems, turn.civ.id):
        theirs.setdefault(colony.civ_id, []).append(colony.world.system.position)

    standing: list[Intent] = []
    for intent in orders:
        target = intent.payload.get("target_civ_id")
        if any(_prosecuting(turn, where) for where in theirs.get(target, ())):
            standing.append(intent)
            continue
        intents.cancel(turn.session, intent)
    pending[IntentKind.ATTACK.value] = standing


def _prosecuting(turn: "_Turn", where: Vec3) -> bool:
    """Whether this civ has a warship over ``where``, or one heading there.

    In transit counts, and has to: the raid's ships are ordered to the target on
    the same turn war is declared, so for the days they spend crossing there is
    nobody standing over anything. Judging the war on presence alone would cancel
    it the tick after it began.
    """
    for fleet in turn.fleets:
        if fleet.strength <= 0:
            continue  # a lander holds no cordon; only a fleet that can fight does
        at = _destination(fleet) if fleet.in_transit else fleet.position
        if at is not None and distance(at, where) <= BLOCKADE_RANGE_LY:
            return True
    return False


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

            intents.move_fleet_to_system(session, civ, lander.id, system, reason="annex")
            turn.claimed.add(lander.id)
            return


def _maybe_reinforce(turn: "_Turn", pending: dict[str, list[Intent]]) -> None:
    """Send a second wave to a cordon that is being outweighed.

    The other half of :func:`_maybe_annex`, and missing for the same reason it
    was: a raid dispatched every ship it would ever send at the moment war was
    declared, and :func:`_maybe_raid` refuses to declare while an attack order
    stands, so *nothing in this AI ever sent a warship at an enemy again*. For
    the whole length of a war every hull the civilization built sat at home --
    and :func:`_maybe_scrap`, which counts strength above the garrison as
    surplus, would break up the very ships that should have been the second wave
    while the first was being ground down.

    That made a war a coin flip resolved on the day it was declared. Win the
    opening engagement and there was nothing further to answer.

    **What a cordon actually loses to is time, not battle.** The first version of
    this topped a cordon up when the defenders outweighed it, and across 120 days
    of eight driven opponents it fired *zero times*: sampled daily over every
    standing war and every colony of its target, the cordon was winning 28 times,
    crossing 18, and outweighed never. It cannot be otherwise --
    :func:`_raidable_colony` only picks worlds it already outweighs, and a
    frontier outpost has no fleet over it at all, so the defenders are usually
    nothing.

    What does happen is that the blockade *expires*. Upkeep is billed to colonies
    near a fleet and there is nothing to draw on in somebody else's space, so a
    cordon deserts away hour by hour -- and of fifteen blockades established, only
    eight ever ground their world down. So the question is not "am I being
    outweighed" but "is there still enough here to finish", and the strength that
    answers it is :attr:`~galaxysim.ai.doctrine.Doctrine.raid_strength`: what this
    opponent thinks an objective is worth committing. A cordon below that gets
    topped back up to it.

    Two limits, and they are the whole design, because the failure mode this
    invents is worse than the one it fixes:

    **It never empties the home systems.** It keeps back what :func:`_maybe_raid`
    keeps back -- a raid's worth of strength, free, at home -- because an opponent
    that spends everything to hold an outpost has not become harder to play
    against, it has become easier.

    **It never feeds a fight it cannot win.** Whatever goes in must leave the
    cordon *past* the defenders standing over that world -- the same judgement
    :func:`_raidable_colony` makes before picking a target at all. Half a wave is
    worth nothing: a blockade that does not outweigh the garrison cuts no supply
    line and grinds itself down for as long as it lasts, so a civ that cannot tip
    the balance sends nobody and lets :func:`_maybe_make_peace` stand the war
    down. That case is rare against this AI and routine against a player, who can
    park a defence fleet over a world they can see is threatened.

    Both numbers come off ``turn.garrisons``, which is where a fleet is standing
    and whose it is -- the same picture a player gets by looking, and the same one
    the raid reads before it commits.
    """
    session, civ = turn.session, turn.civ
    at_war = {
        intent.payload.get("target_civ_id")
        for intent in pending.get(IntentKind.ATTACK.value, [])
    }
    if not at_war:
        return

    colonies = turn.colonies
    if not colonies:
        return

    busy = {
        intent.payload.get("fleet_id") for group in pending.values() for intent in group
    } | turn.claimed | turn.on_station
    warships = [
        fleet
        for fleet in turn.fleets
        if _is_fighting_hull(fleet)
        and not fleet.in_transit
        and fleet.id not in busy
        and fleet.strength > 0
    ]
    if not warships:
        return

    # What it may spend, by the raid's own standard: :func:`_maybe_raid` commits
    # ``raid_strength`` only when it holds twice that free, so it always leaves a
    # raid's worth at home. This leaves the same, because there must be one
    # answer to "what will this opponent spend on a war" rather than two.
    #
    # It is deliberately *not* the doctrine garrison, and that is measured rather
    # than argued: reinforcement gated on that floor bailed on 708 of the 730
    # turns it ran and dispatched nothing, ever -- while ``_maybe_raid``, on the
    # same turns, was perfectly willing to declare a new war. Two answers to the
    # same question, and the stricter one was unreachable.
    #
    # The reason it is unreachable is that a driven civ's warship strength falls
    # away from the garrison line after about eighty days -- 43 against 50 at day
    # 84, then 19 against 65 at day 112 -- which is exactly the window its wars
    # fall in. Why it collapses is an open question and a gap bullet in the
    # README; that it does is enough to keep this decision off that number.
    free = sum(fleet.strength for fleet in warships)
    sendable = free - turn.doctrine.raid_strength
    if sendable <= 0:
        return

    for system in turn.systems:
        for world in system.worlds:
            colony = world.colony
            if colony is None or colony.civ_id not in at_war:
                continue

            where = system.position
            cordon = queries.strength_at(turn.garrisons, where, civ.id, BLOCKADE_RANGE_LY)
            # Ships already crossing count, or a civ sends a fresh wave every
            # tick of the week they spend in flight and arrives with its whole
            # navy at a world it needed two hulls for.
            inbound = sum(
                fleet.strength
                for fleet in turn.fleets
                if fleet.in_transit
                and fleet.strength > 0
                and _destination(fleet) is not None
                and distance(_destination(fleet), where) <= BLOCKADE_RANGE_LY
            )
            holding = cordon + inbound
            if holding <= 0.0:
                continue  # not an objective of ours; nothing to reinforce

            defenders = _defenders(turn, where, colony.civ_id)
            # Enough to outweigh whoever is defending, and enough to finish the
            # job -- the commitment this opponent thinks an objective is worth.
            wanted = max(turn.doctrine.raid_strength, defenders)
            if holding > defenders and holding >= wanted:
                continue  # the blockade is up and deep enough; it needs nothing

            # Heaviest first, and skip anything that would not fit under the
            # garrison line rather than stopping at it -- a wing too big to spare
            # must not shut out the frigate that would have tipped the balance.
            committed: list[Fleet] = []
            spent = 0.0
            strength = holding
            for fleet in sorted(warships, key=lambda f: (-f.strength, f.id)):
                if spent + fleet.strength > sendable:
                    continue
                committed.append(fleet)
                spent += fleet.strength
                strength += fleet.strength
                if strength > defenders and strength >= wanted:
                    break
            if strength <= defenders or not committed:
                return  # cannot tip it; do not feed it

            for fleet in committed:
                turn.claimed.add(fleet.id)
                intents.move_fleet_to_system(session, civ, fleet.id, system, reason="reinforce")
            return


def _maybe_withdraw(turn: "_Turn", pending: dict[str, list[Intent]]) -> None:
    """Bring a starving ship back to somewhere that can pay it.

    **Nothing in this file reacted to a fleet going unsupplied, at all.** Walk
    the decisions: ``_maybe_scout`` is the only one that ever moves an idle
    warship and it moves them to *uncharted* systems, which by definition have no
    colony; ``_maybe_scrap`` needs a hull docked at a colony *and* surplus to
    garrison, which a ship dying in empty space is neither; and reinforce, raid
    and annex all send ships **out**. So a fleet that ended up somewhere with no
    supply had no way back, and bled at
    :attr:`Rates.unpaid_fleet_attrition_per_hour` until it was gone.

    Sampled at day 72, five fleets in two hundred and fifty had *zero percent* of
    their bill within reach while every other fleet had seven to thirty times
    theirs. No middle: a fleet is either where the materials are made, or where
    none of them are.

    **Five is a rate, not a population**, and the first draft of this docstring
    read it as the latter -- "the same ships, going short for months." At
    :attr:`Rates.unpaid_fleet_attrition_per_hour` of 0.1 an hour, a fully
    unsupplied fleet has a **6.6-hour half-life** and is swept up at
    :attr:`Rates.fleet_destruction_threshold` inside about **28 hours**. Nothing
    starves for months; it starves for a day and dies. The 7,176 shortfall events
    of a 120-day run are ~300 fleet-days of starvation, which at that half-life
    is *hundreds of different hulls* walking into dead ground and dying there --
    a flow of new construction, not a stock of stranded veterans.

    So this rescues what it can catch, and it is racing a clock: an hourly turn
    against a 6.6-hour half-life saves a fleet noticed early and buries one
    noticed late. Measured over days 40-90 it fires on 2.4% of turns and takes
    shortfall events down 21% across a 120-day run -- real, and bounded by that
    race. What it is *not* is a whole explanation of the late-run strength curve;
    that has never been decomposed into desertion versus battle damage, and
    :mod:`galaxysim.engine.resolvers.combat` is the other of the only two places
    in the engine that reduce ``Fleet.strength``.

    Watch a fleet starve and you move it. The AI could not, because the action
    did not exist -- which is worth fixing whatever the strength curve turns out
    to be made of.

    **What it deliberately will not touch.** A fleet in ``turn.on_station`` is
    holding a cordon, and a blockade deep in somebody else's space is *supposed*
    to starve -- that is how sieges end. Withdrawing those would dissolve every
    war the civilization is prosecuting and read, on a fleet-strength table, as a
    triumph. ``_besieging`` exists precisely because a ship keeping a blockade
    carries no order and looks idle to everything here.
    """
    session, civ = turn.session, turn.civ
    colonies = turn.colonies
    if not colonies:
        return

    moving = {i.payload.get("fleet_id") for i in pending.get(IntentKind.MOVE_FLEET.value, [])}

    for fleet in turn.fleets:
        if fleet.strength <= 0 or fleet.in_transit:
            continue
        if fleet.id in turn.on_station or fleet.id in turn.claimed or fleet.id in moving:
            continue
        if not _is_fighting_hull(fleet):
            continue  # a freighter's job is to be away from home

        owed = {
            material: per_strength * fleet.strength
            for material, per_strength in FLEET_UPKEEP_PER_STRENGTH.items()
        }
        # The same neighbourhood the biller draws from, asked the same way.
        near = queries.sorted_by_distance(
            colonies, fleet.position, within_ly=SUPPLY_RANGE_LY
        )
        held = {
            material: sum(colony.stockpile.get(material, 0.0) for colony in near)
            for material in owed
        }
        if all(held[material] >= amount for material, amount in owed.items()):
            continue  # it is being fed where it stands

        # Somewhere that could actually cover an hour of it, nearest first.
        haven = next(
            (
                colony
                for colony in queries.sorted_by_distance(colonies, fleet.position)
                if all(
                    colony.stockpile.get(material, 0.0) >= amount
                    for material, amount in owed.items()
                )
            ),
            None,
        )
        if haven is None:
            continue  # nowhere to go; moving would only starve it somewhere else

        turn.claimed.add(fleet.id)
        intents.move_fleet_to_system(
            session, civ, fleet.id, haven.world.system, reason="withdraw"
        )
        return  # one rescue a turn, like every other decision here


def _destination(fleet: Fleet) -> Vec3 | None:
    """Where a fleet in transit is headed, if it is going anywhere."""
    if fleet.dest_x is None or fleet.dest_y is None or fleet.dest_z is None:
        return None
    return Vec3(fleet.dest_x, fleet.dest_y, fleet.dest_z)


def _is_fighting_hull(fleet: Fleet) -> bool:
    """Anything that is not a freighter. Includes settlers.

    The AI builds most of its hulls with a colony pod attached, so the ship that
    plants a flag is usually also the ship that holds a cordon. This population
    answers "what could I send at a war": it is what :func:`_maybe_raid` commits
    from and what :func:`_maybe_reinforce` reserves against.
    """
    return fleet.cargo_capacity <= FREIGHTER_HOLD_TONNES


def _is_only_a_warship(fleet: Fleet) -> bool:
    """A fighting hull with no pod aboard: nothing to do but fight or scout.

    The *other* population, and the distinction is load-bearing rather than
    tidy. A hull that has landed its pod becomes one of these, which is why they
    accumulate. The standing-navy line in :func:`_maybe_build` and the surplus in
    :func:`_maybe_scrap` are both drawn across this set, and :func:`_maybe_scout`
    picks from it because a settler and a freighter each already have somewhere
    to be.

    Measuring one of these two against the other's threshold is a mistake this
    module invited for a long time: the filters were written inline at five call
    sites and neither had a name, so a 120-day reading of "the navy" against the
    garrison line compared settlers-included strength to a warships-only cap and
    drew exactly the wrong conclusion.

    Neither predicate asks about damage. Whether a hull is a warship is a fact
    about how it was built; whether it is any use is a separate question, and the
    callers that care add ``strength > 0`` themselves.
    """
    return _is_fighting_hull(fleet) and fleet.colony_pods <= 0


def _raidable_colony(turn: "_Turn", committing: float) -> Colony | None:
    """A rival's frontier world this civ could plausibly besiege and hold.

    Read off the star charts the AI already has loaded, so a war costs the tick
    no queries at all. Those charts are exactly what a player can see -- systems
    somebody has actually visited -- which is what keeps this an opponent
    playing the game rather than one reading the database.
    """
    ceiling = turn.doctrine.raid_population_ceiling
    # The shared definition of what this civilization can see of its
    # neighbours, so the opponent and the player are looking at one picture
    # rather than two that have to be kept in step by hand.
    candidates = [
        (colony.world.system.position, colony)
        for colony in queries.visible_rivals(turn.systems, turn.civ.id)
        if colony.population <= ceiling
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
        # Somewhere the force being sent could actually hold.
        #
        # A blockade is *more* strength over the world than its owner has, so a
        # raid that arrives outweighed does not merely fail -- it never cuts a
        # single supply line, and it grinds itself down against the defenders
        # for as long as it survives. The target used to be chosen on distance
        # and population alone, which meant a driven opponent would send twelve
        # points of strength at a world guarded by thirty and lose them.
        #
        # An opponent that throws fleets away is not a difficult opponent, it is
        # a stupid one. Same reasoning as the population ceiling above: rarity
        # and hopelessness are fine things for a *player* to walk into, and a
        # bad look on an AI that had every chance to check.
        if _defenders(turn, position, colony.civ_id) >= committing:
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
        if _is_only_a_warship(fleet)
    ]
    if not warships:
        return

    # Freighters are never candidates: an outpost dies without its route, so a
    # civ short of fuel must not balance its books by cutting the supply line.
    #
    # Surplus is the *only* trigger. Insolvency used to be a second one -- the
    # argument being that a hull you cannot pay for is lost either way, and
    # scrapping at least returns a third of the materials while desertion returns
    # nothing. Sound, and measured it was a liquidation spiral: any shortfall at
    # all, however small, made every docked warship a candidate at one hull per
    # decision, which for a driven opponent is hourly. Over 120 days five of eight
    # civilizations lost most of their navy that way -- one shed **45.6 strength
    # to desertion and sixteen hulls to scrapping inside a fortnight**, then
    # rebuilt twelve points of it eight days later, which is what says the
    # shortage was a dip rather than a verdict.
    #
    # The salvage does not even answer the shortage. Upkeep is fuel and alloys;
    # breaking a hull returns alloys, steel and electronics. A civ short of fuel
    # sells its navy and is still short of fuel, having paid a third of build
    # cost for the privilege. Desertion prices that failure well enough on its
    # own; this decision now only sheds what the empire was not trying to keep.
    garrison = len(colonies) * turn.doctrine.garrison_per_colony
    over = sum(f.strength for f in warships) - garrison
    if over <= 0:
        return

    # A hull is only surplus in peacetime. While a war is on, the ships above the
    # garrison are the second wave -- :func:`_maybe_reinforce` spends exactly that
    # margin -- and this function was breaking them up for materials while the
    # first wave was being ground down at the cordon. A civ that cannot pay its
    # crews still sheds: those hulls are surplus to the garrison either way, and
    # the bill is the more urgent problem.
    if civ.upkeep_paid >= 1.0 - 1e-9 and pending.get(IntentKind.ATTACK.value):
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
    busy |= turn.claimed | turn.on_station
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


def _can_carry_more_upkeep(
    turn: "_Turn", colonies, fleets, extra_strength: float, at: Colony | None = None
) -> bool:
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

    Asked, too, **only of the warehouses the fleets can actually reach**. That
    half was missing for a long time and it is the same error one dimension
    over: upkeep is billed from colonies within :data:`SUPPLY_RANGE_LY` of each
    fleet, so an empire-wide sum is a proxy for the question rather than the
    question. Measured over 120 days, fleets went short of alloys **4,730 times
    while their civilizations held 2.3 billion tonnes of them** -- with a mean
    of *two tonnes* inside supply range. The material was not missing; it was
    somewhere else, and no amount of it anywhere else pays a crew.

    Nor can a freighter fix that, which is why this is a limit on the navy
    rather than a job for logistics. A point of strength burns 3,000 tonnes an
    hour; a route ship holds 28,000 and takes 140 hours to go and come back, so
    it delivers 200 tonnes an hour and costs 1,500 in upkeep of its own. One
    strength-2 hull would need thirty freighters, each consuming seven times
    what it carries. **Fleets live where industry is, or they do not live.**

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

    # Asked where the hull would live, not across the empire. The union of every
    # fleet's neighbourhood is very nearly the whole empire once a civ has spread
    # out, so asking it that way is the same global sum wearing a local coat --
    # measured, it changed not one decision in 120 days. What binds is one
    # place: these warehouses, against everything already drawing on them.
    if at is None:
        return True
    here = at.world.system.position
    near = queries.sorted_by_distance(colonies, here, within_ly=SUPPLY_RANGE_LY)

    drawing = extra_strength + sum(
        fleet.strength
        for fleet in fleets
        if fleet.strength > 0 and distance(fleet.position, here) <= SUPPLY_RANGE_LY
    )
    if drawing <= 0:
        return True

    banked: dict[str, float] = {}
    for colony in near:
        for material in FLEET_UPKEEP_PER_STRENGTH:
            banked[material] = banked.get(material, 0.0) + colony.stockpile.get(material, 0.0)

    return all(
        banked.get(material, 0.0)
        >= per_strength * drawing * turn.doctrine.upkeep_reserve_hours
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
        # The shared per-colony estimate, summed the AI's own way.
        #
        # Deliberately *not* the distance-weighted pool the resolver feeds a
        # project (:func:`terraform.pooled_construction_per_hour`), which is what
        # the player's readout uses because it is what will really happen. This
        # is a cheaper question -- "is there enough industry near this rock to
        # bother" -- and the doctrine thresholds it compares against were
        # measured against exactly this sum over exactly this radius. Swapping in
        # the weighted figure would silently move every difficulty level's
        # appetite for terraforming, which is a balance change and not a tidy-up.
        muscle = sum(construction_per_hour(helper) for helper in neighbourhood)
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
    # A ship holding a blockade is not idle, however idle it looks.
    #
    # "Idle" here meant "carrying no move order", and a fleet that has *arrived*
    # carries none -- its order completed when it got there. So the one warship
    # keeping an enemy world cut off read as spare capacity, and every single
    # turn the AI sent it off to look at a star. Staged and watched: a besieger
    # of fifty strength was on station at one tick and gone at the next, the
    # colony's resistance recovered from fifty thousand back past a hundred, and
    # the lander that arrived behind it died alone. Sieges collapsed in six to
    # twenty-five hours and not one world has ever changed hands.
    #
    # Third time today that the same mistake has bitten in a different place:
    # a job nobody wrote down is a job that does not exist.
    idle = [
        fleet
        for fleet in turn.fleets
        if _is_only_a_warship(fleet)
        and not fleet.in_transit
        and fleet.id not in turn.on_station
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
                    reason="scout",
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
    if not _can_carry_more_upkeep(turn, colonies, fleets, BUILD_STRENGTH, at=colony):
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
    warships = sum(f.strength for f in fleets if _is_only_a_warship(f))
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
