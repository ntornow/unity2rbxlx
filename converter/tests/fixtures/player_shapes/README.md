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

| fixture | origin run | paradigm-A result |
|---------|-----------|-------------------|
| `dde248_player.luau` | the **cached** dde248 SimpleFPS transpile | **A-HIT** |
| `cold3a59_player.luau` | the **cold / uncached** cold3a59 SimpleFPS re-transpile | **A-MISS** |

The conversion `output/` directory is **git-ignored** — the raw `Player.luau`
artifacts these came from are NOT committed anywhere in the repo. These two
files are therefore the **permanent copies**; there is no auto-regeneration
path (the source artifacts can't be re-derived deterministically).

### `dde248_player.luau` — the A-HIT shape

Assembled VERBATIM from the inline fragments already in
`tests/test_movement_facet_lowering.py` (`_AWAKE`, `_REAL_ROTATE`, `_REAL_MOVE`,
`_HELPERS`) into a complete `local Player … return Player` module. Its native
raw output contains, all at once:
  - a direct `cam.CFrame = …` camera write (in `Rotate`),
  - a `humanoid:Move(` call (in `Move`),
  - `_axis(Enum.KeyCode.D, …)`-style helper-wrapped WASD.

**Load-bearing:** paradigm A NEUTRALIZES these native writes (A lowers the
camera write to the service `step` and the WASD move to a host
`Humanoid:Move`). So on an A-hit shape the competition C must dominate is the
*lowered* camera write + `Humanoid:Move` — proven here as last-writer-wins.

### `cold3a59_player.luau` — the A-MISS shape

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

This is the shape that abstained BOTH PR #182 player-binding locators
(fail-closed, no bind), so paradigm A misses it and the native camera write +
rig `PivotTo` survive un-lowered.

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

**Load-bearing:** rig-`PivotTo` move → A misses → the C-dominance assertion is
camera-dominance + benign/vestigial rig `PivotTo` drift (present but NOT
contesting the camera), **NOT** a `Humanoid:Move` race — the rig drift is
deferred to Phase 3 U1.

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
