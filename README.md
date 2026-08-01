# Galaxy-Sim

An async, persistent, text-based civilization simulation. Players colonize
procedurally generated worlds across a shared galaxy, research an open-ended
tech space, and build up civilizations over weeks of real time.

The core loop works end to end, and the **colony layer is deep**: colonies hold
their own stockpiles, assign their population to work, build structures in
limited slots, and keep themselves alive on hostile worlds. Procedural tech
generation, lazy world generation and the species pipeline are next.
[`docs/DESIGN.md`](docs/DESIGN.md) is the architecture reference; the notes
below say what is deliberately placeholder.

## Try it

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

.venv/bin/galaxysim new Frontier --ai 3        # solo game, 3 AI opponents
.venv/bin/galaxysim status                     # your civ
.venv/bin/galaxysim systems                    # what's nearby
.venv/bin/galaxysim planet 12                  # full planetary survey
.venv/bin/galaxysim colony 1                   # one colony in detail
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

## The colony

A colony is a place you run, not a number that goes up.

**Matter is local; knowledge is civ-wide.** There is no treasury. Each colony
holds its own stockpile, and ore mined on one world is on that world until a
ship carries it elsewhere. Ships are paid for by the yard building them, fleet
upkeep by the nearest colony to the fleet — so projecting force far from home
means feeding it out there. Research is the one exception: a discovery is known
everywhere the moment it is made.

**Population is assigned to work**, across extraction, industry, research and
life support. The fourth is the interesting one. Life-support load scales with
`1 - habitability`, so a hostile world takes a bite out of the workforce before
anyone mines anything. Habitability is not a growth cap, it is a tax on labor.

**Life support consumes water as well as people.** Sealed habitats need
consumable input; workers cannot make air out of nothing. A world with oceans
hands its colonists water for free. A dry world has two options — mine ice and
refine it, or import every drop — and a world with neither ice nor ocean has
only the second. This is what makes hostile worlds *supply-dependent* rather
than merely expensive, and geology plays against it on purpose:

> The best mining worlds are dry by definition. They are among the richest
> worlds in the game and they cannot keep anyone alive without a supply line.

**Buildings occupy limited per-world slots** and are paid for in industry-work,
so a colony with nobody in industry finishes nothing. Shipyards gate fleet
construction; spaceports gate cargo throughput; domes cut what life support is
*needed*; hydroponics cuts what meeting it *costs*.

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
| Colony overhead by count (superlinear) | `Rates.colony_overhead` | Sprawl paying for itself |
| Fleet upkeep | `FLEET_UPKEEP_PER_STRENGTH` | Hoarding a free navy |
| Life-support upkeep | `Rates.life_support_*` | Holding hostile worlds cheaply |
| Local stockpiles | `Colony.stockpile` | Funding expansion from one pooled purse |

The colony-count *price* multiplier that used to sit here is gone. Local
stockpiles and life-support upkeep brake expansion harder than a price curve
did, and stacking both would have been double-charging.

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
  worldgen/    stars, planetary physics, geology, biospheres, surveys
  colony/      labor sectors, building catalogue, expedition loadouts
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
- **The starting region is generated eagerly.** `bootstrap.seed_starting_region`
  is replaced in step 4 by `system_at(universe_seed, sector)`, a pure function
  that makes space infinite and materializes a system only when someone reaches
  it.
- **Species descriptions are stored but unused.** The extraction pipeline is
  step 6.
- **Names are syllable soup.** Step 7 gives each civ phoneme banks derived from
  its own species profile.

## Known tuning gaps

- **Expansion may now be too slow.** The colony layer swung the pacing hard: an
  AI civ that used to hold ~8 colonies after 28 simulated days now holds ~2, and
  reaches ~3 techs instead of ~5. That direction is intended — expedition
  loadouts cost real resources, they come from one colony's stockpile rather
  than a pooled purse, and governors spend the same stockpile on buildings — but
  the magnitude wants a play session to judge. Deliberately not tuned blind;
  this is the "is it fun" checkpoint the build order calls for.
- **Supply routes over-deliver.** A standing route ships its full manifest every
  trip whether or not the destination needs it, so a well-supplied outpost banks
  months of surplus. A route that tops up to a target level would be better.
- **Ships take about twice as long to build.** Refining now takes half the
  industry pool, so construction runs at half the rate it did. That is the
  intended shape — ore has to become steel before it can become a hull — but the
  split is a first-pass number that wants a play session, not a spreadsheet.
- **Raw materials still outrun their sinks.** Research consuming refined goods
  is a real sink and the draw limit stops one chain cornering the input, but a
  developed colony still accumulates ore faster than it processes it.
  Terraforming and infrastructure upgrades are the next sinks.
- **Tick cost is ~88 ms with 6 AI civs.** Caching derived colony state per tick
  brought this down from ~105 ms, but the colony layer made a tick meaningfully
  more expensive than the ~60 ms it was before. Wants profiling before the
  shared universe is real — the likely wins are batching the per-colony queries
  and not re-reading intents inside the construction loop.

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
