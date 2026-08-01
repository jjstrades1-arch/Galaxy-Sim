# Galaxy-Sim

An async, persistent, text-based civilization simulation. Players colonize
procedurally generated worlds across a shared galaxy, research an open-ended
tech space, and build up civilizations over weeks of real time.

This repository is at **build-order step 1–3**: the core loop works end to end.
Procedural tech generation, lazy world generation and the species pipeline are
next. [`docs/DESIGN.md`](docs/DESIGN.md) is the architecture reference; the
notes below say what is deliberately placeholder.

## Try it

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

.venv/bin/galaxysim new Frontier --ai 3        # solo game, 3 AI opponents
.venv/bin/galaxysim status                     # your civ
.venv/bin/galaxysim systems                    # what's nearby
.venv/bin/galaxysim research                   # standing research programme
.venv/bin/galaxysim move 1 24                  # send fleet 1 to system 24
.venv/bin/galaxysim colonize 1 60              # settle world 60 when it lands
.venv/bin/galaxysim tick 24                    # advance a day
.venv/bin/galaxysim log                        # what happened
```

`galaxysim soak --ai 8 --days 28` runs a throwaway universe of AI civs and
charts how fast they grow. It is the pacing check — if a civ reads as "big"
inside a few simulated days, the cost curves need steepening.

Saves default to `./galaxysim.db`; set `GALAXYSIM_DB` to move it.

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

Three curves keep growth compounding-resistant, all superlinear:

| Curve | Where | What it stops |
|---|---|---|
| Research cost by depth | `Rates.research_cost` | A lineage running away |
| Colony overhead by count | `Rates.colony_overhead` | Sprawl paying for itself |
| Colony price by count | `Rates.colony_cost_multiplier` | Claiming worlds too fast |

Plus fleet upkeep (`FLEET_UPKEEP_PER_STRENGTH`), so a navy is a standing bill
rather than a one-time purchase.

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
2. **Combat** — a fleet that arrived this tick is immediately at risk
3. **Production** — survivors produce; casualties do not
4. **Research** — spends what production just banked
5. **Colonization** — last, so a contested world is settled by whoever holds it

## Layout

```
galaxysim/
  core/        seeds, RNG derivation, continuous-space coordinates, resources
  model/       SQLAlchemy entities (SQLite for solo, Postgres for shared)
  worldgen/    world type table; lazy system_at() lands in step 4
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

- **Resources outrun their sinks.** With one colony and nothing worth buying,
  stockpiles climb into the thousands. Real sinks (infrastructure, terraforming,
  tech costs in resources rather than research points) arrive with the deeper
  economy; tuning the numbers before those exist would be guesswork.
- **Tick cost is ~60 ms with 8 AI civs**, most of it AI decision-making rather
  than resolution. Real players do not think, so this is not yet the 100-player
  number — but it wants profiling before the shared universe is real.

## Tests

```bash
.venv/bin/python -m pytest
```

The load-bearing ones are determinism (`tests/test_determinism.py`) and pace
invariance (`tests/test_pacing.py`). The rest of the suite can be wrong and the
game still works; if those are wrong, nothing else matters.
