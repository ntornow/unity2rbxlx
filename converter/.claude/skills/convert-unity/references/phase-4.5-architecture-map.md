# Phase 4.5a: Architecture Map

> **Last verified:** 2026-04-12 against commit `e19a342`. Some prescriptions may be stale — cross-check against the current `luau_validator.py` and `api_mappings.py` before acting on them. See the 2026-04-12 audit in TODO.md for known discrepancies.

Before rewriting any module, build a mental model of how the Unity project is wired. Read every C# script in `<unity_project_path>/Assets/Scripts/` and produce three artifacts.

## 1. State machine identification

Most Unity games have a central state machine (typically a `GameManager` singleton with a hierarchy of state classes). Map out:

- **States that exist.** Use generic role labels: loading/splash, loadout/menu, gameplay, pause, terminal/game-over. Use the actual class names in your notes, but think in roles — they transfer cleanly to Roblox.
- **Transitions.** For every state, which state does it switch, push, or pop to, and what triggers the transition? (Button click, death event, timer expiry, server message.)
- **Per-state behavior.** What does `Enter`/`Exit`/`Tick` (or the framework's equivalent) do? This tells you what the Roblox port must do in each state's module.

## 2. Component ownership graph

Map which MonoBehaviour owns references to which. Inspector-assigned references become explicit constructor/config wiring in Luau. Trace the chain from the root state down to the leaf components (manager → controller → character/world).

- **Singletons** (`static instance` pattern) → module-level state in Luau. These are easy to spot but easy to mis-wire: the transpiler often turns `Class.instance` into a *function*, not a value — see `phase-4.5-transpiler-gaps.md`.
- **Inspector-serialized fields** (`public SomeClass field`) → config entries passed in at construction time. The bootstrap does the wiring.
- **Scene object references** (`public GameObject x`) → the bootstrap must find these in `workspace` by name and pass them through. The transpiler cannot do this because the GUID-to-name mapping lives in the `.unity` YAML, not the C# source.

## 3. Timing model

Identify how the Unity game measures durations. This is a silent correctness issue — the wrong model preserves game logic but changes game feel.

- **Time-based** (`Time.deltaTime` accumulators). Direct map to a Roblox `Time` helper / `os.clock()` / `tick()`.
- **World-distance-based.** Durations are measured by distance traveled, not elapsed seconds. Common in games where movement speed changes dynamically (many games scale duration by `(1 + speedRatio)` or similar). **The Roblox port MUST preserve this** — swapping in seconds breaks the gameplay feel at higher speeds.
- **Coroutine-based** (`StartCoroutine` + `yield return`). Map to `task.spawn` + `task.wait`. But note: signal callbacks in Roblox cannot yield — see the yielding rules in `phase-4.5-universal-rules.md`.

## Decision: does the map match reality?

Present the architecture map. The agent decides whether it's coherent based on:

- **Every gameplay subsystem is accounted for.** If scoring or spawning isn't in the map, a script was missed.
- **Transition edges form a connected graph.** Orphaned states are a red flag.
- **Timing model is consistent across modules.** Mixing time-based and distance-based within one subsystem usually means the map is wrong.

**Escape hatch:** If the state machine is unclear, start from `Assets/Scenes/*.unity` — the serialized GameManager's initial state field names the first state, and you can walk outward from there.
