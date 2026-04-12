# Phase 4.5f/h (partial): Runtime Content

> **Last verified:** 2026-04-12 against commit `e19a342`. Some prescriptions may be stale — cross-check against the current `luau_validator.py` and `api_mappings.py` before acting on them. See the 2026-04-12 audit in TODO.md for known discrepancies.

Many Unity games generate gameplay content at runtime — spawned enemies, level chunks, procedural terrain, collectible placements. This is the **#1 system that does not survive transpilation** because it depends on Inspector-serialized prefab references, Addressables async loading, and object pooling — none of which have Roblox equivalents. The transpiled code keeps the scoring/movement logic, but spawning methods become empty shells with nil references.

## Why it breaks

- Prefab references are Inspector-serialized `AssetReference`s or Addressable keys — nil in Roblox.
- Object pooling libraries are not transpiled.
- ScriptableObject data that maps themes/levels to prefab lists contains raw GUIDs.
- The transpiled manager keeps `Update` ticking but spawn methods have zero functional code.

**Diagnostic:** If a converted game "runs" (score ticks, character animates) but the world is empty — no content, no obstacles, no collectibles — the spawning system was not ported. Check the manager's spawn arrays: if they stay empty, spawning is broken.

## Input wiring

- Unity polls input via `Input.GetKeyDown()` in `Update`. No setup required.
- Roblox uses `UserInputService.InputBegan`/`InputEnded` signals. No polling API. The transpiler does NOT create signal connections.
- **The bootstrap must connect signals** and dispatch keys to controller methods. Map `Input.GetKeyDown(KeyCode.X)` to `Enum.KeyCode.X`. Without this, the game is unresponsive to player input.

## Physics

- Unity: Rigidbody is opt-in, gravity/collision configured per-object.
- Roblox: all parts have physics; the character has Humanoid physics with WalkSpeed/JumpPower.
- Override when: the game positions objects directly via CFrame/Transform rather than through physics forces. The pipeline emits `runtime/physics_bridge.luau` to help bridge common Unity physics idioms.

## What spatial data does NOT survive conversion

- **Path/spline data** (child Transforms defining waypoints or curves) — the pipeline strips non-rendered objects (`Transparency=1, CanCollide=false`). If Unity uses child Transforms as waypoints, they become featureless invisible Parts. **Do not write auto-discovery code** that walks a Model's children looking for waypoints — it will find rendering geometry instead.
- **Normalized position values** (e.g., spawn positions stored as 0–1 t-values along a path) — meaningless without the original path.
- **Collider-only geometry** (trigger volumes, invisible walls) — stripped or transparent. If gameplay depends on trigger placement within prefabs, extract that data manually.

## What DOES survive

- **Template Models** in `ReplicatedStorage/Templates` preserve their Unity prefab names and visible mesh hierarchy. The pipeline names them from the prefab root GameObject.
- **UnityLayer attributes** set by the pipeline on scene-placed instances (e.g., collectible/obstacle classification layers). Use `part:GetAttribute("UnityLayer")` for collision classification — do NOT invent custom tagging systems (BoolValues, CollectionService tags) that duplicate this. **Important:** UnityLayer attributes are on scene instances, NOT on prefab templates. When cloning templates at runtime, the spawning code must set `UnityLayer` explicitly on cloned parts. Do not auto-discover templates by scanning for UnityLayer — use explicit name lists.
- **ScriptableObject data** as `_Data.lua` ModuleScripts — but contains raw GUIDs that must be resolved to Template names.

## Porting pattern

1. **Identify templates.** The pipeline produces Models in `ReplicatedStorage/Templates`. Each Model keeps its Unity prefab name. **Never auto-discover templates** by scanning name substrings or child structure — use the known prefab names from the Unity project. Build a lookup table mapping names (or Unity GUIDs from the data modules) to template references.

2. **Extract per-template metadata from Unity prefab YAML.** Read `.prefab` files to determine: template dimensions/length, sub-object spawn positions, which sub-templates can appear within a template. Hardcode this metadata in a Luau table — it cannot be derived at runtime because spatial data is lost during conversion.

3. **Write spawn logic that `:Clone()`s templates** and positions them in world space. Replace Unity's `Instantiate()` with `:Clone()` + `Parent = workspace`. Resolve ScriptableObject GUID references to Template names (see `phase-4.5-transpiler-gaps.md`).

4. **Implement cleanup.** When spawned content moves past a threshold from the player, `:Destroy()` it. No need to port Unity's object pooling — Roblox's instance creation is fast enough. If a clone has an animator, call `animator:Destroy()` before destroying the clone.

   **Wire transform animations on spawned clones.** Check ReplicatedStorage for animator config modules matching template names (see `phase-4.5-animation.md`). Without this, converted objects that had spin/bob/tilt animations in Unity will be static.

5. **Wire into the game loop.** Spawning checks must run every frame via the Heartbeat connection, not as a one-time setup.

6. **Create ground/environment surfaces explicitly if needed.** Unity games often have invisible ground planes, procedurally generated floors, or terrain that doesn't convert as renderable geometry. Only substitute a static surface when the Unity game's ground is **genuinely missing** — if Unity generates ground at runtime, port the generation system instead (faithful port over workarounds).

## Movement model — account for lost spatial data

- If Unity moves objects along spline paths whose waypoints are lost, determine the *effective* movement from the Unity code: is it straight-line, curved, grid-based? Port the effective movement, not the spline interpolation mechanism.
- If Unity uses `GetPointAt(t)` but the underlying path is a straight line, replace with direct position arithmetic. If the path is genuinely curved, manually extract and hardcode the control points.

## Movement direction — match the pipeline's coordinate system

- The pipeline places objects at their Unity world positions with the axis flip applied (`(x, y, z)` → `(x, y, -z)` — see `core/coordinate_system.py`). Unity's forward axis is +Z; after the flip, converted scene objects are arranged along −Z in Roblox space.
- The game loop's movement direction **must match** the axis the converted objects are placed along. If segments lie along −Z, the character moves in −Z.
- **Character facing:** if the character should face the converted "forward" direction, set the appropriate `CFrame.Angles` since Roblox's default front face is −Z.
- **Camera placement:** use absolute world-space offsets (e.g., `characterPos + Vector3.new(0, height, behindDistance)`) rather than rotation-relative offsets (e.g., `charCF.LookVector * distance`). Rotation-relative offsets break when the character has a fixed facing rotation, because LookVector points in the character's local forward — which may be opposite to world movement.
