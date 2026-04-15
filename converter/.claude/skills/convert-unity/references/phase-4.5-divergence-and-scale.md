# Phase 4.5b/c: Platform Divergence & Scale

> **Last verified:** 2026-04-12 against commit `e19a342`. Some prescriptions may be stale — cross-check against the current `luau_validator.py` and `api_mappings.py` before acting on them. See the 2026-04-12 audit in TODO.md for known discrepancies.

Unity is a blank canvas — no character, camera, input, or physics until you write them. Roblox provides defaults for all of these. For each pillar below, ask: **"Does the Unity game do this itself?"** Then: **"Does Roblox's default do the same thing, or must we override it?"**

## Pillars

**Character.**
- Unity: no character until `Instantiate` + scripts.
- Roblox: player gets a Humanoid rig with health, collision, animation.
- Override when: custom character controller, non-humanoid avatar, or no visible character.

**Camera.**
- Unity: no camera behavior until you script it or attach a component.
- Roblox: third-person follow camera that orbits the character.
- Override when: fixed, rail, top-down, isometric, or any non-orbit view.

**Input → Movement.**
- Unity: no movement until `Update` + `Translate` / CharacterController.
- Roblox: WASD/stick moves character, Space jumps, Humanoid handles it.
- Override when: auto-run, on-rails, grid-based, turn-based, vehicle, etc.

**Character positioning.**
- Unity: `Transform.position` set by scene or code; no default spawn.
- Roblox: spawns at a SpawnLocation or origin.
- Override: after anchoring HRP and disabling default movement, set `hrp.CFrame` to the game's starting location. Without this, the avatar floats at the default spawn.

For each pillar where Unity diverges: identify what the Unity code does, decide the override, and — if too complex to port fully — design a simpler approximation that preserves gameplay feel.

## Scale strategy

Unity uses 1 unit ≈ 1 meter. Roblox uses studs (1 stud ≈ 0.28 m). The pipeline (`core/coordinate_system.py`) applies `STUDS_PER_METER ≈ 3.571` to FBX-derived sizes and to scene positions, but a Roblox avatar is still ~5.5 studs tall vs Unity's ~1.8 units — so a Roblox avatar is ~4× larger than a typical Unity character at the converted scale.

**Decision framework — pick one:**
- **Scale character down** (preferred for dense scene geometry): `character:ScaleTo(SCALE)` with `SCALE = unity_char_height / roblox_avatar_height` (typically 0.2–0.3). Also adjust groundY, camera offset, and any world-space gameplay constants.
- **Scale world up.** Multiply positions/sizes by a uniform factor. Simpler, but re-run the pipeline and watch for broken mesh proportions.
- **Hybrid.** Scale gameplay values without touching visual scale. Fastest hack, produces visual mismatch.

**Implementation for "scale character down":**

1. Measure the Unity character's height from collider or mesh bounds.
2. Compute `SCALE = unity_height / roblox_height`.
3. Bootstrap: `character:ScaleTo(SCALE)`, then `task.wait(0.1)` for physics, then anchor HRP.
4. `GROUND_Y = default_hrp_height × SCALE`.
5. Pass `groundY` and any original Unity positioning constants through to the controllers (do not rescale the constants — the world is already at Unity scale).
6. Scale camera offset proportionally.
7. Scale world-space UI geometry (road widths, lane stripes, etc.) to match Unity's source values.
8. **Do NOT scale runtime-spawned content by default.** Both the character (now scaled down) and converted world geometry are at Unity scale. Cloned templates from ReplicatedStorage are already correct. Scaling them by the character factor makes them too small. Only scale spawned content if the Unity game explicitly scales instantiated objects in code. Note: `Model:ScaleTo()` only works on Models, not individual BaseParts. If needed:
   ```lua
   if clone:IsA("Model") then clone:ScaleTo(SCALE)
   elseif clone:IsA("BasePart") then clone.Size = clone.Size * SCALE end
   ```

## Pipeline details

**World-space computation.** Unity stores transforms as local-space. The pipeline computes world-space recursively in `converter/scene_converter.py` via the `node_to_part()` recursion that threads parent transforms through the scene tree. The formula: `world_pos = parent_pos + parent_rot * local_pos`; `world_rot = parent_rot * local_rot`; `world_scale = parent_scale × node_scale`. The Unity → Roblox axis flip (`(x, y, z)` → `(x, y, -z)`; quaternion `(qx, qy, qz, qw)` → `(-qx, -qy, qz, qw)`) lives in `core/coordinate_system.py`. If objects cluster at the origin, check that root-level scene nodes start with parent position `(0, 0, 0)` and identity rotation `(0, 0, 0, 1)`.

**FBX bounding box sizing.** MeshPart sizes are derived from FBX bounds (see `converter/mesh_processor.py`), scaled by the FBX `UnitScaleFactor` and Unity's `.fbx.meta` settings (`globalScale`, `useFileScale`). Three things must be right:
- **UnitScaleFactor** — stored in the FBX binary; `1.0` ≈ cm (scale ×0.01), `100.0` ≈ m (scale ×1.0).
- **Unity import scale** — `useFileScale=1` → `globalScale × USF/100`; `useFileScale=0` → `globalScale` alone.
- **Parent scale chain** — non-unit parent scales accumulate. If a mesh is at correct position but wrong size, walk its hierarchy for non-unit scales.

**Decoration positions are baked into prefabs — preserve them faithfully.** Artists pre-position all decoration children at specific local offsets. There is no runtime repositioning at the decoration level. Never override or "fix" these positions in pipeline output. If decorations appear to block the play area, the root cause is elsewhere (camera angle, character scale, mesh orientation) — not the baked positions.

## Mesh facing direction

The pipeline passes positions and rotations 1:1 (with the axis flip). FBX meshes are uploaded as-is; Unity is left-handed Y-up (Z-forward), Roblox is right-handed Y-up. Mesh geometry baked into the FBX may face the wrong direction.

After conversion, visually verify decoration meshes. If objects face the wrong way, apply a 180° Y-axis rotation at spawn time:

```lua
local Y_FLIP = CFrame.Angles(0, math.pi, 0)
local rot = (desc.CFrame - desc.CFrame.Position) * Y_FLIP
desc.CFrame = CFrame.new(pos) * rot
```

**This is not always needed — it depends on how the original meshes were authored. Test visually before applying.**

## Decision output

The agent decides the override approach for each pillar and the scale strategy based on the factors above, then applies them in the bootstrap (see `phase-4.5-module-rewrite.md`).
