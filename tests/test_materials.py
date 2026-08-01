"""The materials economy.

The load-bearing property here is that **a world produces what it is made of**.
The old three-resource system meant every world was worth some amount of
"metal"; now a world either has uranium in its crust or it does not, and no
amount of labour, infrastructure or technology conjures it.

That single fact is what turns geology into logistics and logistics into
conflict, so most of this file is about verifying that absence is real and
that scarcity survives at population scale.
"""

from __future__ import annotations

import pytest

from galaxysim.core.seeds import rng_for
from galaxysim.materials import (
    CONSUMABLES,
    MATERIALS,
    RAW_MATERIALS,
    RECIPES,
    REFINED_MATERIALS,
    can_afford,
    deposit,
    material,
    recipes_producing,
    spend,
    total_mass,
)
from galaxysim.materials.catalogue import STARTING_STOCKPILE, MaterialClass
from galaxysim.materials.extraction import (
    STRATEGIC_MATERIALS,
    extract,
    extractable,
    extraction_rates,
    strategic_shortfall,
)
from galaxysim.materials.recipes import GROWN_NOT_REFINED, POWER_FUELS, missing_inputs
from galaxysim.worldgen.geology import EARTH_CRUST, Deposit
from galaxysim.worldgen.star import Star, roll_star
from galaxysim.worldgen.survey import plausible_mass, plausible_orbits, survey_world


def sample_worlds(systems: int = 500):
    worlds = []
    for index in range(systems):
        rng = rng_for("materials", index)
        star = roll_star(rng)
        for orbit, distance in enumerate(plausible_orbits(rng, star, rng.randint(1, 6))):
            world_rng = rng_for("materials-world", index, orbit)
            worlds.append(
                survey_world(
                    world_rng, star, distance, plausible_mass(world_rng, distance, star)
                )
            )
    return worlds


# --- the catalogue -----------------------------------------------------------

def test_raw_material_keys_match_the_geology_generator():
    """The integration contract of the whole phase.

    Extraction is a direct lookup from a world's deposits, with no mapping layer
    in between. That only works if the names agree, so a rename on either side
    has to fail here rather than silently producing a world that mines nothing.
    """
    geological = set(EARTH_CRUST) | {"water_ice", "helium3", "deuterium"}
    for key in RAW_MATERIALS:
        assert key in geological, f"raw material {key!r} is not something geology produces"


def test_catalogue_is_coherent():
    assert len(MATERIALS) == len(RAW_MATERIALS) + len(REFINED_MATERIALS) + len(CONSUMABLES)
    for key, spec in MATERIALS.items():
        assert spec.key == key
        assert spec.name and spec.description
        assert spec.mass_per_unit > 0
    with pytest.raises(KeyError):
        material("unobtanium")


def test_a_civilization_starts_with_real_industrial_stock():
    """A homeworld of billions is not a landing party with a crate of ore."""
    assert all(key in MATERIALS for key in STARTING_STOCKPILE)
    refined = sum(v for k, v in STARTING_STOCKPILE.items() if k in REFINED_MATERIALS)
    assert refined > 0, "a developed civilization holds refined goods, not just ore"


# --- refining ----------------------------------------------------------------

def test_refining_never_creates_mass():
    """You cannot launder ten tonnes of ore into twenty tonnes of steel.

    Without this the total material in a civilization is unbounded and every
    scarcity argument in the design collapses.
    """
    for recipe in RECIPES.values():
        assert recipe.yield_fraction < 1.0, f"{recipe.key} gains mass"
        assert recipe.yield_fraction > 0.05, f"{recipe.key} wastes almost everything"
        assert recipe.work > 0
        assert recipe.inputs and recipe.outputs


def test_every_refined_material_can_actually_be_made():
    for key in REFINED_MATERIALS:
        assert recipes_producing(key), f"nothing produces {key}"


def test_nothing_is_orphaned():
    """Every raw material is consumed by something, or explicitly exempt.

    The exemptions are deliberate and named -- fusion fuels are burned for power
    rather than refined, and food is grown rather than made. Listing them means
    this test distinguishes 'by design' from 'somebody forgot'.
    """
    consumed = {key for recipe in RECIPES.values() for key in recipe.inputs}
    orphans = set(RAW_MATERIALS) - consumed - POWER_FUELS
    assert not orphans, f"raw materials nothing uses: {sorted(orphans)}"

    unmade = set(CONSUMABLES) - {k for r in RECIPES.values() for k in r.outputs}
    assert unmade == GROWN_NOT_REFINED, f"unexpected unmakeable consumables: {unmade}"


def test_electronics_is_the_chain_that_forces_trade():
    """One route, and it needs three inputs that rarely co-occur.

    Deliberate: alloys and fissiles each have a fallback route so geology cannot
    lock a civ out entirely, but electronics is meant to be the thing you have
    to trade for.
    """
    assert len(recipes_producing("electronics")) == 1
    assert len(recipes_producing("alloys")) == 2, "alloys need a fallback route"
    assert len(recipes_producing("fissiles")) == 2, "fissiles need a fallback route"


def test_missing_inputs_reports_what_is_actually_short():
    recipe = RECIPES["electronics"]
    assert missing_inputs(recipe, {}) == tuple(sorted(recipe.inputs))
    plenty = {key: amount * 10 for key, amount in recipe.inputs.items()}
    assert missing_inputs(recipe, plenty) == ()
    short = dict(plenty, copper=0.0)
    assert missing_inputs(recipe, short) == ("copper",)


# --- extraction --------------------------------------------------------------

def test_a_world_without_an_element_yields_none_of_it():
    """Absence is real. This is the whole point of the change."""
    deposits = {
        "iron": Deposit("iron", 0.05, 6.0, 0.2),
        "silicon": Deposit("silicon", 0.27, 3.0, 0.1),
    }
    produced = extract(deposits, worker_hours=10_000)
    assert "uranium" not in produced
    assert "copper" not in produced
    assert produced["iron"] > 0

    # And no labour, however vast, changes that.
    assert "uranium" not in extract(deposits, worker_hours=1e9)


def test_richer_and_shallower_deposits_out_yield_poorer_deeper_ones():
    rich = {"iron": Deposit("iron", 0.06, 10.0, 0.05)}
    poor = {"iron": Deposit("iron", 0.06, 1.0, 0.9)}
    assert extraction_rates(rich)["iron"] > extraction_rates(poor)["iron"] * 3


def test_trace_deposits_are_not_worth_mining():
    """A colony's output list should be what it produces, not everything it
    contains at four grams an hour."""
    trace = {"uranium": Deposit("uranium", 1e-12, 0.9, 0.95)}
    assert extractable(trace) == {}


def test_extraction_scales_with_labour_but_workers_are_not_split():
    """A mining workforce works every seam the colony has opened.

    Splitting workers between deposits would make a world with many poor
    deposits worse than one with a single poor deposit, which is backwards.
    """
    deposits = {
        "iron": Deposit("iron", 0.05, 6.0, 0.2),
        "copper": Deposit("copper", 0.0001, 6.0, 0.2),
    }
    one = extract(deposits, 100.0)
    two = extract(deposits, 200.0)
    assert two["iron"] == pytest.approx(one["iron"] * 2)
    assert two["copper"] == pytest.approx(one["copper"] * 2)

    # Adding a second deposit does not reduce the first.
    alone = extract({"iron": deposits["iron"]}, 100.0)
    assert one["iron"] == pytest.approx(alone["iron"])


def test_no_labour_produces_nothing():
    deposits = {"iron": Deposit("iron", 0.05, 6.0, 0.2)}
    assert extract(deposits, 0.0) == {}
    assert extract(deposits, -5.0) == {}


# --- scarcity at population scale --------------------------------------------

def test_strategic_materials_are_genuinely_scarce():
    """Across a realistic population of worlds, the things worth fighting over
    must actually be rare.

    This is the test that backs the scarcity design. If uranium turned up
    everywhere, "the only uranium source in forty light-years" would be a
    fiction and there would be nothing specific to fight about.
    """
    worlds = sample_worlds()
    assert len(worlds) > 1200

    presence = {key: 0 for key in STRATEGIC_MATERIALS}
    for survey in worlds:
        available = extractable(survey.deposits)
        for key in STRATEGIC_MATERIALS:
            if key in available:
                presence[key] += 1

    fraction = {key: count / len(worlds) for key, count in presence.items()}

    # The genuinely rare ones.
    assert fraction["uranium"] < 0.25, f"uranium on {fraction['uranium']:.1%} of worlds"
    assert fraction["water_ice"] < 0.35, f"water on {fraction['water_ice']:.1%} of worlds"

    # And the ones that should be broadly available, or the economy seizes up.
    assert fraction["carbon"] > 0.5
    assert fraction["copper"] > 0.5


def test_most_worlds_are_missing_something_they_need():
    """Specialisation: a world should rarely be self-sufficient in everything.

    If a typical world could supply its own whole strategic list, there would be
    no reason to ship anything anywhere and the logistics layer would be
    decoration.
    """
    shortfalls = [
        len(strategic_shortfall(survey.deposits, STRATEGIC_MATERIALS))
        for survey in sample_worlds(200)
    ]
    self_sufficient = sum(1 for count in shortfalls if count == 0)
    assert self_sufficient / len(shortfalls) < 0.25, (
        "too many worlds need nothing shipped in"
    )


def test_geology_drives_output_end_to_end():
    """A generated world's extraction follows from its actual crust."""
    star = Star("G", 2, 1.0, 1.0, 1.0, 5772, 5.0, 0.0)
    survey = survey_world(rng_for("end-to-end", 1), star, 1.0, 1.0)
    rates = extraction_rates(survey.deposits)

    for key, rate in rates.items():
        assert key in survey.deposits, f"{key} produced from nothing"
        assert rate > 0
    for key in survey.deposits:
        if key not in rates and key in MATERIALS:
            # Present but not worth mining -- must be a genuinely poor deposit.
            assert survey.deposits[key].yield_index < 1.0


# --- stockpile arithmetic ----------------------------------------------------

def test_stockpile_arithmetic():
    stock = {"iron": 100.0}
    assert can_afford(stock, {"iron": 50.0})
    assert not can_afford(stock, {"iron": 150.0})
    assert not can_afford(stock, {"copper": 1.0})

    spend(stock, {"iron": 40.0})
    assert stock["iron"] == pytest.approx(60.0)
    with pytest.raises(ValueError):
        spend(stock, {"iron": 1000.0})
    assert stock["iron"] == pytest.approx(60.0), "a failed spend must not part-deduct"

    deposit(stock, {"iron": 10.0, "copper": 5.0})
    assert stock["iron"] == pytest.approx(70.0)
    assert stock["copper"] == pytest.approx(5.0)


def test_cargo_mass_respects_what_is_being_carried():
    """A hold of electronics and a hold of ore are different amounts of
    usefulness, and freighter capacity should notice."""
    assert total_mass({"iron": 100.0}) == pytest.approx(100.0)
    assert total_mass({"electronics": 100.0}) < total_mass({"iron": 100.0})
    assert total_mass({}) == 0.0
    assert total_mass({"not_a_material": 500.0}) == 0.0


def test_material_classes_partition_cleanly():
    for key in RAW_MATERIALS:
        assert MATERIALS[key].material_class is MaterialClass.RAW
        assert MATERIALS[key].is_raw
    for key in REFINED_MATERIALS:
        assert MATERIALS[key].material_class is MaterialClass.REFINED
        assert not MATERIALS[key].is_raw
