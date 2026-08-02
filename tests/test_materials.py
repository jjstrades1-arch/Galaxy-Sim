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
    CALCIUM,
    CARBON,
    CONSUMABLES,
    ELECTRONICS,
    IRON,
    RARE_EARTHS,
    SILICON,
    STEEL,
    WATER,
    WATER_ICE,
    MATERIALS,
    RAW_MATERIALS,
    RECIPES,
    REFINED_MATERIALS,
    can_afford,
    deposit,
    material,
    recipes_producing,
    refine,
    spend,
    total_mass,
)
from galaxysim.materials.catalogue import STARTING_STOCKPILE, MaterialClass
from galaxysim.materials.costs import (
    ACCELERANT_PER_PROGRESS,
    RESEARCH_ACCELERANTS,
    RESEARCH_COST_PER_PROGRESS,
    accelerant_multiplier,
)
from galaxysim.materials.extraction import (
    STRATEGIC_MATERIALS,
    extract,
    extractable,
    extraction_rates,
    strategic_shortfall,
)
from galaxysim.materials.recipes import GROWN_NOT_REFINED, POWER_FUELS, missing_inputs
from galaxysim.worldgen.geology import EARTH_CRUST, Deposit
from galaxysim.worldgen.serialize import has_surface_water
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


# --- running the chains ------------------------------------------------------


def test_refining_turns_ore_into_things_you_can_actually_spend():
    """The step that makes a world an economy rather than a pile of rock.

    Everything in the game is priced in refined goods -- buildings in steel and
    construction materials, ships in alloys and electronics, life support in
    water. A colony holding nothing but ore can buy none of it.
    """
    stock = {IRON: 500.0, CARBON: 200.0, SILICON: 400.0, CALCIUM: 200.0, WATER_ICE: 300.0}
    spent, produced = refine(stock, work=200.0, hours=1.0)

    assert spent > 0
    assert produced.get(STEEL, 0.0) > 0, "iron and carbon should have become steel"
    assert produced.get(WATER, 0.0) > 0, "ice should have become water"
    assert stock[IRON] < 500.0, "and the ore should be gone from the ground floor"


def test_refining_cannot_run_a_chain_the_world_cannot_supply():
    """Nothing substitutes. A world with no copper makes no electronics."""
    stock = {SILICON: 1000.0, RARE_EARTHS: 1000.0}  # no copper
    _, produced = refine(stock, work=500.0, hours=1.0, priorities={"electronics": 1.0})
    assert ELECTRONICS not in produced


def test_refining_conserves_mass_at_run_time_too():
    """Not just per recipe -- across a whole tick of mixed chains.

    The per-recipe check elsewhere proves the table is honest. This proves the
    runner is: no ordering of chains, no leftover budget, no second pass can
    launder ten tonnes of ore into twenty tonnes of steel.
    """
    stock = {key: 400.0 for key in RAW_MATERIALS}
    before = total_mass(stock)
    refine(stock, work=1000.0, hours=1.0)
    assert total_mass(stock) < before
    assert all(amount >= -1e-9 for amount in stock.values()), "nothing may go negative"


def test_a_refining_plan_beats_leaving_it_automatic():
    """Automation is deliberately mediocre.

    A colony nobody has configured spreads its industry across every chain it
    can run. Naming the one chain this world is good at should beat that -- if
    it did not, there would be no reason to ever look at a colony.
    """
    ore = {IRON: 1000.0, CARBON: 1000.0, SILICON: 1000.0, CALCIUM: 1000.0}

    automatic = dict(ore)
    refine(automatic, work=100.0, hours=1.0)

    directed = dict(ore)
    refine(directed, work=100.0, hours=1.0, priorities={"smelting": 1.0})

    assert directed[STEEL] > automatic[STEEL]


def test_an_unknown_chain_falls_back_rather_than_idling():
    """A colony must never stop working because its plan was nonsense."""
    stock = {IRON: 200.0, CARBON: 200.0}
    spent, _ = refine(stock, work=50.0, hours=1.0, priorities={"perpetual_motion": 1.0})
    assert spent > 0


def test_one_chain_cannot_strip_a_shared_input_in_an_hour():
    """Silicates feed ceramics, construction and electronics alike.

    Without a draw limit the first chain in the plan eats the lot and the player
    has to babysit priorities just to stop the economy consuming itself.
    """
    stock = {SILICON: 100.0, CALCIUM: 100.0}
    refine(stock, work=10_000.0, hours=1.0, priorities={"ceramics": 1.0})
    assert stock[SILICON] > 50.0, "an hour cannot take more than a fraction of the seam"


# --- what research costs -----------------------------------------------------


def test_research_is_priced_in_common_goods_only():
    """Geology must never lock a civilization out of a branch of tech.

    A metal-poor start should research *slower*, not not at all. So the bill is
    payable in things every civ can make by more than one route, and the rare
    materials appear only as accelerants -- which are optional by construction.
    """
    for key in RESEARCH_COST_PER_PROGRESS:
        assert key in REFINED_MATERIALS, f"{key} is not something industry makes"
        assert key not in STRATEGIC_MATERIALS, f"{key} would let geology gate research"

    for key in RESEARCH_ACCELERANTS:
        assert key in MATERIALS
        assert RESEARCH_ACCELERANTS[key] > 1.0, "an accelerant that slows you down"


def test_accelerants_help_in_proportion_and_never_stack():
    """Three multiplicative bonuses would outrun the cost curve they sit under."""
    progress = 10.0
    wanted = progress * ACCELERANT_PER_PROGRESS

    none, consumed = accelerant_multiplier({}, progress)
    assert none == 1.0 and consumed == {}

    # A trickle gives a partial speed-up, so shipping some is worth doing.
    trickle, _ = accelerant_multiplier({"rare_earths": wanted * 0.1}, progress)
    full, manifest = accelerant_multiplier({"rare_earths": wanted * 10}, progress)
    assert 1.0 < trickle < full
    assert manifest["rare_earths"] == pytest.approx(wanted), "no more than it can absorb"

    # Everything at once still buys only the best one.
    everything = {key: wanted * 10 for key in RESEARCH_ACCELERANTS}
    best, manifest = accelerant_multiplier(everything, progress)
    assert len(manifest) == 1
    assert best == pytest.approx(max(RESEARCH_ACCELERANTS.values()))


def test_a_wet_world_supplies_its_own_water_and_a_dry_one_does_not():
    """The line between a place and a permanent supply liability.

    It is drawn by the phase diagram, not by a flag somebody set: liquid water
    on the surface means life support is labour and nothing more. Ice in the
    ground is not the same thing -- getting water out of it is a chain somebody
    has to run.
    """
    assert has_surface_water({"hydrosphere": {"liquid_water": True}})
    assert not has_surface_water({"hydrosphere": {"liquid_water": False}})
    assert not has_surface_water({}), "an unsurveyed rock is dry until proven otherwise"


def test_a_plan_runs_its_priorities_first_not_its_alphabet():
    """Order in a refining plan *is* the priority, and it used to be the alphabet.

    :func:`refine` walks the plan spending a shared per-material draw allowance,
    so whoever comes first gets the scarce input. Sorted by name, ``polymers``
    -- eight carbon a run -- emptied the carbon before ``smelting`` was reached,
    and a capital holding eight billion tonnes of iron made no steel at all
    while its own plan named smelting the top priority. Eight AI civilizations
    stopped expanding for two simulated months on that one ``sorted()``.
    """
    from galaxysim.materials.refining import plan_for, refine

    priorities = {"polymers": 0.96, "smelting": 1.0, "fuel_synthesis": 0.74}
    ordered = [recipe.key for recipe, _ in plan_for(priorities)]
    assert ordered[0] == "smelting", f"highest weight should run first, got {ordered}"

    # And it shows up in what actually comes out: plenty of iron, barely any
    # carbon, and all three chains competing for that carbon.
    stock = {
        "iron": 1e9, "carbon": 300_000.0, "water_ice": 1e8,
        "copper": 1e6, "rare_earths": 1e6, "silicon": 1e6,
    }
    _, made = refine(dict(stock), work=1e7, hours=1.0, priorities=priorities)
    assert made.get("steel", 0.0) > 0.0, (
        "the top-priority chain must get a share of the scarce input"
    )
