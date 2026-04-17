# Phase 4b.1: Universal Rules

> **Last verified:** 2026-04-16. Cross-check `luau_validator.py` and `api_mappings.py` before acting on prescriptions.

Rules every emitted Luau module must follow. Feed into the transpile prompt — not a post-hoc fix pass.

## Game loop wiring

Unity calls `Update`/`FixedUpdate`/`LateUpdate` implicitly. Roblox doesn't — a method named `Update` that isn't connected **never executes**. Always emit `RunService.Heartbeat:Connect(function(dt) obj:Update(dt) end)`. Disconnect in `End`/`OnDisable`/`Destroy`. Without this the game appears frozen.

## Threading and yielding

Unity coroutines run cooperatively on the main thread. Roblox signal callbacks (`Heartbeat:Connect`, `Touched:Connect`) **cannot yield** — `task.wait()` inside silently stops with no error.

- No-yield methods → plain functions.
- Yielding methods → wrap in `task.spawn(function() ... end)`.
- **Never `coroutine.wrap` + `task.wait()`.** `coroutine.wrap` makes a raw Lua coroutine, not a Roblox thread; `task.wait()` won't resume.

## Array indexing

The transpiler converts access (`arr[i]` → `arr[i + 1]`) but **must NOT** convert default/initial values of index variables. If C# has `usedTheme = 0` and the transpiler changes it to `1`, then `themes[usedTheme + 1]` becomes `themes[2]` — off-by-one returning `nil`. Index variables keep their C# value; the `+1` lives only in the subscript.

## Part size limits

Roblox Parts **silently fail to render** if any dimension > 2048 studs. No error, no warning. Pipeline caps visible Parts at 2048 in `roblox/rbxlx_writer.py`; invisible/trigger parts get 16384 (they don't render).

## Visibility rule

**No renderer = invisible. Non-negotiable.**

Unity: objects without MeshRenderer/SkinnedMeshRenderer/SpriteRenderer are invisible. Typical scenes have dozens of invisible script containers, triggers, audio sources. Roblox: every Part is visible by default. A mesh-less Part renders as an opaque gray block.

`converter/scene_converter.py` enforces:

1. No renderer, no mesh → `Transparency=1, CanCollide=false`.
2. Trigger collider (`isTrigger=true`) → `Transparency=1`.
3. Inactive GameObject (`m_IsActive=0`) → `Transparency=1, CanCollide=false`.
4. Disabled renderer (`m_Enabled=0`) → `Transparency=1`.
5. UI subtrees (Canvas hierarchies) → ScreenGui via `converter/ui_translator.py`.

**Per-node, not inherited.** A parent without a renderer gets `Transparency=1`; its rendered children stay visible. Don't add workarounds that force child MeshParts visible.

**Diagnostic for opaque gray blocking the view:** SpriteRenderer not hidden, Quad/Plane primitives not hidden, MeshLoader race condition.

## Asset loading (mesh strategies)

`MeshId` is read-only at runtime. Two strategies — see `references/upload-patching.md`.

- **A: Headless place builder (preferred).** `roblox/luau_place_builder.py` runs server-side via `execute_luau`, calls `CreateMeshPartAsync`, saves via `SavePlaceAsync`. Geometry visible in Studio edit mode. No bootstrap wait.
- **B: Runtime MeshLoader (fallback).** When the headless script exceeds 4 MB. MeshLoader ServerScript clones from `InsertService:LoadAsset()` at runtime. Bootstrap **must** wait. Poll, don't `Changed:Wait()`:

  ```lua
  local done = ReplicatedStorage:WaitForChild("MeshLoaderDone", 120)
  if done and done:IsA("BoolValue") and not done.Value then
      while not done.Value do task.wait(0.1) end
  end
  ```

**Skinned meshes** (FBX with bones) are invisible as static MeshParts. Pipeline strips skinning. If invisible despite correct MeshId/Size/Transparency, run `assimp info <file>.fbx` for `Bones: N > 0`.

## ScriptableObject data and database init

`.asset` files become `_Data.lua` ModuleScripts via `converter/script_asset_rewriter.py`, but data still has raw GUIDs and `nil` placeholders. GUIDs must map to Template names; loading code must run before game start.

**Init order matters.** If the bootstrap skips UI states (loadout/shop) that trigger database init, scripts get `nil`. Bootstrap calls `LoadDatabase()` explicitly before the gameplay state. Audit every singleton's `Create`/`Init` for database side effects.

## ScreenGui placement

`StarterGui` children auto-clone to PlayerGui on every spawn. **Place converted ScreenGuis in `ReplicatedStorage` with `Enabled=false`** — the state machine parents them to PlayerGui when needed. Never place converted UIs in StarterGui.
