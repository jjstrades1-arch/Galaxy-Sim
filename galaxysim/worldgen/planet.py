"""Planets, derived rather than rolled.

The generation order here *is* the causality, and following it is what stops
worlds from contradicting themselves:

    orbit -> body -> outgassing -> [temperature <-> atmosphere] -> hydrosphere
          -> biosphere -> free oxygen -> final temperature -> habitability

Only a handful of things are actually rolled: where the planet orbits, how much
mass it has, its rotation and tilt, and how much gas it outgassed. Everything
else is computed. That is why you will never see an airless world with oceans,
or liquid water at 200 K, or a small hot rock holding onto hydrogen -- not
because a rule forbids it, but because the physics never produces it.

The two loops worth understanding:

**Atmospheric retention.** A world keeps a gas if its escape velocity
comfortably exceeds that gas's thermal velocity at that temperature. Light gases
move fastest, so hydrogen and helium escape first, which is exactly why Earth
has nitrogen and oxygen but no hydrogen, and why Titan keeps a thick nitrogen
atmosphere despite being tiny -- it is cold.

**Temperature and greenhouse are mutually dependent.** Surface temperature
depends on the atmosphere, and which atmosphere the world can hold depends on
temperature. We solve it by iterating a few times from the bare equilibrium
temperature, which is both stable and how the real feedback works.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from galaxysim.worldgen.star import Star

# --- physical constants ------------------------------------------------------

#: Earth's escape velocity, km/s. Scales as sqrt(M/R) in Earth units.
EARTH_ESCAPE_KMS = 11.186
#: Universal gas constant, J/(mol·K).
GAS_CONSTANT = 8.314
#: Equilibrium temperature of a perfectly absorbing body at 1 AU from the Sun.
SOLAR_CONSTANT_TEMP_K = 278.6
#: Freezing and boiling points of water at 1 bar.
WATER_FREEZE_K = 273.15
WATER_BOIL_K = 373.15
#: Below this pressure liquid water cannot exist at any temperature -- the
#: triple point. This is why Mars has ice and vapour but no lakes.
WATER_TRIPLE_POINT_BAR = 0.006

#: Tectonic activity below which a world's core has frozen solid and it has no
#: magnetic dynamo. Mars is the cautionary case: it lost its field, and then
#: the stellar wind took its atmosphere.
DYNAMO_THRESHOLD = 0.12

#: Calibrates the tidal-locking timescale, in Gyr for a world at 1 AU around a
#: one-solar-mass star. Set so an Earth analogue never locks (its real locking
#: time is far longer than the age of the universe) while a red dwarf's
#: habitable zone, which sits ten times closer in, locks within a billion years.
TIDAL_LOCK_CONSTANT_GYR = 2.4e5

#: Ratio of escape velocity to a gas's RMS thermal velocity above which the
#: world holds that gas over geological time. Six is the standard rule of thumb,
#: and it correctly predicts that Earth keeps N2 but slowly loses H2.
RETENTION_RATIO = 6.0

#: Molar mass in kg/mol for each atmospheric gas we model.
MOLAR_MASS: dict[str, float] = {
    "H2": 0.002,
    "He": 0.004,
    "CH4": 0.016,
    "NH3": 0.017,
    "H2O": 0.018,
    "Ne": 0.020,
    "N2": 0.028,
    "O2": 0.032,
    "Ar": 0.040,
    "CO2": 0.044,
    "SO2": 0.064,
}

#: Greenhouse potency relative to CO2, per unit partial pressure.
GREENHOUSE_POTENCY: dict[str, float] = {
    "CO2": 1.0,
    "CH4": 25.0,
    "H2O": 0.6,
    "SO2": 0.3,
    "NH3": 0.5,
}

#: Greenhouse forcing is anchored to Earth: 33 K at Earth's greenhouse index.
#: The exponent is fitted so Mars (thin CO2) lands near 5 K, and the cap keeps a
#: Venus-like runaway near its real ~500 K rather than diverging.
EARTH_GREENHOUSE_INDEX = 0.0064
EARTH_GREENHOUSE_K = 33.0
GREENHOUSE_EXPONENT = 0.36
GREENHOUSE_CAP_K = 500.0

#: Gases that are toxic to breathe above these partial pressures, in bar.
TOXIC_LIMITS: dict[str, float] = {"CO2": 0.01, "CH4": 0.05, "SO2": 0.000005, "NH3": 0.00003}

#: Breathability window for oxygen partial pressure, in bar.
O2_MIN_BAR = 0.16
O2_MAX_BAR = 0.50
#: Total pressure a person can survive unaided, in bar.
PRESSURE_MIN_BAR = 0.5
PRESSURE_MAX_BAR = 4.0


@dataclass(frozen=True, slots=True)
class Orbit:
    """Where a planet sits and how it turns."""

    semi_major_axis_au: float
    eccentricity: float
    inclination_deg: float
    period_days: float
    rotation_hours: float
    axial_tilt_deg: float
    tidally_locked: bool

    @property
    def perihelion_au(self) -> float:
        return self.semi_major_axis_au * (1.0 - self.eccentricity)

    @property
    def aphelion_au(self) -> float:
        return self.semi_major_axis_au * (1.0 + self.eccentricity)


@dataclass(frozen=True, slots=True)
class Body:
    """The physical planet."""

    mass_earth: float
    radius_earth: float
    density_gcm3: float
    gravity_g: float
    escape_velocity_kms: float
    #: 0-1. Drives ore concentration, volcanism and internal heat.
    tectonic_activity: float
    volcanic_provinces: int
    #: Surface field in gauss. Earth is ~0.5. Shields the surface from stellar
    #: wind and, over time, stops the atmosphere being stripped away.
    magnetic_field_gauss: float

    @property
    def is_shielded(self) -> bool:
        return self.magnetic_field_gauss >= 0.1


@dataclass(frozen=True, slots=True)
class Atmosphere:
    """What the world holds, and what that does."""

    pressure_bar: float
    #: Gas -> mole fraction, summing to 1. Empty for an airless world.
    composition: dict[str, float] = field(default_factory=dict)
    albedo: float = 0.3
    greenhouse_k: float = 0.0

    def partial_pressure(self, gas: str) -> float:
        return self.pressure_bar * self.composition.get(gas, 0.0)

    @property
    def is_breathable(self) -> bool:
        """Could an unprotected human-like species breathe this?

        Needs oxygen in the right window, survivable total pressure, and no
        toxin above its limit. All three, which is why breathable worlds are
        rare and finding one matters.
        """
        if not (PRESSURE_MIN_BAR <= self.pressure_bar <= PRESSURE_MAX_BAR):
            return False
        if not (O2_MIN_BAR <= self.partial_pressure("O2") <= O2_MAX_BAR):
            return False
        return all(
            self.partial_pressure(gas) <= limit for gas, limit in TOXIC_LIMITS.items()
        )

    def toxins(self) -> list[str]:
        """Which gases exceed their safe partial pressure."""
        return sorted(
            gas for gas, limit in TOXIC_LIMITS.items() if self.partial_pressure(gas) > limit
        )


@dataclass(frozen=True, slots=True)
class Climate:
    """Thermal state of the surface."""

    equilibrium_temp_k: float
    surface_temp_k: float
    #: Peak-to-mean seasonal swing, from axial tilt and orbital eccentricity.
    seasonal_swing_k: float
    #: Fractions of the surface, summing to 1.
    polar_fraction: float
    temperate_fraction: float
    tropical_fraction: float

    @property
    def surface_temp_c(self) -> float:
        return self.surface_temp_k - WATER_FREEZE_K


@dataclass(frozen=True, slots=True)
class Hydrosphere:
    """Surface water, if any."""

    liquid_water: bool
    ocean_fraction: float
    ice_fraction: float
    mean_ocean_depth_km: float

    @property
    def land_fraction(self) -> float:
        return max(0.0, 1.0 - self.ocean_fraction)


# --- generation --------------------------------------------------------------


def rms_velocity_kms(gas: str, temperature_k: float) -> float:
    """Root-mean-square thermal velocity of a gas molecule, km/s."""
    molar = MOLAR_MASS[gas]
    return math.sqrt(3.0 * GAS_CONSTANT * temperature_k / molar) / 1000.0


def retains(gas: str, escape_velocity_kms: float, temperature_k: float) -> bool:
    """Whether a world holds this gas over geological time.

    The whole reason small hot worlds are airless and cold ones are not.
    """
    return escape_velocity_kms >= RETENTION_RATIO * rms_velocity_kms(gas, temperature_k)


def equilibrium_temperature(star: Star, distance_au: float, albedo: float) -> float:
    """Blackbody temperature from stellar flux alone, before any greenhouse."""
    flux = star.flux_at(distance_au)
    return SOLAR_CONSTANT_TEMP_K * (flux ** 0.25) * ((1.0 - albedo) ** 0.25)


def greenhouse_forcing(atmosphere_pressure: float, composition: dict[str, float]) -> float:
    """Surface warming from greenhouse gases, in kelvin.

    Scales with both the amount of greenhouse gas *and* total pressure, because
    pressure broadening genuinely widens absorption lines -- which is why Mars
    has more CO2 than Earth by partial pressure and far less greenhouse effect.
    """
    if atmosphere_pressure <= 0:
        return 0.0

    index = 0.0
    for gas, potency in GREENHOUSE_POTENCY.items():
        index += potency * atmosphere_pressure * composition.get(gas, 0.0)
    index *= atmosphere_pressure

    if index <= 0:
        return 0.0
    forcing = EARTH_GREENHOUSE_K * (index / EARTH_GREENHOUSE_INDEX) ** GREENHOUSE_EXPONENT
    return min(GREENHOUSE_CAP_K, forcing)


def roll_orbit(rng: random.Random, star: Star, distance_au: float, age_gyr: float) -> Orbit:
    """Orbit and rotation at a given distance."""
    period_years = math.sqrt(distance_au**3 / star.mass_solar)
    eccentricity = min(0.6, abs(rng.gauss(0.0, 0.07)))
    inclination = abs(rng.gauss(0.0, 2.5))

    # Tidal locking. The braking timescale goes as a^6 / M^2, so it explodes
    # with distance: a world twice as far out takes sixty-four times as long to
    # lock. The constant is calibrated against the solar system -- Earth's
    # locking time comes out astronomically longer than the age of the universe,
    # and Mercury's is long enough that it never fully locked either, which is
    # correct.
    #
    # The same constant makes red dwarfs lock their habitable-zone planets,
    # because a dim star's habitable zone is so close in. That is a real and
    # much-discussed objection to red dwarf habitability, and it falls out of
    # the arithmetic rather than being asserted.
    lock_timescale_gyr = (
        TIDAL_LOCK_CONSTANT_GYR * distance_au**6 / max(star.mass_solar, 0.05) ** 2
    )
    tidally_locked = lock_timescale_gyr < age_gyr

    if tidally_locked:
        rotation_hours = period_years * 365.25 * 24.0
        axial_tilt = 0.0
    else:
        rotation_hours = max(2.5, rng.gauss(24.0, 14.0))
        # Tilt is broadly distributed; large tilts come from giant impacts.
        axial_tilt = abs(rng.gauss(23.0, 16.0)) if rng.random() > 0.08 else rng.uniform(60, 120)

    return Orbit(
        semi_major_axis_au=round(distance_au, 4),
        eccentricity=round(eccentricity, 4),
        inclination_deg=round(inclination, 2),
        period_days=round(period_years * 365.25, 2),
        rotation_hours=round(rotation_hours, 2),
        axial_tilt_deg=round(min(179.0, axial_tilt), 2),
        tidally_locked=tidally_locked,
    )


def roll_body(rng: random.Random, rng_mass: float, beyond_frost_line: bool, age_gyr: float) -> Body:
    """Mass, and everything that follows from it.

    ``rng_mass`` is the pre-rolled mass in Earth masses so callers can control
    the size class (moon, terrestrial, superearth, giant) while the derived
    physics stays here.
    """
    mass = max(0.001, rng_mass)

    # Ice-rich worlds beyond the frost line are less dense; rocky worlds inside
    # it are denser. Larger bodies self-compress.
    base_density = 3.0 if beyond_frost_line else 5.2
    density = base_density * (mass**0.06) * rng.uniform(0.88, 1.12)

    # radius from mass and density, in Earth units (Earth density 5.51 g/cm3)
    radius = (mass * 5.513 / density) ** (1.0 / 3.0)

    gravity = mass / (radius * radius)
    escape = EARTH_ESCAPE_KMS * math.sqrt(mass / radius)

    # Internal heat drives tectonics, and it comes from radioactive decay and
    # accretion -- so bigger bodies stay hot longer and old worlds cool down.
    heat = (mass**0.65) * math.exp(-age_gyr / 6.0)
    # Rounded before it is used, not after, so the stored value is the one every
    # downstream decision was actually made from. Deciding on the full-precision
    # number and storing a rounded one leaves worlds that contradict their own
    # data at the boundary.
    tectonic = round(max(0.0, min(1.0, heat * rng.uniform(0.7, 1.3))), 4)
    provinces = int(tectonic * 40 * (radius**2) * rng.uniform(0.5, 1.5))

    # A dynamo needs a molten core and rotation. Small or cold worlds have none,
    # which is why Mars lost its field and then its atmosphere.
    field = tectonic * math.sqrt(mass) * rng.uniform(0.3, 1.6) if tectonic > DYNAMO_THRESHOLD else 0.0

    return Body(
        mass_earth=round(mass, 4),
        radius_earth=round(radius, 4),
        density_gcm3=round(density, 3),
        gravity_g=round(gravity, 4),
        escape_velocity_kms=round(escape, 3),
        tectonic_activity=tectonic,
        volcanic_provinces=provinces,
        magnetic_field_gauss=round(field, 3),
    )


def carbonate_silicate_drawdown(temperature_k: float, water_endowment: float) -> float:
    """Fraction of atmospheric CO2 that weathering locks away into rock.

    The planetary thermostat, and the single reason Earth is not Venus. Rain
    dissolves CO2, the resulting acid weathers silicate rock, and the carbon
    ends up in carbonate sediment. Crucially the reaction runs *faster when it
    is hotter*, so it is a negative feedback: a warming world scrubs its own
    greenhouse gas, and a cooling one stops scrubbing and warms back up.

    It needs liquid water. A world that loses its oceans loses the thermostat,
    its CO2 accumulates unchecked, and it runs away into a Venus. That is
    believed to be roughly what happened to Venus.
    """
    if water_endowment <= 0.05 or temperature_k < WATER_FREEZE_K:
        return 0.0
    if temperature_k > WATER_BOIL_K + 40:
        # Oceans boiled off; no rain, no weathering, no brake. This is the
        # runaway, and it is why Venus kept ninety-two bar of CO2.
        return 0.0

    # Exponential in temperature, because the drawdown Earth actually achieves
    # is extreme: roughly sixty bar of CO2 sit locked in carbonate rock against
    # four ten-thousandths of a bar left in the air. A linear brake cannot
    # express that, and a world with a linear brake cooks.
    warmth = temperature_k - WATER_FREEZE_K
    strength = 1.0 - math.exp(-warmth / 4.0)
    return max(0.0, min(0.99999, strength * min(1.0, water_endowment * 1.5)))


def build_atmosphere(
    rng: random.Random,
    star: Star,
    body: Body,
    orbit: Orbit,
    beyond_frost_line: bool,
    oxygenated: bool = False,
    water_endowment: float = 0.0,
) -> tuple[Atmosphere, Climate]:
    """Solve atmosphere and temperature together.

    They depend on each other -- which gases survive depends on temperature,
    temperature depends on the greenhouse those gases create, and how much
    greenhouse gas survives depends on whether it is warm and wet enough to
    weather it away -- so we iterate from the bare equilibrium temperature until
    it settles. Three passes is plenty and cannot oscillate.
    """
    # How much gas the world outgassed, before deciding what it keeps. Driven by
    # mass (more material, more outgassing) and tectonic activity (which is what
    # brings volatiles to the surface at all).
    outgassing = (
        body.mass_earth**1.3
        * (0.25 + body.tectonic_activity)
        * (2.2 if beyond_frost_line else 1.0)
        * rng.uniform(0.25, 2.4)
    )
    # A world with no magnetic field has its atmosphere stripped by stellar
    # wind over billions of years -- the Mars story.
    if not body.is_shielded:
        outgassing *= 0.12

    # Abiotic starting mix: mostly CO2 and N2 from volcanism, with more ices
    # further out. No free oxygen -- that requires life.
    available: dict[str, float] = {
        "CO2": rng.uniform(0.30, 0.80),
        "N2": rng.uniform(0.05, 0.45),
        "H2O": rng.uniform(0.02, 0.20),
        "Ar": rng.uniform(0.005, 0.04),
        "SO2": rng.uniform(0.0, 0.06) * (0.3 + body.tectonic_activity),
    }
    if beyond_frost_line:
        available["CH4"] = rng.uniform(0.02, 0.25)
        available["NH3"] = rng.uniform(0.0, 0.10)
        if body.mass_earth > 8.0:
            # Only a giant holds primordial hydrogen and helium.
            available["H2"] = rng.uniform(0.4, 0.85)
            available["He"] = rng.uniform(0.05, 0.20)

    temperature = equilibrium_temperature(star, orbit.semi_major_axis_au, 0.3)
    atmosphere = Atmosphere(pressure_bar=0.0)

    # Six damped passes. The thermostat is a strong negative feedback, so an
    # undamped solve oscillates between frozen and boiling instead of settling;
    # averaging each step against the last converges smoothly.
    for _ in range(6):
        retained = {
            gas: amount
            for gas, amount in available.items()
            if amount > 0 and retains(gas, body.escape_velocity_kms, temperature)
        }

        # Water freezes out of the air onto the surface when it is cold enough,
        # so a frozen world has a dry atmosphere however much ice it holds.
        if temperature < WATER_FREEZE_K - 15 and "H2O" in retained:
            retained["H2O"] *= 0.05

        # The thermostat, applied to the gas itself rather than to proportions:
        # carbon locked into carbonate rock leaves the atmosphere entirely, so
        # the total pressure falls with it. This is why Earth carries 1 bar and
        # Venus -- which lost its oceans and therefore its thermostat -- carries
        # ninety-two.
        drawdown = carbonate_silicate_drawdown(temperature, water_endowment)
        if drawdown > 0:
            if "CO2" in retained:
                retained["CO2"] *= 1.0 - drawdown
            # Sulphur dioxide is scrubbed even harder than CO2 -- it oxidises
            # and rains out as acid within years, which is why Earth's air
            # carries essentially none of it despite constant volcanism. A wet
            # world is not a sulphurous one.
            if "SO2" in retained:
                retained["SO2"] *= (1.0 - drawdown) ** 2
            # Ammonia is destroyed by ultraviolet light unless it is cold and
            # shielded enough to survive.
            if "NH3" in retained and temperature > 200:
                retained["NH3"] *= 0.001

        total = sum(retained.values())
        if total <= 1e-9:
            atmosphere = Atmosphere(pressure_bar=0.0, composition={}, albedo=0.12)
            temperature = equilibrium_temperature(star, orbit.semi_major_axis_au, 0.12)
            continue

        composition = {gas: amount / total for gas, amount in retained.items()}

        if oxygenated:
            # Free oxygen is a biosignature: it is so reactive that without life
            # continually replenishing it, it disappears within geological
            # moments. Earth's twenty-one percent is the accumulated output of
            # two billion years of photosynthesis burying carbon.
            #
            # So the oxygen budget is *not* limited by whatever CO2 happens to
            # be in the air right now -- that CO2 is recycled through the
            # biosphere continuously and topped up by volcanism. It is set by
            # how long the process has been running.
            oxygen = rng.uniform(0.12, 0.30)
            others = {g: a for g, a in composition.items() if g != "O2"}
            # Biology draws carbon down far below what weathering alone manages,
            # and an oxidising atmosphere destroys sulphur compounds outright.
            others["CO2"] = others.get("CO2", 0.0) * 0.001
            others["SO2"] = others.get("SO2", 0.0) * 0.0001
            remainder = sum(others.values())
            if remainder > 0:
                scale = (1.0 - oxygen) / remainder
                composition = {g: a * scale for g, a in others.items()}
            else:
                composition = {"N2": 1.0 - oxygen}
            composition["O2"] = oxygen

        pressure = outgassing * total * rng.uniform(0.6, 1.5)
        pressure = max(0.0, min(300.0, pressure))

        # Reflectivity: clouds and ice raise it, bare dark rock lowers it.
        albedo = 0.12 + 0.30 * min(1.0, pressure / 2.0)
        if temperature < WATER_FREEZE_K:
            albedo += 0.18  # ice
        albedo = max(0.05, min(0.85, albedo))

        greenhouse = greenhouse_forcing(pressure, composition)
        equilibrium = equilibrium_temperature(star, orbit.semi_major_axis_au, albedo)

        atmosphere = Atmosphere(
            pressure_bar=round(pressure, 5),
            composition={g: round(a, 5) for g, a in sorted(composition.items()) if a > 1e-5},
            albedo=round(albedo, 4),
            greenhouse_k=round(greenhouse, 2),
        )
        temperature = 0.5 * temperature + 0.5 * (equilibrium + greenhouse)

    equilibrium = equilibrium_temperature(
        star, orbit.semi_major_axis_au, atmosphere.albedo
    )
    surface = equilibrium + atmosphere.greenhouse_k

    # Final consistency pass. Each iteration filtered gases against the
    # temperature it *started* with, and the temperature moved afterwards, so
    # the settled composition can still list a gas the settled temperature would
    # boil off. Re-filter against the temperature the world actually ends at.
    atmosphere = _enforce_retention(star, orbit, body, atmosphere, surface)
    if atmosphere.pressure_bar <= 0.0:
        surface = equilibrium_temperature(star, orbit.semi_major_axis_au, atmosphere.albedo)
    else:
        equilibrium = equilibrium_temperature(
            star, orbit.semi_major_axis_au, atmosphere.albedo
        )
        surface = equilibrium + atmosphere.greenhouse_k

    # Seasons come from axial tilt; eccentricity adds a second, separate swing.
    # A thick atmosphere buffers both, which is why Venus has no seasons.
    tilt_swing = 28.0 * math.sin(math.radians(min(90.0, orbit.axial_tilt_deg)))
    eccentric_swing = surface * orbit.eccentricity * 0.5
    buffer = 1.0 / (1.0 + atmosphere.pressure_bar * 0.25)
    swing = (tilt_swing + eccentric_swing) * buffer

    # Tidally locked worlds have no day/night cycle at all -- instead they have
    # a permanent hot face and cold face, which we express as an extreme swing.
    if orbit.tidally_locked:
        swing = max(swing, 60.0 * buffer)

    tilt = min(90.0, orbit.axial_tilt_deg)
    polar = max(0.02, min(0.6, 0.20 - tilt / 400.0))
    tropical = max(0.05, min(0.85, 0.25 + tilt / 120.0))
    temperate = max(0.05, 1.0 - polar - tropical)
    band_total = polar + tropical + temperate

    climate = Climate(
        equilibrium_temp_k=round(equilibrium, 2),
        surface_temp_k=round(surface, 2),
        seasonal_swing_k=round(swing, 2),
        polar_fraction=round(polar / band_total, 4),
        temperate_fraction=round(temperate / band_total, 4),
        tropical_fraction=round(tropical / band_total, 4),
    )
    return atmosphere, climate


def _enforce_retention(
    star: Star,
    orbit: Orbit,
    body: Body,
    atmosphere: Atmosphere,
    surface_temp_k: float,
) -> Atmosphere:
    """Drop any gas the world cannot hold at its settled temperature.

    Losing gas costs pressure, which costs greenhouse, so this is applied once
    at the end rather than fed back into the solve -- the feedback is
    stabilising (less gas, cooler, easier to retain what is left), so a single
    pass cannot leave it worse than it found it.
    """
    if atmosphere.pressure_bar <= 0.0 or not atmosphere.composition:
        return atmosphere

    kept = {
        gas: fraction
        for gas, fraction in atmosphere.composition.items()
        if retains(gas, body.escape_velocity_kms, surface_temp_k)
    }
    total = sum(kept.values())
    if total <= 1e-9:
        return Atmosphere(pressure_bar=0.0, composition={}, albedo=0.12, greenhouse_k=0.0)
    if total >= 0.999999:
        return atmosphere

    composition = {gas: fraction / total for gas, fraction in kept.items()}
    pressure = atmosphere.pressure_bar * total
    return Atmosphere(
        pressure_bar=round(pressure, 5),
        composition={g: round(a, 5) for g, a in sorted(composition.items()) if a > 1e-5},
        albedo=atmosphere.albedo,
        greenhouse_k=round(greenhouse_forcing(pressure, composition), 2),
    )


def roll_water_endowment(rng: random.Random, beyond_frost_line: bool) -> float:
    """How much water the world accreted when it formed.

    Rolled before the atmosphere, because whether a world has water decides
    whether its climate has a thermostat at all -- see
    :func:`carbonate_silicate_drawdown`.
    """
    return rng.uniform(0.0, 1.0) * (2.5 if beyond_frost_line else 1.0)


def build_hydrosphere(
    rng: random.Random,
    body: Body,
    atmosphere: Atmosphere,
    climate: Climate,
    beyond_frost_line: bool,
    water_endowment: float,
) -> Hydrosphere:
    """Surface water, gated on the phase diagram.

    Liquid water needs a temperature between freezing and boiling *and* enough
    pressure to keep it from subliming straight to vapour. Below the triple
    point there is no liquid at any temperature -- the reason Mars has ice caps
    and vapour but no lakes.
    """
    if water_endowment < 0.12 or body.mass_earth < 0.05:
        return Hydrosphere(False, 0.0, 0.0, 0.0)

    temp = climate.surface_temp_k
    pressure = atmosphere.pressure_bar

    # Boiling point rises with pressure; at Earth pressure it is 373 K.
    boiling = WATER_BOIL_K * (1.0 + 0.28 * math.log10(max(pressure, 0.01)))

    if pressure < WATER_TRIPLE_POINT_BAR or temp > boiling:
        # Too thin or too hot for liquid. Ice may still survive if cold.
        ice = min(0.9, water_endowment) if temp < WATER_FREEZE_K else 0.0
        return Hydrosphere(False, 0.0, round(ice, 4), 0.0)

    if temp < WATER_FREEZE_K:
        # Frozen over. Still no liquid surface, but plenty of water.
        return Hydrosphere(False, 0.0, round(min(0.95, water_endowment), 4), 0.0)

    ocean = min(0.95, water_endowment * rng.uniform(0.5, 1.2))
    # Ice caps shrink as the world warms.
    ice = max(0.0, min(0.35, (WATER_FREEZE_K + 25 - temp) / 90.0)) * rng.uniform(0.5, 1.5)
    depth = ocean * rng.uniform(1.0, 6.0)

    return Hydrosphere(
        liquid_water=True,
        ocean_fraction=round(ocean, 4),
        ice_fraction=round(min(ice, 1.0 - ocean), 4),
        mean_ocean_depth_km=round(depth, 3),
    )
