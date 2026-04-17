# Phase 4b.1: Universal Rules

> **Last verified:** 2026-04-16. Cross-check against current `luau_validator.py` and `api_mappings.py` before acting on prescriptions.

These rules apply to every Luau module the transpiler emits. Feed them into the transpile prompt / ruleset — not as post-hoc fixes.

## Game loop wiring

- Unity implicitly calls `Update()`, `FixedUpdate()`, `LateUpdate()` on all active MonoBehaviours every frame.
- Roblox has no implicit per-frame callbacks. A method named `Update()` that isn't connected **never executes**.
- **Always override.** Add `RunService.Heartbeat:Connect(function(dt) obj:Update(dt) end)`. Without this, the game appears frozen — no movement, spawning, or scoring. Disconnect in cleanup paths (`End`, `OnDisable`, `Destroy`).

## Threading and yielding

- Unity coroutines (`yield return`) run cooperatively on the main thread. `Update()` never yields.
- Roblox signal callbacks (`Heartbeat:Connect`, `Touched:Connect`) **cannot yield**. `task.wait()` inside a callback silently stops execution — no error, no warning.
- **Rules:**
  1. No-yield methods → plain functions.
  2. Yielding methods → wrap in `task.spawn(function() ... end)`.
  3. **Never combine `coroutine.wrap` with `task.wait()`.** `coroutine.wrap` creates a raw Lua coroutine, not a Roblox thread; `task.wait()` inside will not resume properly.

## Array indexing (0-based vs 1-based)

- The transpiler converts access (`arr[i]` → `arr[i + 1]`) but must NOT convert default/initial values of index variables.
- If C# has `usedTheme = 0` and the transpiler changes it to `1`, then `themes[usedTheme + 1]` becomes `themes[2]` — off-by-one returning `nil`.
- **Rule:** Index variables keep their C# value (0-based); the `+1` lives only in the subscript expression.

## Part size limits

Roblox Parts **silently fail to render** if any dimension exceeds 2048 studs. No error, no warning. Ground planes, roads, and terrain boundaries from Unity often exceed this. Either clamp and tile, or use Roblox Terrain. The pipeline caps visible Part sizes at 2048 studs per axis in `roblox/rbxlx_writer.py` (invisible/trigger parts get a 16384-stud cap since they don't render).

## Visibility rule — the #1 correctness issue

**No renderer = invisible. Non-negotiable.**

- **Unity:** objects are invisible unless they have a MeshRenderer, SkinnedMeshRenderer, or SpriteRenderer. A typical scene has dozens of invisible script containers, triggers, audio sources, managers.
- **Roblox:** every Part is visible by default. A Part with no mesh renders as an opaque gray block.

The pipeline MUST set `Transparency = 1` on every converted Part that lacks a renderer. Full visibility rules (enforced in `converter/scene_converter.py`):

1. **No renderer, no mesh** → `Transparency=1, CanCollide=false` (script containers, empty transforms, managers).
2. **Trigger colliders** (`isTrigger=true`) → `Transparency=1`.
3. **Inactive GameObjects** (`m_IsActive=0`) → `Transparency=1, CanCollide=false`.
4. **Disabled renderers** (`m_Enabled=0`) → `Transparency=1`.
5. **UI subtrees** (Canvas hierarchies) → filtered out of the 3D hierarchy, converted to ScreenGui by `converter/ui_translator.py`.

**Visibility is per-node, not inherited.** A parent without a renderer gets `Transparency=1`, but its children with renderers stay visible. Matches Unity's behavior. Do NOT add workarounds that force child MeshParts visible.

**Diagnostic for opaque gray rectangles blocking the view:** check (1) SpriteRenderer nodes not hidden, (2) Quad/Plane primitives not hidden, (3) MeshLoader race condition.

## Asset loading (mesh strategies)

`MeshId` is read-only at runtime. u2r supports two strategies — see `references/upload-patching.md` for detail.

- **Strategy A — Headless place builder (preferred).** `roblox/luau_place_builder.py` runs server-side via `execute_luau`, calls `CreateMeshPartAsync` for every uploaded mesh, saves the place via `SavePlaceAsync`. Resulting `.rbxlx` has real geometry visible in Studio edit mode. **No bootstrap wait needed.**
- **Strategy B — Runtime MeshLoader (fallback).** Used when headless script exceeds the 4 MB Luau Execution API limit. MeshLoader ServerScript clones MeshParts from `InsertService:LoadAsset()` results at runtime. **Bootstrap MUST wait for MeshLoader completion** before entering gameplay. Use polling, not `Changed:Wait()`:

  ```lua
  local done = ReplicatedStorage:WaitForChild("MeshLoaderDone", 120)
  if done and done:IsA("BoolValue") and not done.Value then
      while not done.Value do task.wait(0.1) end
  end
  ```

- **Skinned meshes** (FBX with bone data) are invisible as static MeshParts. Pipeline strips skinning during FBX conversion. If a mesh is invisible despite correct MeshId/Size/Transparency, check `assimp info <file>.fbx` for `Bones: N > 0`.

## ScriptableObject data and database init

- Pipeline transpiles `.asset` files to `_Data.lua` ModuleScripts via `converter/script_asset_rewriter.py`, but data still contains raw GUIDs and `nil` placeholders. GUIDs must be mapped to Template names in ReplicatedStorage, and data-loading code must be called before game start.
- **Database initialization order.** If bootstrap skips UI states (loadout/shop) that trigger database init, scripts get `nil`. Bootstrap must call all `LoadDatabase()` functions explicitly before the gameplay state. Audit every singleton's `Create`/`Init` for database-loading side effects.

## ScreenGui placement

- `StarterGui` children are auto-cloned to PlayerGui on every character spawn.
- **Always place converted ScreenGuis in `ReplicatedStorage` with `Enabled=false`.** The state machine parents them to PlayerGui when needed. Never place converted UIs in StarterGui.
