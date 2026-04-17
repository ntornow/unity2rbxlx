# Phase 4a.2: Platform Divergence & Scale

> **Last verified:** 2026-04-16. Cross-check `luau_validator.py` and `api_mappings.py` before acting on prescriptions.

Unity has no defaults. Roblox has defaults for character, camera, input, physics. For each pillar: **does Unity diverge from Roblox's default, and what's the override?**

## Pillars

**Character.** Unity: nothing until `Instantiate` + scripts. Roblox: Humanoid rig with health, collision, animation. Override for custom controllers, non-humanoid avatars, no visible character.

**Camera.** Unity: nothing until scripted. Roblox: third-person follow camera. Override for fixed, rail, top-down, isometric.

**Input → Movement.** Unity: nothing until `Update` + `Translate`/CharacterController. Roblox: WASD/stick/jump via Humanoid. Override for auto-run, on-rails, grid, turn-based, vehicles.

**Character positioning.** Unity: scripted or scene-placed. Roblox: SpawnLocation or origin. After anchoring HRP and disabling default movement, set `hrp.CFrame` to the game's start. Without this, the avatar floats at default spawn.

## Scale strategy

Unity uses 1 unit ≈ 1 m. Roblox uses studs (1 stud ≈ 0.28 m). The pipeline (`core/coordinate_system.py`) applies `STUDS_PER_METER ≈ 3.571`. But a Roblox avatar is ~5.5 studs vs Unity's ~1.8 units — ~4× larger at converted scale.

**Pick one:**
- **Char down** (preferred for dense geometry): `character:ScaleTo(SCALE)` where `SCALE = unity_height / roblox_height` (typically 0.2–0.3). Adjust groundY, camera offset, world-space gameplay constants.
- **World up.** Multiply positions/sizes uniformly. Re-run pipeline; watch for broken mesh proportions.
- **Hybrid.** Scale gameplay values without touching visuals. Fastest, produces visual mismatch.

**Char-down implementation:**

1. Measure Unity character height from collider/mesh bounds.
2. `SCALE = unity_height / roblox_height`.
3. Bootstrap: `character:ScaleTo(SCALE)`, `task.wait(0.1)` for physics, anchor HRP.
4. `GROUND_Y = default_hrp_height × SCALE`.
5. Pass `groundY` and Unity positioning constants through to controllers (don't rescale; the world is already at Unity scale).
6. Scale camera offset proportionally.
7. Scale world-space UI geometry (road widths, lane stripes) to Unity source values.
8. **Don't scale runtime-spawned content.** Templates from ReplicatedStorage are already correct; scaling them by the character factor makes them too small. Only scale spawned content if Unity scales instantiated objects in code. `Model:ScaleTo()` only works on Models, not BaseParts.

## Pipeline details

**World-space.** Unity stores transforms local-space; the pipeline computes world-space recursively in `converter/scene_converter.py` via `node_to_part()`. Formula: `world_pos = parent_pos + parent_rot * local_pos`; `world_rot = parent_rot * local_rot`; `world_scale = parent_scale × node_scale`. Axis flip in `core/coordinate_system.py`. If objects cluster at origin, root nodes likely lack identity rotation/zero position.

**FBX sizing.** MeshPart sizes from FBX bounds (`converter/mesh_processor.py`), scaled by FBX `UnitScaleFactor` and Unity `.fbx.meta` (`globalScale`, `useFileScale`):
- `UnitScaleFactor` in FBX binary; `1.0` ≈ cm (×0.01), `100.0` ≈ m (×1.0).
- `useFileScale=1` → `globalScale × USF/100`; `useFileScale=0` → `globalScale`.
- Non-unit parent scales accumulate. If position is right but size is wrong, walk hierarchy.

**Decoration positions are baked.** Never override or "fix" them. If decorations block play area, root cause is camera / scale / mesh orientation.

## Mesh facing

FBX uploaded as-is; Unity is left-handed Y-up (Z-forward), Roblox right-handed Y-up. Geometry baked into FBX may face wrong. Apply at spawn if needed:

```lua
local Y_FLIP = CFrame.Angles(0, math.pi, 0)
local rot = (desc.CFrame - desc.CFrame.Position) * Y_FLIP
desc.CFrame = CFrame.new(pos) * rot
```

**Not always needed — depends on how meshes were authored. Test visually first.**

## Output

`divergence_overrides`:

```
character: { mode: "humanoid_default" | "custom" | "non_humanoid" | "none" }
camera:    { mode: "orbit_default" | "fixed" | "rail" | "topdown" | "isometric" }
input:     { mode: "default_wasd" | "auto_run" | "on_rails" | "grid" | "vehicle" }
scale:     { strategy: "char_down" | "world_up" | "hybrid", factor: float }
mesh_facing: { apply_y_flip: bool }
```
