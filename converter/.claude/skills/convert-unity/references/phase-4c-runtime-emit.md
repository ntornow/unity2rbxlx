# Phase 4c.1: Runtime Emission (spawners + animator wiring)

> **Last verified:** 2026-04-16. Cross-check against current `luau_validator.py` and `api_mappings.py` before acting on prescriptions.

Write spawn/cleanup code and animator wiring on top of already-transpiled manager modules. Inputs: `templates_manifest` and `animation_plan` from 4a, transpiled modules from 4b.

## Why it breaks without this phase

Many Unity games generate gameplay content at runtime — spawned enemies, level chunks, procedural terrain, collectibles. This is the **#1 system that does not survive transpilation** because it depends on Inspector-serialized prefab references, Addressables async loading, and object pooling — none have Roblox equivalents. Transpiled code keeps the scoring/movement logic, but spawning methods become empty shells with nil references.

**Diagnostic:** If a converted game "runs" (score ticks, character animates) but the world is empty — no content, no obstacles, no collectibles — the spawning system was not ported. Check the manager's spawn arrays: if they stay empty, spawning is broken.

## Porting pattern

1. **Identify templates from the plan.** Use `templates_manifest` from `conversion_plan.json`. Each entry maps a name to a Model in the container assigned by 4a.5 storage classification (`ReplicatedStorage/Templates` or `ServerStorage/Templates`). **Never auto-discover templates** by scanning name substrings or child structure — use the explicit plan names.

2. **Use per-template metadata from the plan.** `templates_manifest.metadata` contains dimensions / length, sub-object spawn positions, sub-template slots — extracted from Unity prefab YAML by 4a.3.

3. **Write spawn logic that `:Clone()`s templates** and positions them in world space. Replace Unity's `Instantiate()` with `:Clone()` + `Parent = workspace`. Resolve ScriptableObject GUID references to Template names via the data modules.

4. **Implement cleanup.** When spawned content moves past a threshold, `:Destroy()` it. No need to port Unity's object pooling — Roblox's instance creation is fast enough. If a clone has an animator, call `animator:Destroy()` before destroying the clone.

   **Wire transform animations on spawned clones.** Check ReplicatedStorage for animator config modules matching template names. Without this, converted objects that had spin/bob/tilt animations in Unity will be static.

5. **Wire into the game loop.** Spawning checks must run every frame via the Heartbeat connection, not as a one-time setup.

6. **Create ground/environment surfaces explicitly if needed.** Unity games often have invisible ground planes, procedurally generated floors, or terrain that doesn't convert as renderable geometry. Only substitute a static surface when the Unity game's ground is **genuinely missing** — if Unity generates ground at runtime, port the generation system instead (faithful port over workarounds).

## Wiring animations to spawned clones

The pipeline auto-generates animator config modules and injects the runtime animator into ReplicatedStorage. But this only provides data + engine — the scripts that **spawn** animated objects must wire them up:

```lua
local Animator = require(ReplicatedStorage:WaitForChild("AnimatorRuntime"))
local Config = require(ReplicatedStorage:WaitForChild(templateName .. "_AnimConfig"))
-- After cloning and positioning:
local animator = Animator.new(clone, Config)
table.insert(spawnedObjects, { model = clone, animator = animator })
```

On cleanup, call `animator:Destroy()` before destroying the clone. The runtime animator auto-ticks via a shared Heartbeat connection.

**Identify which templates need animation from the plan**, not by scanning names at runtime. `animation_plan.transform_anims` and `animation_plan.mecanim_anims` list them explicitly.

## Particle emission — burst triggers

For particle systems flagged `burst` in `animation_plan.particle_systems`, wire `emitter:Emit(burstCount)` calls at the right gameplay moments (collection sparkles, death effects). Sub-emitter wiring: `runtime/sub_emitter_runtime.luau`.

## Scene object classification — menu vs gameplay

Unity scenes contain objects meant for different contexts. The pipeline places non-prefab scene objects into Workspace. Prefab instances are automatically excluded (they live in Templates).

- **Menu/UI scene objects** (title backdrops, menu cameras, preview platforms): hide via `SetActive(obj, false)` (`Transparency=1, CanCollide=false` on descendants). Identify by name patterns from scene hierarchy ("Menu", "UI", "Background", "Title"). Confirm against `.unity` YAML before hiding.
- **Gameplay environment**: keep visible. Decoration positions are baked into prefabs — preserve them.
- **Broken visual artifacts** (white boxes, gray rectangles from missing textures): remove from both Workspace and Templates.

Hide by **known name list**, not broad pattern matching that could catch gameplay objects.
