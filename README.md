# Galaxy-Sim

An async, persistent, text-based civilization simulation. Players colonize
procedurally generated worlds across a shared galaxy, research an open-ended
tech space, and build up civilizations over weeks of real time.

The core loop works end to end and the world underneath it is real: planets
generated from physics, an economy made of actual elements, populations counted
in billions, terraforming that rewrites a planet's numbers, and a hundred
thousand light-years of spiral galaxy that costs nothing until somebody flies
into it. Procedural tech generation, conflict resolution and the species
pipeline are next. [`docs/DESIGN.md`](docs/DESIGN.md) is the architecture
reference; the notes below say what is deliberately placeholder.

## Try it

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

.venv/bin/galaxysim new Frontier --ai 3        # solo game, 3 AI opponents
.venv/bin/galaxysim new Deep --region core     # ...or start somewhere harder
.venv/bin/galaxysim status                     # your civ
.venv/bin/galaxysim systems                    # what you have charted
.venv/bin/galaxysim chart --radius 60          # ...and what is out there
.venv/bin/galaxysim planet 12                  # full survey, incl. what powers it
.venv/bin/galaxysim colony 1                   # one colony in detail, incl. its grid
.venv/bin/galaxysim labor 1 --extraction 3 --industry 2 --life-support 1
.venv/bin/galaxysim structure                  # list buildings
.venv/bin/galaxysim structure 1 laboratory     # build one
.venv/bin/galaxysim govern 2 --policy survival # delegate a colony
.venv/bin/galaxysim move 1 24                  # send fleet 1 to system 24
.venv/bin/galaxysim colonize 1 60 --preview    # what would settling cost?
.venv/bin/galaxysim colonize 1 60 --stores 200 # send an expedition
.venv/bin/galaxysim route 2 --from 1 --to 3 --carry water:50
.venv/bin/galaxysim tick 24                    # advance a day
.venv/bin/galaxysim log                        # what happened
```

`galaxysim soak --ai 8 --days 28` runs a throwaway universe of AI civs and
charts how fast they grow. It is the pacing check — if a civ reads as "big"
inside a few simulated days, the cost curves need steepening.

Saves default to `./galaxysim.db`; set `GALAXYSIM_DB` to move it.

## Planets are derived, not rolled

A world is not a row of independently rolled stats. It is generated in causal
order, and every consequence follows from the physics:

```
orbit → body → outgassing → [temperature ⇄ atmosphere] → hydrosphere
      → biosphere → free oxygen → final temperature → habitability
```

Only a few things are actually rolled — where a planet orbits, its mass, its
rotation and tilt, how much gas it outgassed. Everything else is computed. So
you will never see an airless world with oceans, liquid water at 200 K, or a
small hot rock holding hydrogen. Not because a rule forbids it, but because the
physics never produces it. `tests/test_planets.py` generates twelve thousand
worlds and checks exactly that.

The model is validated against the only three planets anyone has measured:

| | model | reality |
|---|---|---|
| Earth surface temperature | 287 K | 288 K |
| Mars surface temperature | 215 K | 210–218 K |
| Venus surface temperature | 727 K | 737 K |
| Sun-analogue habitable zone | 0.95–1.37 AU | 0.95–1.37 AU |
| Sun-analogue frost line | 4.84 AU | ~4.85 AU |
| Earth CO₂ after weathering | 0.04% | 0.04% |

Some of the mechanisms worth knowing about, because they produce most of the
interesting behaviour:

- **Atmospheric retention is a physics check**, not a roll. A world keeps a gas
  if its escape velocity comfortably beats that gas's thermal velocity at that
  temperature — which is why Earth keeps nitrogen and loses hydrogen, and why
  tiny frigid Titan keeps a thick atmosphere.
- **The carbonate–silicate cycle is why Earth isn't Venus.** Rain weathers CO₂
  into carbonate rock, faster when hotter — a negative feedback that regulates
  temperature. Lose your oceans and you lose the thermostat, and the CO₂ runs
  away.
- **Free oxygen is a biosignature.** It is too reactive to persist without life
  replenishing it, so in this model photosynthesis *causes* the oxygen rather
  than the oxygen being rolled and life inferred.
- **Tectonics concentrates ore.** Bulk abundance is not mineable ore —
  hydrothermal circulation is what gathers a diffuse element into a seam. A dead
  world may hold as much copper as Earth with none of it usable.
- **Three quarters of stars are red dwarfs**, on the real distribution, and
  their habitable zones sit close enough in to tidally lock — the actual
  objection to red-dwarf habitability, falling out of the arithmetic.

Habitability is the *last* thing computed and never an input to anything above
it. `galaxysim planet <id>` prints the full survey and shows the score as a
conclusion with its reasoning attached.

Breathable worlds are about 1 in 6,000. Finding one is an event; a civ's own
homeworld is guaranteed by searching a life-bearing star's habitable zone for a
world the generator would genuinely produce — never by overwriting a number.

## The economy is made of real substances

There is no `metal`, no `energy`, no `volatiles`. There are twenty-nine
materials, and the raw ones are **the elements the geology generator already
produces** — extraction is a direct lookup against a world's crust, with no
mapping layer in between.

**A world produces what it is made of.** Iron ore, bauxite, rare earths,
uranium, water ice, helium-3 — each with an abundance, a concentration set by
how tectonically active the world is, and a depth. A world with no uranium in
its crust yields none at any labour allocation, any infrastructure level and any
technology. Absence is real, which is what makes a uranium-bearing world an
objective rather than a preference.

**Refining is where specialisation lives.** Ore is nearly useless: buildings are
priced in steel and construction materials, ships in alloys and electronics,
life support in water. Twelve chains convert one into the other, mass is
conserved minus tailings, and **nothing substitutes**. Alloys and fissiles each
have a fallback route so geology can never lock a civ out of a whole tier;
electronics deliberately has one, needing copper, rare earths and silicon
together — it is meant to be the chain that forces trade.

Refining draws on the same industry-work pool as construction, split by
`Rates.refining_share_of_industry`. A colony cannot both process everything it
digs and build at full speed. Left alone it works through every chain it can,
evenly — deliberately mediocre, so naming the two or three chains a world is
actually good at beats it (`galaxysim refining <id> --chain smelting:2`).

**Research is bought, not banked.** Laboratories consume electronics, polymers,
ceramics and fuel out of the stockpile where they stand. A colony with the
workers, the buildings and an empty warehouse discovers nothing — so an
industrial base is a prerequisite for a scientific one, and cutting a rival's
supply line slows their tech without any rule saying so. The bill is payable in
common goods only, never strategic ones, so a metal-poor start researches
*slower*, never not at all. Rare earths, fissiles and helium-3 appear as
optional **accelerants**: they speed research when supplied and cost nothing but
the material when they are not.

## Societies at real scale

Population is people — real ones, in the billions — and materials are tonnes.
Rates are anchored to reality where reality offers an anchor: humanity moves
~100 Gt of ore a year across 8 billion people, so extraction is set from
1.4×10⁻³ t per person per hour, and a person eats 2 kg of food a day. Everything
else is derived from those and says so in `engine/rates.py`.

**A homeworld opens nearly full.** A species with lightspeed travel is not a
landing party: its capital holds several billion people at 88–96% of what the
planet can hold, so it grows a few percent and then stops. Essentially all
growth has to come from expanding, which is the decision the opening position
exists to force. The capital is a reservoir and an industrial heart, not a thing
you develop.

**Capacity is physical, and there are two regimes.** A living world is limited
by its land: `land_area × 53 people/km² × habitability`, which puts an
Earth-analogue near 13 billion. A dead world's natural capacity is genuinely
zero — nobody lives outdoors on Mars — so its ceiling is whatever the habitats
hold, a few million at most. **Terraforming is the only thing that closes that
gap**, and closing it is a thousandfold transformation. That is what the whole
terraforming tree exists to buy.

**Agriculture is the fifth labour sector**, and the world decides what it costs.
Open farmland on a compatible biosphere feeds a colony with ~3% of its people; a
sealed world needs more; a bare rock manufactures every calorie in hydroponics
with ~35% of its population and imports fertiliser besides. A world can be
perfectly breathable and still a terrible farm, which gives edible biospheres a
payoff habitability alone never captured. **Standard of living** — how much of
what people needed was actually met — scales growth, and reverses it below
subsistence.

**Industry is bounded by people and ground, not slots.** `World.slots` is gone.
Buildings are industries with levels: cost rises with the square of the level
while effect rises with its square root, so the tenth level costs a hundred
times the first and is worth three times as much. Total levels are capped by
`population / 250k` and `land_area / 90k km²`, whichever binds first — a crowded
moon and an empty continent fail in opposite directions, and both limits rise as
the colony grows. The consequence is the one the design wanted: past a point,
the next tonne of steel is better spent founding a colony than deepening one.

**Migration is a convoy.** People ride in the same hold as ore, at half a tonne
each, so a migration run is a supply run you did not make. Nobody is forced to
run them — natural growth carries a habitable colony to maturity in three to
four weeks — but a garden world with room and nobody on it, next to a capital
that is full, is a situation only shipping fixes.

**Nothing artificial slows a wide empire.** `Rates.colony_overhead` is deleted,
and so is the fleet cap that replaced it in the AI. Twelve colonies produce what
twelve colonies produce. What slows a large empire is real: distance, supply
lines that have to be defended, worlds that cost more to hold than they yield,
and the hourly upkeep of everything you built.

## Everything costs what it weighs

For two phases it did not. When the economy went to real tonnes, terraforming
and research came with it and **buildings, ships, expeditions and fleet upkeep
were left behind** — so a civilization producing eighty-six million tonnes an
hour bought a warship for thirty-eight tonnes and kept an entire navy flying for
seventeen tonnes an hour, two hundred-thousandths of one percent of its output.
Nothing real bound anything, which is why the AI needed an artificial cap: it
was the only brake in the game.

Prices are now measured rather than picked. `tests/test_prices.py` generates a
capital and a landing party — 1,401,102 and 0.24 units of construction an hour, a
factor of six million — and asserts the whole ladder against what they actually
produce, so the next time a rate moves this fails instead of quietly going slack:

| | cost | on a developed capital |
|---|---|---|
| Industry, level 1 | 55,000 t | minutes (a fortnight for a landing party) |
| Industry, level 200 | 2.2 Bt | ~2.5 weeks |
| Industry, level 320 | 5.6 Bt | ~10 weeks |
| Warship, strength 2 | 400,000 t | ~8 hours |
| Terraforming, one project | 470 Mt | ~1.5 days |
| Terraforming, full transformation | ~8 Gt | ~3 weeks |

**Work scales as the cube of the level while materials stay quadratic.** Effort
and tonnage do not rise together in real industry — the hard part of a bigger
plant is siting, power and integration rather than fabrication — and it is also
the only curve that spans the economies it has to. One exponent cannot make
level 1 affordable to fifty thousand colonists *and* level 320 a season's work
for a capital producing a million times as much.

Fleet upkeep keeps its documented 1.5%-of-build-cost ratio and moves with the
base. A hundred points of strength now costs **10.7% of a capital's hourly
industry** — the "about a tenth of refined output" its docstring had claimed
since it was written, and which was false by four orders of magnitude because
nobody had checked it.

## Power is a flow

The one quantity here that is a rate compared against a rate. Generation this
hour against what industry, mining and life support want this hour, with the
ratio scaling output — a colony short of power is not dead, it is *throttled*,
and it recovers the hour somebody delivers fuel. There is a floor well above
zero, because a colony that could reach no power could never mine the fuel to
restart and a constraint you cannot escape is a trap rather than a constraint.

**Demand scales with industrial output, not headcount.** That distinction is the
whole mechanic: scaled to people it would grow at exactly the rate the free
baseline does and never bind on anyone at any size. Scaled to output, a colony
that deepens its industries outgrows its grid — which is when it should have to
think about one. A landing party never does.

Four routes, each gated on something the generator already rolled:

- **Solar** on stellar flux `L/a²`. Three quarters of stars are red dwarfs and
  their worlds are dark.
- **Geothermal** on tectonic activity — which is also what concentrates ore into
  seams, so the best mining worlds can power their own mines and the dead ones
  that are cheap to live on must import their fuel. Nobody wrote that down.
- **Fission** on fissiles, and **fusion** on deuterium and helium-3 — which is
  what finally makes a barren regolith world worth crossing a frontier for.

Generation is linear in level where everything else is a square root: ten
reactors make ten reactors' power. The diminishing return lives in the quadratic
materials bill instead, so the economics still discourage stacking one plant
without the physics having to pretend.

This is also the sink extraction never had. `recipes.py` has carried a comment
pointing at "the energy model" since the refining chains were written, while
deuterium and helium-3 sat in the catalogue consumed by nothing.

## Terraforming: what the surplus is for

Everything the economy produces eventually piles up. A developed world mines
faster than it refines, refines faster than it builds, and once its industries
are deep the next level costs more than it returns. Terraforming is the sink at
the end of that chain, and the only one that pays back in something other than
more of the same.

**A project changes the planet's real numbers.** Magnetic shields, atmosphere
processors, greenhouse seeding, orbital shades, cometary redirection, scrubbing,
ecosystem seeding, oxygenation — each rewrites the stored survey, and then
habitability, breathability, capacity and agricultural quality are re-derived by
exactly the functions that derived them at generation. There is no
terraforming-specific habitability path, so a half-terraformed world reads like
a world that is naturally halfway there.

**The order falls out of physics, not a tech tree.** Each project states the
physical fact it needs — a field to hold air down, air to work on, liquid water
to seed life into — and the failure tells you which is missing. Consequences
nobody wrote down come free: thickening the air of a hot world makes it *hotter*,
because greenhouse forcing has always scaled with pressure.

Running the sequence on a cold rock:

```
Qiomdri V           hab 0.000  0.35 bar   221 K            capacity 0
  Magnetic Shield   hab 0.000  0.35 bar   221 K            capacity 0
  Atmosphere x3     hab 0.000  1.10 bar   243 K            capacity 0
  Greenhouse x3     hab 0.094  1.10 bar   297 K            capacity 1.07B
  Cometary          hab 0.189  1.10 bar   297 K  liquid    capacity 2.61B
  Ecosystem         hab 0.295  1.10 bar   297 K  complex   capacity 4.30B
  Oxygenation x2    hab 1.000  1.26 bar   297 K  BREATHABLE capacity 16.15B
```

That is the outpost-to-world transition: a dead rock capped at what its habitats
hold becomes a world of sixteen billion. It is the only thing in the game that
produces a four-order-of-magnitude change, and it is why reshaping a planet is
worth a civilization's entire surplus.

**It requires trade.** Costs are hundreds of megatonnes, and in practice the
binding constraint is not ore but *electronics* — the one refining chain with a
single route, needing copper, rare earths and silicon together. A single-colony
civ six weeks in has billions of tonnes of ceramics and zero electronics. That
is the materials economy doing what it was built to do.

## The galaxy is a function, not a map

There is no world count and no bound. Space is a pure function of
`(universe_seed, sector, index)`: ask what is in a cubic parsec and the answer
is computed, identically, forever. A system becomes a *row* the moment somebody
arrives and not before — because that is the moment it acquires state generation
cannot derive: who owns it, what has been mined out, who has a colony there.
Two players reaching it from opposite directions a month apart find the same
star and the same ore.

The disc is the real one, near enough to check. Stellar density is a bulge plus
an exponential disc plus four log-spiral arms as density *ridges* rather than
walls, and the one anchor is measured rather than chosen: near the Sun's radius
it puts stars 5–8 light-years apart, which is what they are. Metallicity runs
down the same gradient real galaxies have, −0.06 dex per kiloparsec. Nothing has
an edge; density falls off and keeps falling.

**Scarcity comes from where the players are, not from shrinking the map.**
Nobody fights when there is land for everyone, and a hundred billion stars is
land for everyone. So every civilization is seated inside one small **settlement
frontier**, sized once from a target of first contact in one to two weeks and
never grown:

| Players | Median neighbour gap | First contact |
|---|---|---|
| 25 | ~90 ly | weeks |
| 100 | ~50 ly | 1–2 weeks |
| 300 | ~30 ly | days |

Pressure rises with population on its own. Nothing enforces it — it is what is
left when a fixed volume takes more people. And the rest of the galaxy is the
escape valve for anyone willing to travel a very long way for elbow room.

Inside that frontier there are about a thousand systems per player and almost
nothing among them worth having on its own terms: under 1% of worlds are
liveable at all and breathable ones number in the low tens against a hundred
civs. What makes a world valuable is what is in it and where it sits.

**Where you start is a decision.** Density and metallicity run the same
direction, so you cannot have distant neighbours *and* good ore:

| | Neighbours | Metallicity |
|---|---|---|
| **Core** | 1–2 ly away, on top of you from the first week | [Fe/H] +0.34 |
| **Arm** | a handful of ly, company soon enough | [Fe/H] ≈ 0 |
| **Rim** | 10+ ly, long journeys, nobody finds you | [Fe/H] −0.40 |

Those figures are printed by `galaxysim chart`, which lists every star within
reach whether or not anyone has been there. You get the star and where it is,
because that is what a telescope gives you at forty light-years. You do not get
its worlds.

## The colony

A colony is a place you run, not a number that goes up.

**Matter is local; knowledge is civ-wide.** There is no treasury. Each colony
holds its own stockpile, and ore mined on one world is on that world until a
ship carries it elsewhere. Ships are paid for by the yard building them, fleet
upkeep by the nearest colony to the fleet — so projecting force far from home
means feeding it out there. Research is the one exception: a discovery is known
everywhere the moment it is made.

**Population is assigned to work**, across extraction, industry, research, life
support and agriculture. The last two are the interesting ones, because they are
*bills* rather than investments. Life-support load scales with
`1 - habitability` and agriculture with how bad a farm the world is, so a hostile
world takes two bites out of the workforce before anyone mines anything.
Habitability is not a growth cap, it is a tax on labour.

**Life support consumes water as well as people.** Sealed habitats need
consumable input; workers cannot make air out of nothing. A world with oceans
hands its colonists water for free. A dry world has two options — mine ice and
refine it, or import every drop — and a world with neither ice nor ocean has
only the second. This is what makes hostile worlds *supply-dependent* rather
than merely expensive, and geology plays against it on purpose:

> The best mining worlds are dry by definition. They are among the richest
> worlds in the game and they cannot keep anyone alive without a supply line.

**Industries are levelled** and paid for in industry-work, so a colony with
nobody in industry finishes nothing. Shipyards gate fleet construction;
spaceports gate cargo and passenger throughput; domes cut what life support is
*needed*; hydroponics cuts what meeting it *costs* and helps the harvest.

**Founding is an expedition, not a fee.** You compose colonists, equipment and
stores; what you send is both the price and what the colony wakes up with.
Hostility and distance are never surcharged — settle a garden world and a bare
rock with the same manifest and you pay exactly the same. The difference is that
on the rock the manifest is not enough. `colonize --preview` shows the survival
arithmetic; a bad loadout is a legal order that kills its colonists.

**Logistics connects the stockpiles.** Freighters are ordinary fleets with a
large hold, so cargo runs reuse the movement resolver unchanged. A *standing
supply route* shuttles between two colonies indefinitely — set it up once and it
keeps feeding an outpost while you are asleep, which is what makes manual
logistics survivable in an async game.

**Depth is opt-in.** Every colony can be handed to a governor that runs it to a
policy (`balanced`, `extraction`, `industry`, `research`, `survival`), and new
colonies are governed by default. You opt *into* detail on the colonies you care
about rather than opting out everywhere else. A governor gets no hidden bonus —
it issues the same orders you could, so micromanaging well still wins. Life
support runs automatically in both modes: forgetting to assign it must never
silently kill a colony while its owner is asleep.

## The three ideas the design rests on

### 1. Tick rate is granularity, not pace

Two knobs that are easy to conflate and must not be:

- **Cadence** — how often the world updates. Frequent (five minutes by default)
  so async play feels responsive and a player returning after a night away
  reads a fine-grained history instead of one enormous jump.
- **Rates** — how fast a civilization actually grows. Slow, deliberately.

Every rate in `galaxysim/engine/rates.py` is authored **per real hour**, and the
only place ticks enter the arithmetic is `Cadence.per_tick`. Nothing else in the
codebase may express a quantity "per tick". Follow that and switching cadence
changes only the resolution of the event log — nobody grows faster or slower.
`tests/test_pacing.py::test_growth_is_equivalent_across_cadences` enforces it.

Several brakes keep growth compounding-resistant:

| Brake | Where | What it stops |
|---|---|---|
| Research cost by depth (superlinear) | `Rates.research_cost` | A lineage running away |
| Construction time, cubic in level | `colony/industry.py` | Buying a developed world in an afternoon |
| Power demand rising with output | `colony/energy.py` | Industry outgrowing what runs it |
| Fleet upkeep, billed where the fleet is | `FLEET_UPKEEP_PER_STRENGTH` | Hoarding a free navy |
| Life-support and food bills | `Rates.life_support_*`, agriculture | Holding hostile worlds cheaply |
| Local stockpiles | `Colony.stockpile` | Funding expansion from one pooled purse |
| Industry levels capped by people and land | `colony/industry.py` | Deepening one world forever |

Every brake in that table is a real cost of something. The two that were not —
a colony-count price multiplier and `Rates.colony_overhead` — are both deleted.
Expansion *should* get easier as you grow; that is the reward for growing, and
what makes it hard is distance, supply and defence.

### 2. Every tick is replayable

Players are asleep for most of the ticks that affect them. If a tick cannot be
reproduced, someone who loses a fleet overnight has no way to check what
happened and no reason to trust it.

- All randomness descends from `tick_seed(universe_seed, tick_number)`.
- Resolvers take *scoped* RNG streams (`ctx.rng("combat", a, b)`) so adding an
  entity cannot shift another resolver's draws.
- Seeds use blake2b, never `hash()` — CPython randomizes string hashing per
  process, so a restarted server would resolve ticks differently from the ones
  it had already written.
- Every query carries an explicit `ORDER BY`. Resolution order determines RNG
  consumption and row insertion order, so it is part of the game rules.
- A tick commits as one transaction.

### 3. Orders are queued, never applied on submission

`galaxysim/engine/intents.py` is the entire write surface. Players, the CLI, the
future HTTP layer and the AI all go through it — the AI has no privileged path
into the simulation, which is what makes solo mode an honest test of the
multiplayer one, and what lets a hundred AI civs stand in for a hundred players.

Orders resolve together on the tick, so being awake when you submit confers no
advantage. Production, research and defense all run for offline civs.

## What a tick may cost

Performance is a design property, not an optimisation detail. A shared universe
ticks on a wall clock for everybody at once, so a tick that takes a second at
three hundred colonies puts a ceiling on how big the game can be.

The rule: **the number of database round trips a tick makes must not grow with
the size of the universe.** The failure mode is always the same — a resolver
asks a question inside a loop over colonies or civilizations. It reads
correctly, passes every other test, and quietly turns the cost from
"proportional to what happened" into "proportional to how much exists". It has
happened twice, and `tests/test_tick_cost.py` now fails on either shape of it.

Two things follow from that rule and are worth knowing before adding a resolver:

- **Load relationships up front.** `queries.py` eager-loads a colony's world,
  that world's system, and its buildings, because anything walking colonies will
  touch all three.
- **A world's `survey` is never read during a tick.** It is a large JSON
  document and the session is per tick, so every load decodes it again. The
  handful of facts the loop needs — extraction rates, surface water, farm
  quality — are promoted to columns beside it and refreshed by terraforming,
  which is the only thing that can change them.

## Pipeline

Resolution order is fixed and meaningful (`galaxysim/engine/tick.py`):

1. **Movement** — fleets arrive before anything acts on where they are
2. **Logistics** — a freighter that landed this tick unloads now, so supplies
   reach a starving colony before life support is computed against them
3. **Combat** — a fleet that arrived this tick is immediately at risk
4. **Governor** — governed colonies decide labor before production reads it
5. **Production** — life support, then work, then construction
6. **Research** — spends what production just banked
7. **Colonization** — last, so a contested world is settled by whoever holds it

## Layout

```
galaxysim/
  core/        seeds, RNG derivation, continuous-space coordinates
  materials/   catalogue, refining chains, extraction, prices
  model/       SQLAlchemy entities (SQLite for solo, Postgres for shared)
  worldgen/    the galaxy function, stars, planetary physics, geology, life
  colony/      labour sectors, industries, population, agriculture, energy
  terraform/   project catalogue, and applying one back into a planet
  engine/      rates, intent queue, tick pipeline, resolvers
  ai/          AI civs, driving the same intent API a player uses
  flavor/      name generation
  cli/         Typer + Rich terminal client
```

## What is placeholder

Named explicitly so nobody mistakes scaffolding for design:

- **Tech is a depth counter.** `Civ.techs_known` stands in for the generated
  lineage of step 7 — genomes, a bounded effect grammar, and a frontier of
  candidate techs derived from what a civ already knows. The economics of
  paying for the next step will not change when it lands; only what you receive.
- **Conflict has no resolution.** Combat destroys fleets and nothing else, so
  colonies cannot change hands and there is no way to win anything. Blockade and
  capture are the next phase.
- **Species descriptions are stored but unused.** The extraction pipeline is
  step 6.
- **Names are syllable soup.** Step 7 gives each civ phoneme banks derived from
  its own species profile.

## Known tuning gaps

- **Expansion levels off at what the population supports.** A 28-day soak with
  8 AI civs runs 6 → 16 → 18 colonies over the first six days and then holds at
  18. The shape is right and the ceiling is the design rather than a brake: an
  outpost holds tens of thousands against a homeworld's eleven billion, so
  settling rocks barely moves the population, and everything scaled to
  population — fleet upkeep most of all — stops rising with it. The only thing
  that lifts it is terraforming a world into somewhere billions can live, which
  is exactly what the tree is for and exactly what no AI does yet. Whether 18 is
  the *right* plateau is a question for a play session.
- **A tick costs ~760 ms with 8 civs and 144 colonies.** About 5 ms a colony,
  down from 8 — the query count is flat in universe size and tested to stay
  that way, so this is honest per-row cost rather than the quadratic shape that
  was fixed last phase. It still wants profiling before a shared universe is
  real, since a five-minute cadence resolves 288 ticks a day.
- **Supply routes over-deliver.** A standing route ships its full manifest every
  trip whether or not the destination needs it, so a well-supplied outpost banks
  months of surplus. A route that tops up to a target level would be better.
- **Ships take about twice as long to build.** Refining takes half the industry
  pool, so construction runs at half the rate it did. That is the intended
  shape — ore has to become steel before it can become a hull — but the split is
  a first-pass number that wants a play session, not a spreadsheet.
- **A fleet cannot be decommissioned.** Upkeep is a one-way ratchet: a colony
  ship that has landed its pod is a hull with no purpose and a permanent bill,
  and there is no way to scrap it or recover its materials. That mattered less
  when a ship cost nineteen tonnes; now that upkeep is a real fraction of
  output, a fleet of spent settlers is a real drag.
- **The AI terraforms one planet at a time and never re-plans.** It picks a dead
  world with a developed neighbourhood and works the sequence, which is the
  right shape, but it will not abandon a bad target or run two campaigns at
  once even when it could afford both.
- **Nobody has played a core start for long.** Neighbours 1.4 ly apart is a very
  different game from neighbours 10 ly apart, and the difference is currently a
  claim backed by density arithmetic rather than a session.

## Tests

```bash
.venv/bin/python -m pytest
```

The load-bearing ones are determinism (`tests/test_determinism.py`) and pace
invariance (`tests/test_pacing.py`). The rest of the suite can be wrong and the
game still works; if those are wrong, nothing else matters.

`tests/test_colony.py` and `tests/test_logistics.py` cover the colony layer. The
one to read first is
`test_logistics.py::test_a_supply_route_keeps_a_dead_world_alive` — a barren
world that cannot feed itself, held indefinitely by one standing route. That is
the loop the whole colony design is arranged around.
