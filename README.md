# Galaxy-Sim

An async, persistent, text-based civilization simulation. Players colonize
procedurally generated worlds across a shared galaxy, research an open-ended
tech space, and build up civilizations over weeks of real time.

The core loop works end to end and the world underneath it is real: planets
generated from physics, an economy made of actual elements, populations counted
in billions, terraforming that rewrites a planet's numbers, wars that end with a
world changing hands, and a hundred thousand light-years of spiral galaxy that
costs nothing until somebody flies into it. Procedural tech generation and the
species pipeline are next. [`docs/DESIGN.md`](docs/DESIGN.md) is the
architecture reference; the notes below say what is deliberately placeholder.

## Try it

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

.venv/bin/galaxysim new Frontier --ai 3        # solo game, 3 AI opponents
.venv/bin/galaxysim new Deep --difficulty driven   # ...or opponents who play
.venv/bin/galaxysim new Edge --region core     # ...or start somewhere harder
.venv/bin/galaxysim status                     # your civ
.venv/bin/galaxysim empire                     # where your industry actually is
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
.venv/bin/galaxysim terraform 1                # the whole campaign, and its bill
.venv/bin/galaxysim terraform 1 magnetic_shield # commit to a rung
.venv/bin/galaxysim civs                       # who else is out there
.venv/bin/galaxysim attack 3                   # ...and pick a fight with one
.venv/bin/galaxysim scrap 4                    # break up a hull, stop its upkeep
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
life support in water. Thirteen chains convert one into the other, mass is
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

**Sometimes a world gets worse on the way up, and that is the ladder working.**
Thickening the air of a warm world raises its greenhouse forcing, so a step can
cost habitability — one world measured at 0.25 → 0.12 → 0.23 across two
projects. This reads like a sequencing bug from outside, and this file used to
call it one: *a planner that looked one rung ahead would not do it.* That was
asserted without checking, and checking says the opposite. Across 211 finishable
worlds, a planner that refuses any step lowering habitability and otherwise
takes the best one available was **shorter on none of them, longer on 34, and
failed to finish 177 outright** — five worlds in six stranded. Every one of the
59 dips in the sample had a legal alternative that would not have dipped, which
is exactly why the greedy planner looks attractive and exactly why it strands
them: refusing to make a world temporarily worse means never building the
atmosphere everything after it depends on. `plan._wanted` is ordered by *what
blocks what*, not by outcome, and `tests/test_terraform.py` runs both planners
over a generated galaxy so the next attempt to optimise it fails loudly.

**It has to be priced in what the economy actually makes**, and for a long time
it was not. This file used to say the binding constraint was electronics and
call that the materials economy working as designed. It was a defect wearing the
costume of one. The recipe asked for copper and silicon at four parts to six
while the ground yields them at 0.02 to 0.75 — copper twenty-five times faster
than any world produces it — so empires sat on twenty-one *thousand* megatonnes
of silicon and a tenth of a megatonne of copper, and copper alone decided how
many electronics existed anywhere. Fissiles were worse: enrichment needs uranium,
uranium is in the ground on fifty-five worlds out of four hundred and fifty-seven,
and `oxygenation` — the last step to every habitable world — was priced in it.
Every civilization in every soak held exactly zero.

Both are fixed at the source rather than by discounting the projects: the
recipe is silicon-dominant now, which is what electronics are actually made of,
and no project is priced in fissiles. Totals are unchanged. **A campaign is
about fifteen projects and roughly a week of a developed neighbourhood's
industry**, and the readout tells you the whole bill before you start it.

**Two thirds of worlds cannot be finished at all.** An orbital shade cools by
reflection and albedo saturates, so a world whose own air holds it above the
growing band stays there whatever is spent. The ladder used to keep asking for
another shade for ever — 1,295 worlds out of 1,966 in a sweep — and a
civilization would pour 43.5 million work and sixty million tonnes into a
project that provably could not alter the survey, then do it again. It now
stops, and `World.terraform_finishable` says so *before* anything is spent,
because a hopeless world still swallows nine real projects on the way to
saturating, every one of which looks like progress.

## War takes a frontier, never a heart

Combat used to be fleets grinding each other down and nothing else: two
civilizations could destroy each other's navies for a month and finish holding
exactly the worlds they started with. Three ideas make it end in something, and
each falls out of what already existed rather than adding a rule.

**A blockade is just presence.** Hold more strength over somebody's colony than
they do, while at war, and cargo stops moving — nothing loads or unloads at
either end. That is not a new penalty; it is the design's existing dependence on
supply lines finally being worth attacking. An outpost lives on its route, so
cutting the route kills it, and the `life_support_failing` event tells an offline
owner exactly what is happening.

**A siege wears down what is actually there.** A colony's ability to hold out is
its people and what they have built — nothing invented, nothing assigned. That
one choice does all the balancing: a fifty-thousand-person outpost falls in
hours, and a homeworld of eighteen billion cannot be taken from orbit in any
practical time by any fleet a civilization could keep flying. So war takes a
rival's frontier and never their heart, which is emergent rather than a rule, and
it is what protects a player who is asleep.

**Taking a world needs a colony pod.** Grinding the resistance to nothing leaves
a colony besieged and suffering; somebody still has to land an administration.
Annexation is therefore a real investment rather than a side effect of winning a
battle, accidental capture is impossible, and blockade-without-annex — starving a
rival rather than absorbing them — is a strategy that exists for free. What
changes hands is a *developed* world, buildings and warehouses included, minus a
quarter of its people. That is why capturing beats founding.

Projecting force is expensive on purpose. Fleet upkeep is billed to the colonies
near the *fleet*, and there is nothing to draw on past supply range, so a
squadron parked deep in somebody else's space deserts within days. Besieging a
world is something you have to be able to sustain.

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

**Where you start is a decision**, and the decision is how much is within reach.
Density and metallicity run the same direction, so you cannot have an empty sky
*and* good ore:

| | Stars apart | Systems within supply range | Metallicity |
|---|---|---|---|
| **Core** | 1–2 ly | ~7,600 | [Fe/H] +0.34 |
| **Arm** | a handful of ly | ~520 | [Fe/H] ≈ 0 |
| **Rim** | 10+ ly | ~85 | [Fe/H] −0.40 |

What region does *not* change is your neighbours. Seating is spaced in
light-years from the player count, never from density, so at eight players the
nearest rival sits 48–51 ly away in all three — outside the 25 ly supply range
in all three. A ninetyfold denser sky is ninety times more to settle, not a
rival on your doorstep, and the core's first war arrives on the same schedule as
the rim's. The region text used to say the core put your neighbours on top of
you from the first week; that was star spacing wearing a rival's coat.

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

Because upkeep is a standing bill, ending one has to be a decision you can make:
`scrap` breaks a fleet up at the colony it is parked over and returns a third of
its build cost to that warehouse. Without it the only way to stop paying for a
hull with no purpose — a colony ship that has landed its pod, say — was to let
its crew desert, which returns nothing and is something that happens *to* you.

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

A route also carries a **backhaul**: on the way home the freighter brings the
outpost's surplus ore to the world that can refine it. That matters more than it
sounds, because refining is *local* — enrichment wants uranium and fuel in one
warehouse, electronics wants copper, rare earths and silicon together — and the
worlds with the geology are exactly the ones with no industry. It comes home no
fuller than it went out, so ore never crowds out the supplies the route exists to
deliver, and the hold is shared between ores in proportion to what is spare.

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

`galaxysim/engine/intents.py` is the entire write surface. Players, the CLI and
the AI all go through it — the AI has no privileged path into the simulation.

That constraint earns its keep for a narrower reason than it used to claim. This
is a single-player game, so "solo must match multiplayer" is not the argument.
The argument is that **the soak is the only instrument for telling whether the
economy works**: every economic failure this project has found — a fuel famine
that emptied every fleet on day twenty-six, a colony pod that cost nothing at
all, a sixty-day wall in front of expansion — surfaced by watching AI civs run
under exactly the constraints a player faces. Subsidise them and the soak stops
measuring the game and starts measuring the subsidy.

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
4. **Siege** — what is left in orbit after the shooting decides whose supply
   lines run, and whether a colony changes hands
5. **Governor** — governed colonies decide labor before production reads it
6. **Production** — life support, then work, then construction
7. **Research** — spends what production just banked
8. **Terraform** — after production, so a project draws on the industry made
   this tick; before colonization, so a world finished this tick is settled as
   the world it has become
9. **Colonization** — last, so a contested world is settled by whoever holds it

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
- **Diplomacy is not simulated.** A standing attack order is the entire
  mechanical surface of "we are at war" — there is nothing to negotiate, ally
  with or surrender to. In a shared universe players would do that among
  themselves; in a solo game it means a war ends when somebody stops fighting.
- **Species descriptions are stored but unused.** The extraction pipeline is
  step 6.
- **Names are syllable soup.** Step 7 gives each civ phoneme banks derived from
  its own species profile.

## Pacing

`galaxysim soak --ai 8 --days 28` is the check, and the property it tests is the
**shape of the curve** rather than any figure in it: colony count should still
be climbing at day 28, with what slows a civilization being the cost of what it
has already built.

That is an inversion of what this file used to say. "Slow and
compounding-resistant" produced exactly what it asked for — AI empires that
reached six colonies on day two and sat there for twenty-six days. Growth is
meant to compound, and a flat line is the failure rather than the target.

Median colonies per AI civ, 8 civs over 28 simulated days:

```
day     4   8  12  16  20  24  28  32  40  48  60
      ────────────────────────────────────────────
        2   3   4   4   6   6   7   8  10  12  14
```

Still rising at the end, which is the point.

That curve moved once, and the way it moved is worth recording. Fixing the
construction orders that never closed — see below — put an extra world on the
board through the middle of the run (day 28 went 6 → 7, about 4.7 days per world
against 5.1) and then **converged**: 14 colonies at day 60 against 13 before. So
the effect of letting every governor build again is one world earlier rather than
a faster game, which is the right shape. It was tempting to price a colony pod up
by ten percent and put day 28 back to six, and that would have been the exact
mistake this file keeps documenting: hiding a fixed economy behind a constant. Every version of this before the
prices were real went flat inside a fortnight, and each time the cause was a
number that had stopped meaning anything rather than a civilization running out
of room — a colony pod, most recently, which was an integer nobody charged for,
so the pace of the whole game was accidentally the build time of a gunboat.

## How hard the opponents are

Difficulty is made of three things, and each is something a player could also
have or do. Never free materials, never hidden information — and that is a test
(`tests/test_ai.py`) rather than a comment.

| | Dormant | **Steady** | Driven | Relentless |
|---|---|---|---|---|
| re-plans every | 24 h | **2 h** | 1 h | 1 h |
| industrial worlds | 1 | **1** | 3 | 6 |
| settles by | nearest | **nearest** | best | best |
| hulls in build | 1 | **1** | 2 | 4 |
| upkeep reserve | 7 d | **3 d** | 2 d | 1 d |
| seated in | core | **arm** | arm | rim |

```bash
.venv/bin/galaxysim new Frontier --difficulty relentless
.venv/bin/galaxysim soak --ai 8 --days 60 --difficulty driven
```

**Attention** — how often it re-plans — is the truest axis in an asynchronous
game, because it is exactly what differs between human opponents: how often they
check in. Authored in *hours*, never ticks; per-tick decisions meant an opponent
in a five-minute universe thought twelve times as often as one in an hourly
universe, which was true for most of this project's life.

**Competence** is how well it plays: how many worlds it industrialises, whether
it settles the nearest rock or the one carrying what its economy is short of, how
deep a build queue it keeps, how early it commits to terraforming.

**Circumstance** is the galaxy itself, and it is a real difficulty axis rather
than scenery — though not the one it was written to be. Region does not move your
neighbours: they sit 48–51 ly away in all three. What it moves is **supply**. A
core empire's routes are a couple of light-years end to end and it never misses
a fleet payment; a rim empire's are three times as long and it misses tens of
thousands. So the core is the forgiving sky, the rim is the demanding one, and
the ladder seats them that way — it used to run backwards, on the strength of a
rival proximity that was never there. A property of the sky rather than a gift,
since the player lives under the same one.

That cuts both ways, and it is worth knowing before you pick Relentless. At 28
days it settles a world every 5.6 days against Driven's 5.1 and ends with much
the same population — the rim slows *it* down too. What it keeps is the bigger
navy. So the top of the ladder is harder because it plays better and because the
galaxy is less forgiving to you both, not because it out-expands you.

There is a fifth dial, and it is the sharpest one because it is the only one
that can take something away from you: **whether the opponent goes to war at
all.** Dormant and Steady never do. Driven will blockade and annex your frontier
the moment it has ships to spare, and Relentless does it with more. Nothing
about the mechanic differs between them — the same siege, the same colony pod,
the same supply problem — only the willingness to spend a fleet on it. Steady
staying peaceful is deliberate: every price in the game is calibrated against a
soak of Steady opponents, and one that starts annexing its neighbours is
measuring something else.

A war it starts is one it keeps feeding. Nothing shoots at a blockade — what
ends one is upkeep, since a fleet draws on colonies near it and there are none in
your space — so a cordon quietly deserts away, and the opponent tops it back up
to the strength it thinks the objective is worth. It will not strip its home
systems to do it: it keeps a raid's worth free at home, the same reserve it
demanded before declaring. Over 120 days at eight Driven civs that means eight
sieges rather than fifteen, five abandoned rather than twelve, and the same six
worlds taken. Beating the first wave no longer wins the war.

Measured across a 120-day run of eight Driven civs:

```
day        10    30    60    90   120
colonies   23    49    95   138   171
worlds ≥ 0.4 habitability:  8 → 37
population:              88 B → 510 B
buildings finished over the run:      1,957
terraforming projects finished:         365
```

Population is the interesting column. It was frozen near 11.37 B per civ in
every run at every setting for most of this project's life, and what moved it
was not shipyards — it was worlds becoming places people can live. A terraformed
world holds billions where a dead one holds a few hundred thousand. The
compounding turns on habitability, which contradicted the expectation going in.

**This file used to add "and once one converts it becomes the industry that
converts its neighbours", and that part was simply false.** A converted world
became twenty-five billion people and six factories — measured, the single most
populous world in a 60-day run had six industry levels against a capital's two
thousand, and sat on eleven million tonnes of iron it could not turn into steel.
Both causes are fixed below, and the same worlds now reach twenty-six to
forty-eight levels: no colony over five billion people is still in single digits,
where three of the top eight were. Finished buildings across a 120-day run went
from 625 to 1,957.

**An empire is its capital, and then slowly stops being.** `galaxysim empire`
exists to show this and it is the sharpest number in the game. A homeworld is
*seeded* at 55% of its world's ceiling by `bootstrap._prepare_homeworld` —
around two thousand industry levels — while everything settled or terraformed
afterwards starts at zero. At day 30 the capital is **99.975%** of its
civilization's industry: the next largest colony, with 135 million people,
makes 1,410 units an hour against the capital's 10.2 million. That is not the
readout rounding. It is a factor of seven thousand.

Then it turns. The capital's share goes **100% → 91% → 83%** across days 30, 60
and 120 as terraformed worlds come online, so expansion does eventually buy
industry — it just buys population for two months first, and the compounding
starts on the far side of the first converted world.

That shape is deliberate now rather than merely observed. `STARTING_DEVELOPMENT`
is the lever, and lowering it was measured over 120 days:

```
opening development     0.55     0.35     0.20
capital share, day 60    91%      97%     100%
capital share, day 120   83%      73%      75%
colonies, day 120        171      139       92
worlds ≥ 0.4 hab          37       32       25
population            510 B    454 B    366 B
```

Weakening the opening does not decentralise the empire, it shrinks it. The
capital is what builds the colony pods, so taking industry off it costs 46% of
the colonies and 28% of the population by day 120 and buys eight points of
capital share — and at 0.20 the share is *worse* at day 120 than at 0.35, since
the empire never got large enough to convert worlds. **0.55 stays**, and the
opening being a one-world economy is the intended arc rather than a number
waiting to be tuned.

## Known tuning gaps

- **Ships take about twice as long to build.** Refining takes half the industry
  pool, so construction runs at half the rate it did. That is the intended
  shape — ore has to become steel before it can become a hull — but the split is
  a first-pass number that wants a play session, not a spreadsheet.
- **A navy is not built down, it is starved down, and nothing yet says why the
  famine comes.** The build rule works beautifully: per civilization, per
  fortnight, warship strength sits within a point of `garrison_per_colony ×
  colonies` for as long as nothing goes wrong. Then it falls off a cliff and
  climbs back. One civilization, its whole run:

  ```
  day        14   28   42   56   70   84    98   112   120
  warships  9.0 19.0 29.0 39.0 49.0 59.0  63.6  12.2  28.2
  the line 10.0 20.0 30.0 40.0 50.0 60.0  70.0  80.0  85.0
  deserted   —    —    —    —    —    —     —   60.4    —
  ```

  Five of eight civilizations had one of these, at different times, which is why
  the *median* looked like a decline after day 84 — four happened to be in a bust
  at once. They recover: one fell from 27.6 to 4.6 at day 70 and was back to 55
  by day 120.

  Accounted flow by flow, **desertion is the whole story.** Combat is nearly nil
  — 8.2 strength in one fortnight across the run's worst engagement — and
  scrapping was a second-order amplifier since removed (below). What destroys a
  navy is the upkeep bill going unpaid for days: upkeep is fuel and alloys, fuel
  is synthesised from ice and carbon and competes with the fissile chain, and
  when it runs short every crew in the fleet deserts at once.

  **The famine is a distribution failure, and it cannot be shipped away.**
  Instrumented at the moment of every shortfall across 120 days:

  | short of | times | mean held by the civ | mean within supply range |
  |---|---|---|---|
  | alloys | 4,730 | **2,344,773,396 t** | **2 t** |
  | fuel | 4,901 | **29,473,728 t** | **1 t** |

  Not one of those was a production failure. The material existed by the billion
  tonne; it was in the wrong warehouse, and no amount of it in the wrong
  warehouse pays a crew. Fleets averaged 3.5 colonies inside supply range — the
  suppliers were *there*, holding none of the two things upkeep is billed in,
  because **nothing in this game ever moves fuel or alloys**. Routes carry water,
  food and fertiliser out; the backhaul carries ore in, deliberately.

  Nor can that be fixed by shipping, which is the part worth knowing. A point of
  strength burns **3,000 t/hour**. A route ship holds 28,000 and takes 140 hours
  to go and come back, so it delivers **200 t/hour** and costs **1,500** in
  upkeep of its own. One strength-2 hull would need **thirty freighters**, each
  consuming seven times what it carries. So this is not a logistics gap: **a
  fleet lives where industry is, or it does not live**, and the open question is
  whether the fuel share of upkeep — 1,100 of 3,000 tonnes — is priced where it
  was meant to be. That is a deliberate decision, not another patch.

  The garrison itself is a live cap, not a dead number: `_maybe_build` reached
  the warship test 367 times and the cap turned it away 284 of them. But it is a
  minor constraint next to the queue — **85% of all 23,040 build decisions end at
  `build_queue_depth`** before garrisons are asked about, and the run laid down
  179 settlers against 83 warships.
- **A capital cannot generate what it demands, and that is now a real question
  rather than a bug.** Two defects were hiding this: governors could not build at
  all, and the brownout rule abandoned any plant it could not immediately afford.
  Both are fixed, three of the eight capitals recovered on their own — and four
  still sit between 0.56 and 0.77 power at day 60. Demand scales with industrial
  *output*, which grows with every level of every industry; generation grows only
  with levels of plant, and the quadratic cost curve bites hardest exactly where
  demand is highest. So a developed capital has to build its grid several times
  deeper than the rest of its stack. That may well be the intended pressure — it
  is what makes power a decision — but the number has never been chosen on
  purpose, and it wants a play session rather than another sweep.
- **What is left of the tail is one big read.** After loading the order queue and
  the fleets once a tick (below), the largest single row source is
  `charted_systems` — **848 rows a tick at 120 days**, more than everything else
  put together — because the AI walks every charted system with its worlds and
  owners attached to decide where to settle and scout. It is already read once
  and shared across all eight opponents, so this is not repetition; it is one
  query whose result grows with the charted galaxy. Making it smaller means
  changing *what the AI looks at*, not how often, which is a design change rather
  than a hoist.

## What was on this list and is not any more

Kept because the fixes are the most useful thing in the file: each was a number
or a proxy that had stopped meaning anything, and none of them looked like a bug
from the inside.

- **Region did nothing, and now it is the difficulty axis it was pretending to
  be.** This entry used to say the core plays exactly like the arm. That was
  measured at 28 days on the pace metric, which cannot see what differs. Run to
  60 days at 8 driven civs, two seeds:

  ```
                              core     arm      rim
  supply route length (median) 2.4/2.0  3.6/3.0  7.3/5.6  ly
  routes the empire needs       38/51    56/73    68/87
  upkeep shortfall events          0/0   54/730  28,791/27,907
  ore yield of settled worlds  4.1/4.2  3.8/3.8  3.6/3.3
  terraforming completed      170/234  176/232  109/154
  ```

  **Density pays through supply**, and enormously: a core route is a third the
  length of a rim one, the core needs 40% fewer of them, and a core civilization
  never misses a fleet payment while a rim one misses tens of thousands.

  Two things that looked like channels are not. Colonies within supply range of
  each other run **15 / 14 / 13** regardless of region — the settlement frontier
  is a fixed volume, so star density does not change how far apart an empire's
  own worlds end up, which is why the core terraforms no faster than the arm. And
  "density buys reach" cannot work either: a colony pod costs 450 million units
  of work against an expedition's 317,000 tonnes of materials, so pricing the
  crossing would move 1.5% of a decision whose real cost is yard time.

  **The decision was to leave the rim uncompensated and say so.** It is the hard
  start; the core is the easy one; the arm is the calibrated middle. The three
  region descriptions now say that, and the difficulty ladder — which seated
  Dormant on the rim and Relentless in the core, on the strength of a rival
  proximity that Phase 22 measured as 48–51 ly in *all three* regions — was
  turned the right way round. Steady and Driven stay in the arm, because every
  price in the game is calibrated there. `tests/test_ai.py` asserts the ordering
  so it cannot invert again.

  Population does *not* separate the regions — the arm leads on one seed, the
  core on the other — so any claim that one is stronger overall is seed noise at
  this sample size. An earlier draft made exactly that claim before a second seed
  caught it.

- **Eighteen resolvers each asked the database for the orders.** A tick reads the
  intent queue from movement, logistics, combat, siege, the governor, production
  five times over, research, terraforming and colonization — and each one asked
  separately, some of them once per civilization. Measured at 120 days that was
  **32 statements a tick** against a table whose active rows are a standing route
  for every colony and an attack order for every war, plus **3.8 full scans of
  the fleet table** to look at 175 fleets. The AI did the same thing one level
  up: two decisions selected the whole galaxy's orders of a kind and then
  filtered to themselves, once per opponent.

  All of it now loads once a tick and is handed out in slices, through the memo
  the context already had for colonies and star charts.

  What makes that safe rather than merely fast is that **the status filter is
  applied per call, not baked into the memo**. Resolvers finish each other's
  orders all through a tick, and those are the same objects, so re-checking on
  the way out sees a cancellation without a round trip. The one thing a memo
  cannot see is an order that did not exist when it was built — the governor
  queues construction one stage before production reads it — so that one gives
  the memo up when it queues. Both rules are tests, and both were checked by
  breaking them.

  | | before | after |
  |---|---|---|
  | SELECTs per tick (4 civs × 20 colonies) | 26.2 | **10.2** |
  | statements per tick | 57 | 41 |
  | rows loaded per tick at 120 days | 2,209 | **1,611** |
  | 120-day soak | 212 ms/tick | **180–186** |

  A 120-day run is byte-identical afterwards — same colonies, same population to
  the unit — which is the only thing that proves a stale cache did not quietly
  change the game. The absolute round-trip ceiling was ratcheted from 120 to 60
  in the same breath, because a guard rail left at three times the real number is
  a guard rail that lets all of this leak back.

- **The AI asked whether it could afford a hull by counting warehouses its
  fleets could not reach.** `_can_carry_more_upkeep` is the brake on a navy's
  size, and it summed the upkeep materials across **every colony the civ owned** —
  while the bill it was predicting is charged only from colonies within supply
  range of each fleet. The same proxy-for-the-real-thing this file keeps finding,
  one dimension over: the function's own docstring had already learned that a
  civ can hold a hundred million tonnes of steel and no fuel, and then measured
  the fuel everywhere instead of where it was owed.

  It now asks the local question — these warehouses, against everything already
  drawing on them — which is the rule the biller itself uses.

  **Honest about the size of it:** almost nothing. Over 120 days at eight driven
  opponents it changed one civilization's fleet by about a point of strength and
  left the other seven byte-identical, because the build queue refuses 85% of
  decisions before affordability is ever asked, and because the collapses happen
  when everything still looks affordable — the fleet sits at 59 against a line of
  60 with `upkeep_paid` at 1.00 the fortnight it loses two thirds of itself. A
  first attempt banked the *union* of every fleet's neighbourhood, which for a
  spread-out empire is very nearly the whole empire; that one changed nothing at
  all. Worth fixing because the question was wrong, not because the answer moved.

- **A bad week cost a civilization its navy.** Scrapping had two triggers:
  strength above the garrison, and *insolvency*. The second had a good argument
  — a hull you cannot pay for is lost either way, and scrapping returns a third
  of the materials while desertion returns nothing — and measured over 120 days
  it was a liquidation spiral. Any shortfall at all, however small, made every
  docked warship a candidate at one hull per decision, which for a driven
  opponent is hourly. One civilization shed **45.6 strength to desertion and
  sixteen hulls to scrapping inside a fortnight**, then rebuilt twelve points of
  it eight days later — the shortage was weather, and it had sold the fleet.

  The salvage does not even answer the shortage. Upkeep is **fuel and alloys**;
  breaking a hull returns **alloys, steel and electronics**. A civ short of fuel
  sells its navy and is still short of fuel, having paid a third of build cost
  for the privilege. Surplus is the only trigger now.

  It is not free: the hulls that used to be scrapped now desert instead, so the
  desertion column rises. It is still a large net gain, because desertion is
  proportional and scrapping was not — aggregate warship strength at day 120 goes
  **239 → 281**, and the two worst-hit civilizations end at **29.3 and 30.1
  instead of 4.7 and 18.1**.

  A navy that survives its bad weeks is a navy that finishes sieges. Over 150
  days at eight driven opponents, old rule against new:

  | | before | after |
  |---|---|---|
  | wars declared | 6 | **9** |
  | blockades established | 13 | 14 |
  | worlds subdued | 10 | **13** |
  | blockades abandoned | 7 | **5** |
  | **worlds captured** | **10** | **14** |

  Population is unmoved — 97.553 B against 97.542 B median — so this is the same
  economy fighting harder rather than a different one. **At 120 days the same
  comparison read 6 captures against 4 and looked like a regression**; the run
  simply ended mid-campaign, with two wars still standing and three subdued
  worlds not yet taken. Worth recording, because a soak cut off at the wrong day
  argues the opposite of the truth.

- **A war was one raid, and everything after it sat at home.** The AI dispatched
  every ship it would ever send at the moment war was declared, and `_maybe_raid`
  refuses to declare while an attack order stands — so nothing in the AI ever
  sent a warship at an enemy twice. Win the opening engagement and the war was
  over. Worse, `_maybe_scrap` counts strength above the garrison as surplus, so
  it broke up the very hulls that should have been the second wave.

  Two wrong diagnoses before the right one, each killed by measuring. This file
  said the raid gets *ground down* — nothing shoots at a blockade; what kills one
  is upkeep, billed to colonies near a fleet, of which there are none in somebody
  else's space. So the first version topped a cordon up when the defenders
  outweighed it, and across 120 days that fired **zero times**: sampled daily
  over every war and every colony of its target, the cordon was winning 28 times,
  crossing 18, outweighed never — the AI only picks worlds it already outweighs,
  and a frontier outpost has no fleet at all. The second version topped a worn
  cordon back up to `raid_strength` and *also* fired zero times, because it was
  gated on the doctrine garrison, which a developed civ is permanently under (the
  gap bullet above). Instrumented, it bailed on **708 of the 730 turns it ran**.

  What it does now is spend by the raid's own standard — keep a raid's worth free
  at home, commit the rest — because there must be one answer to what a
  civilization will spend on a war rather than two. Over 120 days at eight driven
  opponents that halves the number of sieges and finishes them instead: blockades
  **15 → 8**, lifted before finishing **12 → 5**, worlds subdued 8 → 7, and the
  **same six worlds captured** off six wars instead of nine. Fewer sieges,
  heavier ones, and they finish.

- **A supply route shipped a quantity, not a shortfall.** A standing route
  carried its full manifest every trip whether the far end was empty or
  drowning — the one thing a supply run exists to respond to was the one thing
  it could not see. This file used to say the consequence was outposts banking
  months of surplus; measured, it was the opposite. A round trip took **140
  hours**, the manifest was 12,000 tonnes of water, and a typical destination
  drank 210 an hour, so a route replaced about **40%** of what its world
  consumed between visits, and one outpost sat at zero after eighteen
  deliveries. A manifest is now the **level to keep** at the destination, and
  the AI sizes it from what that colony actually drinks rather than a constant.
  Both halves were needed: a level frozen at the moment the order was placed is
  a landing party's ration, and an outpost quadruples within weeks, so the
  target is refreshed as the world grows. Import-dependent destinations under a
  day of water went **3 → 1 of 63, and none are at zero**.
- **The best world in the game had six factories.** A governor stopped walking
  its build order at the first entry it could not pay for — right while a colony
  is *accumulating*, and nothing checked whether it was. A terraformed garden of
  24.8 billion people with room for 7,122 industry levels had six, waiting on a
  factory priced at 1.62 million tonnes of steel while holding 777 thousand, with
  a mine and a refinery at thirty thousand each going unbuilt every tick for two
  months. It could not earn its way out either: the industry build order had no
  refinery in it, so the one policy dedicated to being an empire's workshop was
  the only one that could never turn its own ore into the material every entry on
  its list is priced in. Same species as the power order above, one level up — a
  ranked list treated as a queue when its entries are not independent. Skipping
  what it cannot afford makes both paths one rule instead of two.
- **Every colony built one thing and then stopped, for ever.** Nothing completed
  a `build_structure` order — the resolver charged for it, laid the foundations,
  marked it in progress, and that was the last thing that ever happened to it.
  The governor decides whether to build by asking whether an order is
  outstanding, so one order that never closed froze a colony's development
  permanently. Eighty-five colonies produced **sixteen** buildings in sixty days,
  all sixteen orders still open with their buildings long finished; the first
  capital's fusion plant was ordered on tick 1, completed on tick 2, and was
  still holding the only build slot fourteen hundred ticks later. That is why
  every homeworld in every soak ran at half power for the whole game: it could
  never build another reactor. Sixteen buildings became **144**, and three of
  the six brownout capitals came back to full power on their own.
- **The brownout rule abandoned the plant it could afford.** Below the power
  threshold a governor drops its policy and builds generation, walking
  `fusion → fission → geothermal → solar` and stopping at the first thing it
  cannot pay for. That stop is right for a *policy* order, where the entries do
  different jobs and saving up for the mine you want beats building the cheapest
  shed. It is wrong here: those four are four ways to buy the same commodity, so
  stopping at the dearest one leaves a colony in the dark beside a plant it could
  pay for today. Every one of the four capitals still browning out could afford a
  geothermal plant and was stopping at a fusion plant twenty levels deep. A
  preference standing in for a set of alternatives — the same species of bug as
  the rest of this list.
- **Half the event log was one sentence.** A brownout is a condition that lasts,
  and it was logged every tick it lasted — 8,506 of 19,907 events in a sixty-day
  soak were eight homeworlds each writing "still at 50% power" fourteen hundred
  times, burying the terraform completions and life-support failures underneath.
  The log is what an offline player reads to find out what happened, so a
  standing state is not news; the *edges* are, exactly as a blockade already gave
  them a "cut" and a "reopened". Fourteen power events over sixty days now, and
  the most common thing in the log is a ship arriving somewhere.
- **Power depended on the tick rate.** The demand side mixed per-tick industrial
  figures with per-hour life support; the generation side mixed a per-tick
  baseline with per-hour sunlight, ground heat and reactors. At the hourly
  cadence everything here runs at, the two conventions coincide and the mixture
  is invisible — the same world was at 0.496 power hourly, 1.000 at fifteen
  minutes and 0.415 at five. Since power multiplies industry, extraction,
  refining, construction, shipbuilding and terraforming, the cadence was setting
  the pace of the entire economy, which is the one thing this design forbids.
  The pace guard did not catch it because its fixture is a fresh homeworld, and
  a fresh homeworld never browns out.
- **"Terraforming has never once run in an actual game."** It runs 370 projects
  in 120 days now and converts 21 worlds past 0.4 habitability, twelve of them
  to 1.000 — a dead rock becoming a world of twenty billion people. Three things
  were wrong at once: the ladder dead-ended on two thirds of worlds, projects
  were priced in materials the economy cannot make, and an order nobody could
  pay for held a campaign slot for ever so a civilization could be frozen out of
  a world it could have afforded that afternoon.
- **"Almost every colony is a dead rock, and that is the ceiling on
  everything."** It was, and terraforming was the way out of all of it at once.
  Population over 120 days went from 156 B to 561 B once worlds started
  converting, because a terraformed world holds billions where a dead one holds
  a few hundred thousand.
- **"Conflict has no resolution."** Blockade, siege and capture all run, and a
  world changed hands unprompted in a soak for the first time at day 107.
- **Neighbours were unreachable.** Civilizations were seated 113–141 ly apart
  against a 25 ly supply reach, and expansion does not close it — an empire's
  radius grows to about 13 ly and *stops*, because it settles the nearest world
  and fills in its own neighbourhood. The frontier was being sized for a hundred
  players however few were actually seated.
- **The fleet's late knock.** Fuel is paid in full through a 120-day run now
  and fleet strength quadruples over it.

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
