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
.venv/bin/galaxysim colony 1                   # one colony in detail
.venv/bin/galaxysim labor 1 --extraction 3 --industry 2 --life-support 1
.venv/bin/galaxysim structure                  # list buildings
.venv/bin/galaxysim structure 1 laboratory     # build one
.venv/bin/galaxysim govern 2 --policy survival # delegate a colony
.venv/bin/galaxysim move 1 24                  # send fleet 1 to system 24
.venv/bin/galaxysim colonize 1 60 --preview    # what would settling cost?
.venv/bin/galaxysim colonize 1 60 --stores 200 # send an expedition
.venv/bin/galaxysim route 2 --from 1 --to 3 --carry volatiles:50
.venv/bin/galaxysim tick 24                    # advance a day
.venv/bin/galaxysim log                        # what happened
```

`galaxysim soak --ai 8 --days 28` runs a throwaway universe of AI civs and
charts how fast they grow. It is the pacing check — if a civ reads as "big"
inside a few simulated days, the cost curves need steepening.

Saves default to `./galaxysim.db`; set `GALAXYSIM_DB` to move it.

## The colony

A colony is a place you run, not a number that goes up.

**Matter is local; knowledge is civ-wide.** There is no treasury. Each colony
holds its own stockpile, and metal mined on one world is on that world until a
ship carries it elsewhere. Ships are paid for by the yard building them, fleet
upkeep by the nearest colony to the fleet — so projecting force far from home
means feeding it out there. Research is the one exception: a discovery is known
everywhere the moment it is made.

**Population is assigned to work**, across extraction, industry, research and
life support. The fourth is the interesting one. Life-support load scales with
`1 - habitability`, so a hostile world takes a bite out of the workforce before
anyone mines anything. Habitability is not a growth cap, it is a tax on labor.

**Life support consumes volatiles as well as people.** Sealed habitats need
consumable input; workers cannot make air out of nothing. This is what makes
hostile worlds *supply-dependent* rather than merely expensive, and it plays
against the world table on purpose:

> Barren worlds yield metal and energy but **no volatiles**. They are among the
> richest worlds in the game and they cannot feed themselves. The best mining is
> on the worlds least able to keep anyone alive.

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
  core/        seeds, RNG derivation, continuous-space coordinates, resources
  model/       SQLAlchemy entities (SQLite for solo, Postgres for shared)
  worldgen/    world type table; lazy system_at() lands in step 4
  colony/      labor sectors, building catalogue, expedition loadouts
  engine/      rates, intent queue, tick pipeline, resolvers
  ai/          AI civs, driving the same intent API a player uses
  flavor/      name generation
  cli/         Typer + Rich terminal client
```

## What is placeholder

Named explicitly so nobody mistakes scaffolding for design:

- **Tech is a depth counter.** `Civ.techs_known` stands in for the generated
  lineage of step 5 — genomes, a bounded effect grammar, and a frontier of
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
- **Resources outrun their sinks.** Buildings help, but stockpiles still climb
  once a colony is developed. More sinks (infrastructure upgrades, terraforming,
  resource costs on research) arrive with the deeper economy.
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
