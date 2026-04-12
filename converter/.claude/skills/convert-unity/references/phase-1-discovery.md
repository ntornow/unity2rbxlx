# Phase 1: Discovery

Scans `<unity_project_path>` for scenes, prefabs, and top-level structure. Produces a JSON summary listing scenes with their sizes and dependency counts.

## Command

```bash
python3 convert_interactive.py discover <unity_project_path> <output_dir> 2>/dev/null
```

## Decision: scene selection (when multiple scenes exist)

**Question:** Which scene is the primary gameplay scene?

**Factors:**
- Scene size (object count, asset count). Gameplay scenes are usually the largest.
- Name conventions. `Main`, `Game`, `Play` are strong gameplay signals; `Menu`, `Splash`, `Loading` are not.
- Dependency count. Gameplay scenes reference the most prefabs and scripts.
- Presence of character controllers, camera rigs, or level geometry in the scene YAML.

**Options:**
- **Single dominant scene.** One scene leads by size and dependency count — pick it.
- **Multi-scene game.** The game streams scenes at runtime (additive loading). Pick the scene with the player controller and core game loop; other scenes get ported as templates in later phases.
- **Genuinely ambiguous.** Two or more scenes look equally gameplay-relevant. Only in this case, surface the list and escalate.

**Escape hatch:** If nothing looks like a gameplay scene, read `ProjectSettings/EditorBuildSettings.asset` — it lists the scenes Unity considers part of the build. The pipeline also has a heuristic in `Pipeline.parse` (`converter/pipeline.py`) that picks the highest-scoring "gameplay" scene; honour `--scene <path>` or `--scene all` to override.

## Decision: parse errors

**Question:** Do parse errors block the conversion?

**Factors:**
- Error rate (errors / total files scanned). A few warnings on thousands of files is noise; errors on 10% of files is structural.
- File type. Errors in `.unity`/`.prefab` YAML are load-bearing; errors in `.meta` sidecars usually aren't.
- Whether the primary scene parses cleanly.

**Options:**
- **Continue with warnings.** Low error rate, errors concentrated in non-gameplay assets. Log and move on.
- **Abort.** High error rate, or errors in the primary scene. Downstream phases will produce unreliable output.

**Escape hatch:** Inspect the discover JSON's `parse_errors` array before deciding. Binary `.unity` files require UnityPy — if the project ships only binary scenes and UnityPy isn't installed, that's an environment fix, not a project problem.
