"""Founding a colony as an investment decision.

There is no flat colonization fee and no surcharge for settling somewhere
nasty. Instead you compose an expedition -- colonists, equipment, life-support
stores -- and what you send is both what you pay and what the colony wakes up
with.

Hostility and distance are therefore priced *implicitly*, which is the point.
Nobody charges you extra for a toxic world; you simply cannot keep anyone alive
there without sending stores, and under local stockpiles a distant world cannot
be topped up quickly, so it needs more of them. The cost of a hard world is the
cost of what surviving it actually requires.

:func:`assess` computes the survival arithmetic up front so a player can see
what they are committing to. It never refuses. Sending colonists to a toxic rock
with no stores is a legal order and it kills them -- the game says so clearly
and then lets you do it.
"""

from __future__ import annotations

from dataclasses import dataclass

from galaxysim.core.resources import ENERGY, METAL, VOLATILES

#: Cost per unit of each expedition component.
COLONIST_COST: dict[str, float] = {METAL: 4.0, VOLATILES: 3.0}
EQUIPMENT_COST: dict[str, float] = {METAL: 12.0, ENERGY: 6.0}
#: Life-support stores are just volatiles, shipped as cargo and burned later.
STORES_COST: dict[str, float] = {VOLATILES: 1.0}

#: Infrastructure a colony gains per unit of equipment landed. Infrastructure
#: multiplies extraction, industry and research, so equipment is the difference
#: between a colony that is productive on arrival and one that must bootstrap.
INFRASTRUCTURE_PER_EQUIPMENT = 0.25
#: Floor, so even a bare landing can do something.
BASE_INFRASTRUCTURE = 0.5

#: What the CLI offers when the player does not compose one by hand.
DEFAULT_COLONISTS = 3.0
DEFAULT_EQUIPMENT = 4.0
DEFAULT_STORES = 40.0


@dataclass(frozen=True, slots=True)
class Loadout:
    """What an expedition carries."""

    colonists: float = DEFAULT_COLONISTS
    equipment: float = DEFAULT_EQUIPMENT
    stores: float = DEFAULT_STORES

    def __post_init__(self) -> None:
        if self.colonists <= 0:
            raise ValueError("an expedition needs colonists")
        if self.equipment < 0 or self.stores < 0:
            raise ValueError("equipment and stores cannot be negative")

    @classmethod
    def from_payload(cls, payload: dict) -> "Loadout":
        """Rebuild a loadout from an intent payload, falling back to defaults."""
        return cls(
            colonists=float(payload.get("colonists", DEFAULT_COLONISTS)),
            equipment=float(payload.get("equipment", DEFAULT_EQUIPMENT)),
            stores=float(payload.get("stores", DEFAULT_STORES)),
        )

    def as_payload(self) -> dict:
        return {
            "colonists": self.colonists,
            "equipment": self.equipment,
            "stores": self.stores,
        }

    def cost(self) -> dict[str, float]:
        """Resources the outfitting colony must supply."""
        total: dict[str, float] = {}
        for unit_cost, quantity in (
            (COLONIST_COST, self.colonists),
            (EQUIPMENT_COST, self.equipment),
            (STORES_COST, self.stores),
        ):
            for resource, amount in sorted(unit_cost.items()):
                total[resource] = total.get(resource, 0.0) + amount * quantity
        return total

    def starting_infrastructure(self) -> float:
        return BASE_INFRASTRUCTURE + self.equipment * INFRASTRUCTURE_PER_EQUIPMENT

    def starting_stockpile(self) -> dict[str, float]:
        """What is left standing on the ground after landing.

        Only the stores: equipment becomes infrastructure and colonists become
        population.
        """
        return {VOLATILES: self.stores} if self.stores > 0 else {}


@dataclass(frozen=True, slots=True)
class Assessment:
    """What settling a given world with a given loadout would mean."""

    loadout: "Loadout"
    cost: dict[str, float]
    habitability: float
    #: Volatiles per hour life support will burn once landed.
    burn_per_hour: float
    #: Hours the stores cover. ``None`` means indefinitely -- the world is
    #: habitable enough to need nothing.
    survival_hours: float | None
    #: True if the colony can sustain itself from local yields alone.
    self_sufficient: bool

    @property
    def needs_resupply(self) -> bool:
        return not self.self_sufficient and self.survival_hours is not None

    def summary(self) -> str:
        """One line a player can act on."""
        if self.self_sufficient:
            return "Self-sufficient: local volatiles cover life support."
        if self.survival_hours is None:
            return "No life support required."
        days = self.survival_hours / 24.0
        if self.survival_hours <= 0:
            return "WILL DIE ON ARRIVAL: no stores and nothing local to burn."
        return (
            f"Stores last about {days:.1f} days ({self.burn_per_hour:.2f} volatiles/hour). "
            "Needs a supply route before then."
        )


def assess(loadout: Loadout, world, rates) -> Assessment:
    """Work out whether this expedition can actually survive on ``world``.

    Deliberately pessimistic about local supply: it counts only what the world
    yields, not what a hoped-for supply route might bring. A player should be
    warned by the arithmetic they can rely on.
    """
    habitability = world.habitability
    cost = loadout.cost()

    if habitability >= 1.0:
        return Assessment(
            loadout=loadout,
            cost=cost,
            habitability=habitability,
            burn_per_hour=0.0,
            survival_hours=None,
            self_sufficient=True,
        )

    burn = (
        loadout.colonists
        * rates.life_support_per_pop_per_hour
        * (1.0 - habitability)
        * rates.volatiles_per_life_support
    )
    if burn <= 0:
        return Assessment(
            loadout=loadout,
            cost=cost,
            habitability=habitability,
            burn_per_hour=0.0,
            survival_hours=None,
            self_sufficient=True,
        )

    # Can the world itself keep up? Extraction is only part of the population,
    # so compare against a realistic share rather than the whole colony mining
    # volatiles and nobody breathing.
    local_yield = float(world.resource_yield.get(VOLATILES, 0.0))
    local_per_hour = (
        local_yield
        * loadout.colonists
        * 0.25  # a balanced allocation's extraction share
        * rates.extraction_per_worker_per_hour
        * loadout.starting_infrastructure()
    )

    return Assessment(
        loadout=loadout,
        cost=cost,
        habitability=habitability,
        burn_per_hour=burn,
        survival_hours=loadout.stores / burn,
        self_sufficient=local_per_hour >= burn,
    )
