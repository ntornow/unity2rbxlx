# Phase 4.5: Game Logic Porting — Overview

> **Last verified:** 2026-04-12 against commit `e19a342`. Some prescriptions may be stale — cross-check against the current `luau_validator.py` and `api_mappings.py` before acting on them. See the 2026-04-12 audit in TODO.md for known discrepancies.

The AI transpiler in Phase 4 converts each file independently. Phase 4.5 provides **cross-file architectural awareness**: state machines, component wiring, platform-specific overrides, and the universal rules that keep transpiled Luau from silently breaking.

**Do NOT flatten the game into a monolithic script.** The Roblox port must mirror the Unity project's component separation, state machine structure, and timing models.

## Sub-phase files (read in order)

| Sub-phase | File | Topic |
|---|---|---|
| 4.5a | `phase-4.5-architecture-map.md` | State machines, ownership graphs, timing model |
| 4.5b, 4.5c | `phase-4.5-divergence-and-scale.md` | Character/camera/input overrides, scale strategy, mesh facing |
| 4.5d, 4.5e | `phase-4.5-universal-rules.md` | Game loop, yielding, visibility, assets, ScreenGui |
| 4.5f, 4.5h (partial) | `phase-4.5-runtime-content.md` | Runtime spawning, templates, movement model |
| 4.5g | `phase-4.5-animation.md` | Transform animation, root motion, particles |
| 4.5h | `phase-4.5-transpiler-gaps.md` | Semantic gaps where 1:1 translation silently breaks |
| 4.5h, 4.5i | `phase-4.5-module-rewrite.md` | Module-per-component rules, bootstrap wiring |

## Dependency DAG

Some files assume context from earlier files. Read in this order:

```
4.5a (architecture)
  ↓
4.5b/c (divergence + scale) ── decides bootstrap overrides
  ↓
4.5d/e (universal rules)    ── gates every module
  ↓
4.5g (animation)            ── gates spawned-content wiring
  ↓
4.5f (runtime content)      ── needs templates + animation
  ↓
4.5h (transpiler gaps)      ── reference while writing modules
  ↓
4.5i (module rewrite + bootstrap)
```

## The one rule that overrides everything

**Faithful port over workarounds.** If Unity generates content at runtime, the Roblox port must generate it at runtime too. Never substitute a Unity runtime system with a static Roblox-side workaround. The game should work the same way — if there's no static ground in Unity, there should be no static ground in Roblox. Port the system, not the symptom.
