# `player_shapes/` — checked-in player-controller shape-variance corpus

Two permanent, checked-in `Player.luau` fixture shapes that the Phase-1 Gate-0
corpus test (`tests/test_player_shape_corpus.py`) asserts host-C-dominance
against. They are *inputs* the proven host-ordering primitives dominate — NOT
patterns any pipeline matches-and-rewrites. No runtime/pipeline code reads them.

The AC7 C-dominance test EXECUTES each fixture's REAL competing code (loads the
module under bus-backed mocks and calls its actual `Update`/`Rotate`/`Move`), so
fixture drift — or a fixture that stops writing — FAILS the test; it does not
re-prove a hand-coded surrogate.

## Provenance — two origin runs (2026-06-05)

> **Phase 5 note:** paradigm A (`movement_facet_lowering.py` + the camera-facet
> PLAYER path) has been DELETED. There is no longer an "A-HIT" / "A-MISS"
> distinction at conversion time — every CC-identified player is owned by
> paradigm C, and these fixtures' **native raw writes** are the competitor C
> dominates. The two shapes still differ in WHAT they natively write (below),
> which is why the C-dominance proof is per-fixture.

| fixture | origin run | native competing writes |
|---------|-----------|--------------------------|
| `dde248_player.luau` | the **cached** dde248 SimpleFPS transpile | raw `cam.CFrame =` + `humanoid:Move(` + `humanoid.Jump = true` |
| `cold3a59_player.luau` | the **cold / uncached** cold3a59 SimpleFPS re-transpile | raw `cam.CFrame =` + rig `:PivotTo` move (NO `Humanoid:Move`/`Jump`) |

The conversion `output/` directory is **git-ignored** — the raw `Player.luau`
artifacts these came from are NOT committed anywhere in the repo. These two
files are therefore the **permanent copies**; there is no auto-regeneration
path (the source artifacts can't be re-derived deterministically).

### `dde248_player.luau` — native CAMERA + MOVE + JUMP shape

Assembled VERBATIM from the inline fragments already in
`tests/test_movement_facet_lowering.py` (`_AWAKE`, `_REAL_ROTATE`, `_REAL_MOVE`,
`_HELPERS`) into a complete `local Player … return Player` module. Its native
raw output contains, all at once:
  - a direct `cam.CFrame = …` camera write (in `Rotate`),
  - a `humanoid:Move(` call (in `Move`),
  - a `humanoid.Jump = true` write (in `Move`, on Space),
  - `_axis(Enum.KeyCode.D, …)`-style helper-wrapped WASD.

**Load-bearing:** these native raw writes (camera + `Humanoid:Move` + `Jump`)
ARE the mid-pass competitor C must dominate. The C-dominance proof
(`test_player_corpus_dominance.py`) runs the native `Rotate`/`Move` inside the
real `_tick` brackets and proves C is the last writer of the camera CFrame, the
`Humanoid:Move`, and the `Jump` — i.e. dde248 carries the **CAMERA + MOVE +
JUMP** dominance proof.

### `cold3a59_player.luau` — native CAMERA shape (PivotTo move)

**Reconstructed** from the documented shape (design doc lines 31-33, 203):
  - `_getAxis`-wrapped WASD,
  - an `Update` that caches `GetMouseDelta`, consumed in a `Rotate` that writes
    the camera (`cam.CFrame =`),
  - a MOVE that drives `self.gameObject:PivotTo(...)` (rig pivot) — **NOT**
    `Humanoid:Move`.

The AC6 shape-fact guard enforces this A-miss contract WITHOUT a brittle
file-global negative substring: it requires a CAMERA write specifically
(`cam.CFrame` / `CurrentCamera`, not any `CFrame =`), `GetMouseDelta` cached in
the `Update` method body, and a rig `:PivotTo(` move; the "no `Humanoid:Move`"
invariant is checked SCOPED to the `Move` method body only, so it is not brittle
against an incidental `:Move(` elsewhere.

Historically this was the shape that abstained BOTH PR #182 player-binding
locators (provenance; the locators were removed in slice 2.6). Post-Phase-5 there
is no A to "miss" — the native camera write + rig `PivotTo` are simply the
production shape, and C owns the camera.

**Fidelity caveat (load-bearing to state):** because `output/` is git-ignored
and the original cold3a59 artifact is GONE, this fixture's fidelity to the
ORIGINAL lost artifact is **UNVERIFIABLE**. AC6 (the shape-fact guard in the
corpus test) guards only against post-hoc EDITS to *this* fixture drifting from
the generic shape — it does NOT and CANNOT guard against reconstruction error
vs the artifact that no longer exists. This is not a soundness problem for
Phase 1: the load-bearing claim Phase 1 proves with cold3a59 is the GENERIC
invariant **"a `PivotTo`-move + cam-write shape is C-dominated on the camera
surface"** — exact byte-level fidelity to the original cold3a59 is NOT
load-bearing.

**Load-bearing:** the native move is a rig `:PivotTo` (NOT `Humanoid:Move`), so
cold3a59 has NO native `Humanoid:Move`/`Jump` to dominate. Its C-dominance proof
is **CAMERA-surface only** (C is the last writer of the native `cam.CFrame =`);
the rig `PivotTo` is benign/vestigial drift (present but NOT contesting the
camera), deferred to Phase 3 U1. The move/jump dominance proof rides dde248.

## Drift guard

If a future transpiler change makes the *real* output violate the
shape-fact guard's POSITIVE invariants (AC6 in `test_player_shape_corpus.py`),
the regression is in the converter, not these fixtures — re-capture the shape
consciously from a fresh conversion and update the fixture + this README. The
guard does NOT silently auto-update.

When re-capturing a fixture, also update the AC7 luau-sim seed fields: the
C-dominance test hand-mirrors the `self.*` fields the fixture reads
(`camRotation`/`camRotationX`, `minAngle`, `maxAngle`, `moveDirection`,
`sensitivity`, `speed`, `control`, `pendingMouse`). A re-captured shape that
reads a NEW `self.*` field needs a matching seed in the AC7 scenario, or the
fixture errors at runtime instead of exercising its competing writes.
