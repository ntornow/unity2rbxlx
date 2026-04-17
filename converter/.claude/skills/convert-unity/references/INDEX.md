# references/ — File Index

Phase-specific documentation for the `convert-unity` skill. Each file owns one narrow topic and is loaded by an explicit `Read` at the start of its phase.

## Files

| File | Phase | Topic |
|---|---|---|
| `phase-1-discovery.md` | 1 | Scene selection, parse errors |
| `phase-2-inventory.md` | 2 | Duplicate GUIDs, orphaned assets |
| `phase-3-materials.md` | 3 | Material mapping, SurfaceAppearance, split meshes |
| `phase-4a-overview.md` | 4a | Router for the plan sub-phase files below |
| `phase-4a-architecture-map.md` | 4a.1 | State machine, ownership graph, timing model |
| `phase-4a-divergence-and-scale.md` | 4a.2 | Character/camera/input overrides, scale strategy, mesh facing |
| `phase-4a-runtime-plan.md` | 4a.3 | Animation approach, templates manifest, movement model |
| `phase-4a-module-boundaries.md` | 4a.4 | Module-per-component plan, bootstrap shape |
| `phase-4a-storage-classification.md` | 4a.5 | **Server / client / replicated storage decisions (3 networking questions)** |
| `phase-4-transpilation.md` | 4b | Transpile errors, review UX |
| `phase-4b-universal-rules.md` | 4b.1 | Game loop, yielding, visibility, assets, ScreenGui |
| `phase-4b-transpile-rules.md` | 4b.2 | Transpile-time rules (migrated from transpiler gaps) |
| `phase-4c-overview.md` | 4c | Router for reactive sub-phase files below |
| `phase-4c-runtime-emit.md` | 4c.1 | Spawner code, animator wiring on clones, particle `:Emit()` |
| `phase-4c-bootstrap-emit.md` | 4c.2 | Emit `GameBootstrap.lua` against transpiled modules |
| `phase-4c-residual-gaps.md` | 4c.3 | Symptom-indexed debugging for unmigrated transpiler misses |
| `phase-5-assembly.md` | 5 | Asset moderation (Roblox safety standards), upload, mesh resolution, terrain, LFS |
| `phase-6-upload.md` | 6 | Headless place publish via Open Cloud |
| `upload-patching.md` | 6 (detail) | Asset ID patching internals + Strategy A/B |
| `playtest-gotchas.md` | (any) | MCP playtest caveats: module-instance caveat, click coords, Touched spam |

## Phase flow

```
Phase 4 (transpile) = 4a (plan) -> 4b (transpile with plan) -> 4c (reactive fixups)
```

Phase 4a produces `conversion_plan.json`. Phase 4b consumes it to emit correctly-classified Luau. Phase 4c emits bootstrap/spawners and patches residual gaps.

## House rules

1. **File size ceiling: 150 lines or ~6K tokens, whichever is smaller.** If a file exceeds this, split or tighten — never exceed.
2. **No game-specific content.** Rules must apply to any Unity game. No game names, project class names, project-specific numbers. Use mechanics vocabulary (genre archetypes, role names, placeholder variables).
3. **Decision frameworks, not checklists.** Every decision-bearing section names the question, the factors, the options with tradeoffs, and an escape hatch.
4. **Escape hatches preserved.** When a rule has caveats ("not always needed — test visually first"), keep them verbatim.
5. **The agent decides.** Prefer "agent decides based on X, Y, Z" over "ask the user." Only escalate when factors are genuinely ambiguous.
6. **Migrate leftward.** Reactive rules in 4c that become reliably automatable should move to 4b transpile rules. The 4c residual list shrinks over time.

## Grep audit

Before merging changes to these files, run:

```
grep -iE 'trash.?dash|simplefps|simple.?fps|loadoutstate|trackmanager|industrialwarehouse|suburbshouse|fishbones|worldDistance|tutorialThemeData|weaponslot|laneOffset|changeLane' references/
```

Zero matches expected. Any hit is a regression — fix before committing.
