# Galaxy-Sim — Architecture Plan (v1)

> The approved architecture plan, kept in the repo as the reference for what is
> being built and why. Written before any code existed; the "Still open"
> section at the end is the live list. Build-order steps 1–3 are now
> implemented — see the README for what is placeholder.

## Context

This plan turns the project spec plus its amendments into a concrete
architecture, and defines what the first build session produces.

Your amendments materially change the spec in five places, and two of them are
in tension with it:

1. **Lazy world generation** — systems generate on first visit, not upfront.
2. **Tech starts at lightspeed travel** — no pre-FTL tech exists; the game opens
   at interstellar scale.
3. **Solo mode with N configurable AIs.**
4. **Species defined by free-text description**, which drives tech generation.
5. **Infinite tech, no tiers** — this contradicts spec §5.1 (tiered magnitude
   budgets) and §5.2 (fixed skeleton graph). The skeleton is gone.

The hard problem this plan solves: **infinite, tierless, player-seeded tech that
is still fair across 100+ players.** The rest follows from that.

**Decisions locked in this session:** open rule design over rigid systems; LLM at
civ creation only; Python; local CLI first. Two I chose on your behalf and you
should revisit if you disagree: a *hard* power-budget invariant, and deterministic
combat with seeded variance.

---

## 1. The core mechanism: tech as a generative lineage

The spec's fixed skeleton graph is replaced. There is no graph to enumerate —
tech is generated on demand at the edge of what a civ already knows.

**Genome.** Every tech carries:
- `domains: set[str]` — drawn from a fixed domain vocabulary (~15 terms:
  propulsion, energy, materials, biology, computation, gravitics, …).
- `concepts: set[str]` — an open bag of tags, freely mutable and extensible.
- `effect: EffectPayload` — from the bounded effect grammar (spec §5.1.2).
- `depth: float` — cumulative research cost paid along its lineage.

**Frontier.** A civ never sees a tree. It sees 4–8 *candidate* techs, generated
lazily from its known set. Researching one consumes it and regenerates the
frontier. This is what makes tech infinite: the space is generative and
combinatorial, never materialized.

**Derivation.** A candidate is derived from 1–2 known parents:
inherit domains (weighted by species affinity), drift/mutate concepts, then roll
an effect whose *type* is conditioned on the resulting domain+concept mix. A
2-parent derivation is a synthesis and biases toward structural effects
(`unlocks`, `new_resource_type`) rather than flat multipliers.

**Depth replaces tiers.** Magnitude budget is a continuous function of depth
(`budget = base * depth^alpha`), not a tier lookup. Nothing is gated by a tier
number; a civ that dumps everything into one lineage goes deep and narrow.

**Root.** A single seed tech, *Lightspeed Travel*, at depth 0. Every civ starts
holding it. All lineages descend from it.

### Fairness without tiers

Enforced as an invariant at generation time rather than a reroll/validator pass:

> A civ's total effective power is a monotone function of total research paid.

Generation rolls **shape** (which domain, which stat, what kind of effect) but
never **net strength**. Two civs at equal spend are equally strong and
differently shaped. Per-stat stacking uses the diminishing-returns curve from
spec §5.1.4 (`1 - Π(1-xᵢ)`) so deep single-stat lineages self-limit and lateral
expansion stays competitive. No population-average power scoring, no rerolls.

> Revisit if you want lucky lineages to actually be better — that's a soft
> budget with ±20–30% drift, and it compounds badly over hundreds of ticks
> against offline players.

---

## 2. Species description → civilization

The player writes a free-text species description. One LLM call at creation maps
it to a **schema-validated** structure — and only that. The LLM never sets a
number that touches balance.

```
SpeciesProfile:
  domain_affinities: dict[domain, weight]   # normalized to sum 1.0
  traits: list[TraitTag]                    # from a bounded pool
  phoneme_profile: PhonemeBank              # naming
  descriptor_vocab: list[str]               # flavor text
  seed: int
```

- **Affinities are normalized.** Strength in one domain is weakness in another.
  A description claiming excellence at everything yields a flat distribution —
  a generalist, not a god.
- **Traits are drawn from a bounded pool**, and are net-zero: each carries a
  paired drawback. The LLM selects which traits fit the description; the engine
  owns their magnitudes.
- **Everything downstream is deterministic** from `(profile, seed)`. Same
  description + seed ⇒ same civilization. No LLM in the tick loop.
- Rejected/unparseable output falls back to keyword matching so creation never
  hard-fails on an API error.

Affinities bias which candidates surface on the frontier — an aquatic hive-mind
sees biology/collective-computation lineages more often — without making any
individual tech stronger.

---

## 3. Lazy universe

Space is continuous coordinates. Star systems are gravitational clusters, not
movement gates (per your "not bound by systems" philosophy — travel cost is a
function of distance, not of system hops).

**Generation is a pure function**, not stored data:

```
system_at(universe_seed, sector_coords) -> SystemDescriptor
```

A deterministic hash of `(universe_seed, coords)` seeds the RNG for that
sector's stars, worlds, types, and stats. Unvisited space costs nothing to store
and every client agrees on it. A row is **persisted only on first visit or
start**, at which point it gains mutable state (ownership, depletion, colonies).
Scanning at range yields a lower-fidelity descriptor derived from the same seed —
consistent with what you find on arrival.

---

## 4. Tick engine

Server-authoritative, intent-queue, per spec §3.1. Resolution order per tick:

1. Snapshot state, derive `tick_seed = H(universe_seed, tick_number)`.
2. Resolve movement → arrivals trigger lazy generation + persistence.
3. Resolve combat at co-located coordinates.
4. Resolve production/economy (runs for offline players — spec §3.1).
5. Resolve research; regenerate frontiers for civs that completed a tech.
6. Resolve colonization.
7. Commit atomically; emit a per-civ event log (the player's "what happened").

Every random draw derives from `tick_seed`, so a tick is **replayable** — you can
re-run it and get the identical result. That matters for trusting outcomes you
slept through, and for debugging.

**Combat**: deterministic formula over fleet stats and tech effects, with
variance seeded from the tick seed. Auditable, reproducible, still uncertain.

**Cadence**: solo mode ticks on demand ("end turn"); multiplayer ticks on a
wall clock, configurable, default **5 minutes** (hourly also supported). Same
engine, different scheduler.

### Tick rate is granularity, not pace

These are two separate knobs and the engine must keep them separate:

- **Tick rate** = how often the world updates. Frequent (5 min) so async play
  feels responsive and a returning player sees fine-grained history rather than
  one giant jump.
- **Progression rate** = how long a civilization takes to grow. Slow. Deliberately.

All economy, research, and construction rates are authored as **per-real-hour**
quantities in config and divided by `ticks_per_hour` at resolution time.
Consequence: changing the cadence from 5 min to hourly changes only the
resolution of the log — nobody grows faster or slower. Balance is authored
against wall-clock time, never against tick count.

Anti-snowball, so there is no get-big-quick path:
- Research depth cost is **superlinear** in depth — each step deeper along a
  lineage costs meaningfully more than the last.
- Colony count carries superlinear overhead (logistics/admin upkeep), so wide
  expansion pays for itself only with matching infrastructure investment.
- Per-stat diminishing returns (§1) already cap what deep-and-narrow buys.

Target pacing for the first balance pass, to be tuned in the soak runs (§7):
a competitive multi-system civilization is **weeks** of real time, not days.

**Performance budget**: 5-minute ticks means ~288 ticks/day. At 100+ players a
tick must resolve in low seconds. This is why worldgen is pure-functional and
persisted only on visit — the tick loop must never touch unvisited space.

**Solo mode**: N AI civs, configurable count. An AI is an intent generator
implementing the *same* intent API a player uses — no privileged access. This
keeps solo mode honest and doubles as load-generation for multiplayer testing.

---

## 5. Stack & layout

Python 3.12 · SQLAlchemy 2.x · Postgres (SQLite for solo/dev — same models) ·
Typer + Rich CLI · pytest.

```
galaxysim/
  core/        seeds, RNG derivation, coordinates, distance
  model/       SQLAlchemy models + migrations
  worldgen/    system_at(), world types + stat ranges
  tech/        genome, effect grammar, frontier generation, budgets
  species/     LLM profile extraction, schema validation, fallback
  flavor/      phoneme banks, templates, description generation
  engine/      tick pipeline, intent queue, resolvers (economy/combat/colonize)
  ai/          AI intent generators
  cli/         Typer app, Rich rendering
  api/         (deferred — added when multiplayer starts)
```

---

## 6. Build order

Reordered from spec §6 because solo mode moves the playable milestone earlier.

1. **Tick + intent loop** — 2 civs, hardcoded systems, no procgen, SQLite. Prove
   queue→resolve→persist. Tests first: a tick is deterministic and replayable.
2. **Data model** — universe/system/world/colony/civ/fleet/intent, real
   migrations, ownership and offline production correct.
3. **Deep systems** — economy, colonization, combat, with placeholder content.
   Playable solo vs. dumb AI. *This is the "is it fun" checkpoint — stop and
   evaluate here before layering generation on top.*
4. **Lazy worldgen** — `system_at()`, world types, persist-on-visit.
5. **Tech generation** — genome, effect grammar, frontier, depth budgets,
   stacking curve. Lightspeed root.
6. **Species pipeline** — LLM extraction, schema validation, affinity biasing.
7. **Flavor** — naming and description generation across tech, worlds, species.
8. **Scale** — HTTP API, Postgres, auth, deployment (spec §7 — outside a coding
   session).

---

## 7. Verification

- `pytest` throughout; the load-bearing tests are:
  - **Determinism**: same seed + same intents ⇒ byte-identical state. Re-running
    a tick reproduces it exactly.
  - **Balance invariant**: simulate 200 civs to depth 100 with random research
    paths; assert total power spread stays inside tolerance vs. research spent.
    This is the test that proves infinite tech didn't break fairness.
  - **Worldgen purity**: `system_at()` returns identical output across processes;
    scan-at-range agrees with on-arrival.
  - **Pace invariance**: the same scenario run at 5-minute and at hourly cadence
    over the same simulated wall-clock duration produces equivalent civilization
    size. Proves tick rate is granularity, not speed.
- `galaxysim solo --ai 5` for hands-on play; `--ai 99 --auto-tick 500` as a
  headless soak run to surface runaway-civ and performance problems.
- **Pacing soak**: 100 AI civs over a simulated month, charting size and power
  over time. Target curve is slow and compounding-resistant — if a civ reads as
  "big" within a few simulated days, the cost curves need steepening. Same run
  records per-tick wall time against the low-seconds budget.

---

## 8. Still open

- Exact effect grammar vocabulary (settle while building step 5).
- Auth/account model (deferred to step 8).
- Whether depth should decay or plateau, if soak runs show late-game numbers
  becoming meaningless.
