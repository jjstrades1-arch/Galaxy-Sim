"""Combat between co-located fleets.

Two deliberate choices:

**Hostility is declared, not assumed.** Fleets only fight if one side holds a
standing ATTACK order against the other. Meeting a stranger in deep space is not
a war. Diplomacy is not simulated (players negotiate among themselves), so the
attack order is the entire mechanical surface of "we are at war" -- and keeping
it explicit means an ambiguous encounter never costs someone a fleet they were
not risking.

**Combat is attritional and seeded, not instant and random.** Damage is a
deterministic function of the strengths present, scaled by wall-clock hours
elapsed, with a bounded swing drawn from the tick seed. So a battle plays out
over hours rather than resolving the instant two fleets touch, an offline player
is not annihilated by one bad roll, and re-running the tick reproduces the exact
same result -- which is what lets a player trust a battle they slept through.
"""

from __future__ import annotations

from collections import defaultdict

from galaxysim.engine.context import TickContext
from galaxysim.engine.resolvers import queries
from galaxysim.model.entities import Fleet, IntentKind


def resolve(ctx: TickContext) -> None:
    hostilities = _declared_hostilities(ctx)
    if not hostilities:
        return

    for location, by_civ in _fleets_by_location(ctx):
        present = sorted(by_civ)
        for attacker in present:
            for defender in present:
                # Ordered pairs, each unordered pair reached once, so a mutual
                # war does not resolve twice in a tick.
                if attacker >= defender:
                    continue
                if (attacker, defender) not in hostilities and (
                    defender,
                    attacker,
                ) not in hostilities:
                    continue
                _exchange(ctx, location, by_civ[attacker], by_civ[defender])

    _remove_destroyed(ctx)


def _declared_hostilities(ctx: TickContext) -> set[tuple[int, int]]:
    """(aggressor_civ_id, target_civ_id) pairs with a standing attack order.

    Attack orders are standing: they persist across ticks until cancelled, so a
    war does not lapse because a player did not log in to renew it.
    """
    pairs: set[tuple[int, int]] = set()
    for intent in queries.active_intents(ctx.session, ctx.universe.id, IntentKind.ATTACK.value):
        target = intent.payload.get("target_civ_id")
        if isinstance(target, int) and target != intent.civ_id:
            pairs.add((intent.civ_id, target))
    return pairs


def _fleets_by_location(
    ctx: TickContext,
) -> list[tuple[tuple[float, float, float], dict[int, list[Fleet]]]]:
    """Group stationary fleets by exact position, in a stable order.

    Fleets in transit are skipped -- you cannot intercept something mid-flight in
    this model, only meet it where it stops.
    """
    grouped: dict[tuple[float, float, float], dict[int, list[Fleet]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for fleet in queries.fleets(ctx.session, ctx.universe.id):
        if fleet.in_transit or fleet.strength <= 0:
            continue
        key = (round(fleet.x, 6), round(fleet.y, 6), round(fleet.z, 6))
        grouped[key][fleet.civ_id].append(fleet)

    return [(location, dict(grouped[location])) for location in sorted(grouped)]


def _exchange(
    ctx: TickContext,
    location: tuple[float, float, float],
    side_a: list[Fleet],
    side_b: list[Fleet],
) -> None:
    """One tick's worth of mutual attrition between two sides."""
    strength_a = sum(f.strength for f in side_a)
    strength_b = sum(f.strength for f in side_b)
    if strength_a <= 0 or strength_b <= 0:
        return

    civ_a, civ_b = side_a[0].civ_id, side_b[0].civ_id
    rng = ctx.rng("combat", location, civ_a, civ_b)
    swing = ctx.rates.combat_variance
    effect_a = rng.uniform(1.0 - swing, 1.0 + swing)
    effect_b = rng.uniform(1.0 - swing, 1.0 + swing)

    intensity = ctx.per_tick(ctx.rates.combat_intensity_per_hour)
    # Both sides' damage is computed from the pre-exchange strengths, so neither
    # side gains an advantage from being evaluated first.
    damage_to_b = strength_a * effect_a * intensity
    damage_to_a = strength_b * effect_b * intensity

    lost_a = _apply_damage(side_a, strength_a, damage_to_a)
    lost_b = _apply_damage(side_b, strength_b, damage_to_b)

    for civ_id, lost, taken in ((civ_a, lost_a, lost_b), (civ_b, lost_b, lost_a)):
        ctx.log(
            "combat",
            f"Engagement at ({location[0]:.2f}, {location[1]:.2f}, {location[2]:.2f}): "
            f"lost {lost:.2f} strength, destroyed {taken:.2f}",
            civ_id=civ_id,
            payload={"lost": round(lost, 4), "destroyed": round(taken, 4)},
        )


def _apply_damage(side: list[Fleet], total_strength: float, damage: float) -> float:
    """Spread ``damage`` across ``side`` in proportion to each fleet's strength.

    Returns the strength actually lost, which is less than ``damage`` when the
    side is wiped out.
    """
    damage = min(damage, total_strength)
    lost = 0.0
    for fleet in side:
        share = fleet.strength / total_strength
        hit = min(damage * share, fleet.strength)
        fleet.strength -= hit
        lost += hit
    return lost


def _remove_destroyed(ctx: TickContext) -> None:
    for fleet in queries.fleets(ctx.session, ctx.universe.id):
        if fleet.strength > ctx.rates.fleet_destruction_threshold:
            continue
        ctx.log(
            "fleet_destroyed",
            f"{fleet.name} was destroyed",
            civ_id=fleet.civ_id,
            payload={"fleet_id": fleet.id},
        )
        ctx.session.delete(fleet)
