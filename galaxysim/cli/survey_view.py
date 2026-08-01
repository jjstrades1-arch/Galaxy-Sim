"""Rendering a planetary survey.

This is a showpiece rather than a stat dump, and it is deliberately generous
with detail that carries no mechanical weight — the star's age, whether a moon
is a captured irregular, the count of volcanic provinces. It is there because
reading it is the point, and because the numbers that *do* drive the game feel
earned when they sit among real ones.

The last block is the payoff of deriving habitability rather than rolling it:
the score is presented as a **conclusion with its reasoning attached**, so a
player can see exactly why a world scored what it did.
"""

from __future__ import annotations

from galaxysim.core.units import format_count  # re-exported for the CLI

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from galaxysim.worldgen.biosphere import describe as describe_biosphere
from galaxysim.worldgen.biosphere import oxygen_note
from galaxysim.worldgen.planet import WATER_FREEZE_K
from galaxysim.worldgen.survey import Survey

#: Gases in a fixed display order -- roughly by how much a reader cares.
GAS_ORDER = ("N2", "O2", "CO2", "H2O", "Ar", "CH4", "SO2", "NH3", "H2", "He", "Ne")

GAS_LABEL = {
    "N2": "N₂",
    "O2": "O₂",
    "CO2": "CO₂",
    "H2O": "H₂O",
    "CH4": "CH₄",
    "SO2": "SO₂",
    "NH3": "NH₃",
    "H2": "H₂",
    "He": "He",
    "Ne": "Ne",
    "Ar": "Ar",
}

ELEMENT_LABEL = {
    "rare_earths": "rare earths",
    "water_ice": "water ice",
    "helium3": "helium-3",
}




def render(console: Console, survey: Survey, name: str, discovered_tick: int | None = None) -> None:
    """Print a full survey."""
    console.print()
    console.print(_header(survey, name, discovered_tick))
    console.print(_star_line(survey))
    console.print()
    console.print(_orbit_and_body(survey))
    console.print(_atmosphere(survey))
    console.print(_climate_and_water(survey))
    console.print(_geology(survey))
    console.print(_biosphere(survey))
    console.print(_power(survey))
    console.print(_satellites(survey))
    console.print(_conclusion(survey))


# --- sections ----------------------------------------------------------------


def _header(survey: Survey, name: str, discovered_tick: int | None) -> Text:
    line = Text(name.upper(), style="bold")
    line.append(f"   {survey.world_class}", style="dim")
    if discovered_tick is not None:
        line.append(f"   surveyed tick {discovered_tick:,}", style="dim")
    return line


def _star_line(survey: Survey) -> Text:
    star = survey.star
    inner, outer = star.habitable_zone
    text = Text()
    text.append(f"{star.designation} {star.colour}", style="yellow")
    text.append(
        f" · {star.luminosity_solar:.4g} L☉ · {star.temperature_k:,.0f} K"
        f" · {star.age_gyr:.1f} Gyr · [Fe/H] {star.metallicity:+.2f}\n",
        style="dim",
    )
    text.append(
        f"habitable zone {inner:.2f}–{outer:.2f} AU · frost line {star.frost_line_au:.2f} AU",
        style="dim",
    )
    return text


def _row(label: str, body: Text | str) -> Table:
    table = Table.grid(padding=(0, 1))
    table.add_column(width=10, style="bold cyan")
    table.add_column()
    table.add_row(label, body)
    return table


def _orbit_and_body(survey: Survey) -> Group:
    orbit, body = survey.orbit, survey.body

    spin = (
        "tidally locked — one face always to the star"
        if orbit.tidally_locked
        else f"rotation {orbit.rotation_hours:.1f} h · tilt {orbit.axial_tilt_deg:.1f}°"
    )
    orbit_text = Text(
        f"{orbit.semi_major_axis_au:.3f} AU · e={orbit.eccentricity:.3f} · "
        f"{orbit.period_days:,.1f} d · {spin}"
    )

    body_text = Text(
        f"{body.radius_earth:.2f} R⊕ · {body.mass_earth:.3g} M⊕ · "
        f"{body.density_gcm3:.2f} g/cm³ · {body.gravity_g:.2f} g · "
        f"escape {body.escape_velocity_kms:.1f} km/s\n"
    )
    if body.is_shielded:
        body_text.append(
            f"magnetic field {body.magnetic_field_gauss:.2f} G — surface shielded", style="green"
        )
    else:
        body_text.append("no magnetic field — surface radiation unshielded", style="red")

    if body.tectonic_activity > 0.05:
        descriptor = "tectonically active" if body.tectonic_activity > 0.3 else "geologically quiet"
        body_text.append(
            f"\n{descriptor} · {body.volcanic_provinces} volcanic provinces", style="dim"
        )
    else:
        body_text.append("\ngeologically dead", style="dim")

    return Group(_row("ORBIT", orbit_text), _row("BODY", body_text))


def _atmosphere(survey: Survey) -> Group:
    air = survey.atmosphere
    if air.pressure_bar <= 0.001:
        return _row("ATMOS", Text("none — vacuum at the surface", style="red"))

    headline = Text(f"{air.pressure_bar:.3g} bar — ")
    if air.is_breathable:
        headline.append(f"BREATHABLE (O₂ {air.partial_pressure('O2'):.2f} bar)", style="bold green")
    else:
        toxins = air.toxins()
        if toxins:
            headline.append(
                f"TOXIC ({', '.join(GAS_LABEL.get(g, g) for g in toxins)})", style="bold red"
            )
        else:
            headline.append("unbreathable", style="yellow")

    parts = []
    for gas in GAS_ORDER:
        fraction = air.composition.get(gas, 0.0)
        if fraction >= 0.0001:
            parts.append(f"{GAS_LABEL.get(gas, gas)} {fraction * 100:.2f}%")
    if parts:
        headline.append("\n" + " · ".join(parts), style="dim")
    headline.append(
        f"\ngreenhouse +{air.greenhouse_k:.0f} K · albedo {air.albedo:.2f}", style="dim"
    )
    return _row("ATMOS", headline)


def _climate_and_water(survey: Survey) -> Group:
    climate, water = survey.climate, survey.hydrosphere

    climate_text = Text(
        f"mean {climate.surface_temp_k:.0f} K ({climate.surface_temp_c:+.0f} °C) · "
        f"seasonal swing ±{climate.seasonal_swing_k:.0f} K\n"
    )
    climate_text.append(
        f"polar {climate.polar_fraction:.0%} · temperate {climate.temperate_fraction:.0%} · "
        f"tropical {climate.tropical_fraction:.0%}",
        style="dim",
    )

    if water.liquid_water:
        water_text = Text("liquid water", style="green")
        water_text.append(
            f" · {water.ocean_fraction:.0%} ocean · {water.ice_fraction:.0%} permanent ice"
            f" · mean depth {water.mean_ocean_depth_km:.1f} km",
            style="dim",
        )
    elif water.ice_fraction > 0.001:
        water_text = Text(f"no liquid water · {water.ice_fraction:.0%} surface ice", style="yellow")
    else:
        water_text = Text("no surface water", style="red")

    return Group(_row("CLIMATE", climate_text), _row("HYDRO", water_text))


def _geology(survey: Survey) -> Group:
    table = Table.grid(padding=(0, 2))
    table.add_column(width=13)
    table.add_column(justify="right", width=9)
    table.add_column(width=6)
    table.add_column(width=9)
    table.add_column(style="dim")

    grade_style = {"RICH": "bold green", "good": "green", "fair": "", "poor": "dim"}

    # Most interesting first: what is worth digging, not what is most abundant.
    ranked = sorted(survey.deposits.values(), key=lambda d: -d.yield_index)
    for deposit in ranked[:9]:
        note = ""
        if deposit.grade == "RICH" and survey.body.tectonic_activity > 0.3:
            note = "← volcanic concentration"
        table.add_row(
            ELEMENT_LABEL.get(deposit.element, deposit.element),
            deposit.describe_abundance(),
            Text(deposit.grade, style=grade_style.get(deposit.grade, "")),
            deposit.depth_label,
            note,
        )

    trace = [d.element for d in ranked[9:] if d.abundance > 0]
    if not trace:
        return _row("GEOLOGY", table)

    # Outside the grid: a long comma list inside a narrow column wraps one word
    # per line and looks like a fault.
    footer = Text(
        "also present: " + ", ".join(ELEMENT_LABEL.get(e, e) for e in sorted(trace)),
        style="dim",
    )
    return Group(_row("GEOLOGY", table), _row("", footer))


def _biosphere(survey: Survey) -> Group:
    bio = survey.biosphere
    if not bio.exists:
        return _row("BIOSPHERE", Text(describe_biosphere(bio), style="dim"))

    text = Text(bio.stage.upper(), style="bold magenta")
    text.append(f" — {bio.biochemistry}, ~{format_count(bio.biomass_tonnes)} t biomass\n")
    text.append(
        "compatible biochemistry" if bio.is_compatible else "incompatible biochemistry",
        style="green" if bio.is_compatible else "yellow",
    )
    text.append(f" · pathogen hazard {bio.hazard_label}\n", style="dim")
    text.append(describe_biosphere(bio), style="italic dim")
    note = oxygen_note(bio)
    if note:
        text.append(f"\n{note}", style="italic dim")
    return _row("BIOSPHERE", text)


def _satellites(survey: Survey) -> Group:
    parts = []
    if survey.moons:
        parts.append(f"{survey.moons} moon{'s' if survey.moons != 1 else ''}")
    if survey.has_rings:
        parts.append("ring system")
    return _row("SATELLITES", Text(", ".join(parts) if parts else "none", style="dim"))


def _power(survey: Survey) -> Group:
    """What a colony here could keep its lights on with.

    Two of the four routes are free and neither works everywhere: sunlight falls
    off as the square of the distance, and ground heat only exists on a world
    that is still alive. Where both fail, a colony runs on shipped fuel -- which
    is worth knowing *before* you settle the place rather than after, since it
    turns an industrial world into a permanent supply liability.
    """
    from galaxysim.colony import energy

    flux = survey.star.flux_at(survey.orbit.semi_major_axis_au)
    tectonics = survey.body.tectonic_activity

    parts: list[str] = []
    if flux >= energy.SOLAR_REFERENCE_FLUX:
        parts.append(f"solar excellent ({flux:.1f}x Earth)")
    elif flux >= 0.15:
        parts.append(f"solar workable ({flux:.2f}x Earth)")
    else:
        parts.append(f"solar negligible ({flux:.3f}x Earth)")

    if tectonics >= 0.5:
        parts.append(f"geothermal strong (activity {tectonics:.2f})")
    elif tectonics >= 0.15:
        parts.append(f"geothermal weak (activity {tectonics:.2f})")
    else:
        parts.append("geothermally dead")

    fuels = [
        name
        for key, name in (("uranium", "uranium"), ("deuterium", "deuterium"), ("helium3", "helium-3"))
        if key in survey.deposits
    ]
    parts.append("fuel on site: " + (", ".join(fuels) if fuels else "none — must be shipped in"))

    return _row("POWER", Text(" · ".join(parts), style="dim"))


def _conclusion(survey: Survey) -> Panel:
    score = survey.habitability
    style = "green" if score >= 0.5 else "yellow" if score >= 0.15 else "red"

    text = Text("HABITABILITY ", style="bold")
    text.append(f"{score:.2f}", style=f"bold {style}")
    text.append("   derived from: ", style="dim")
    text.append(" · ".join(survey.habitability_reasons()), style="dim")
    text.append(
        f"\nUsable land {survey.land_area_km2:,.0f} km²"
        f" — estimated capacity {format_count(survey.carrying_capacity)} people",
    )
    return Panel(text, border_style=style, padding=(0, 1))
