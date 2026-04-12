# references/ — File Index

Phase-specific documentation for the `convert-unity` skill. Each file owns one narrow topic and is loaded by an explicit `Read` at the start of its phase.

## Files

| File | Phase | Topic |
|---|---|---|
| `phase-1-discovery.md` | 1 | Scene selection, parse errors |
| `phase-2-inventory.md` | 2 | Duplicate GUIDs, orphaned assets |
| `phase-3-materials.md` | 3 | Material mapping, SurfaceAppearance, split meshes |
| `phase-4-transpilation.md` | 4 | Transpile errors, review UX |
| `phase-4.5-overview.md` | 4.5 | Router for the topic files below |
| `phase-4.5-architecture-map.md` | 4.5a | State machine, ownership graph, timing model |
| `phase-4.5-divergence-and-scale.md` | 4.5b, 4.5c | Character/camera/input overrides, scale strategy |
| `phase-4.5-universal-rules.md` | 4.5d, 4.5e | Game loop, yielding, visibility, asset loading |
| `phase-4.5-animation.md` | 4.5g | Transform animation, root motion, particles |
| `phase-4.5-runtime-content.md` | 4.5f, 4.5h (partial) | Runtime spawning, templates, movement model |
| `phase-4.5-transpiler-gaps.md` | 4.5h | Semantic gaps where transpiled Luau silently breaks |
| `phase-4.5-module-rewrite.md` | 4.5h, 4.5i | Module-per-component rules, bootstrap wiring |
| `phase-5-assembly.md` | 5 | Asset upload, mesh resolution, terrain, LFS |
| `phase-6-upload.md` | 6 | Headless place publish via Open Cloud |
| `upload-patching.md` | 6 (detail) | Asset ID patching internals + Strategy A/B |

## House rules

1. **File size ceiling: 150 lines or ~6K tokens, whichever is smaller.** If a file exceeds this, split or tighten — never exceed.
2. **No game-specific content.** Rules must apply to any Unity game. No game names, project class names, project-specific numbers. Use mechanics vocabulary (genre archetypes, role names, placeholder variables).
3. **Decision frameworks, not checklists.** Every decision-bearing section names the question, the factors, the options with tradeoffs, and an escape hatch.
4. **Escape hatches preserved.** When a rule has caveats ("not always needed — test visually first"), keep them verbatim.
5. **The agent decides.** Prefer "agent decides based on X, Y, Z" over "ask the user." Only escalate when factors are genuinely ambiguous.

## Grep audit

Before merging changes to these files, run:

```
grep -iE 'trash.?dash|simplefps|simple.?fps|loadoutstate|trackmanager|industrialwarehouse|suburbshouse|fishbones|worldDistance|tutorialThemeData|weaponslot|laneOffset|changeLane' references/
```

Zero matches expected. Any hit is a regression — fix before committing.
