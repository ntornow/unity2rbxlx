# Phase 4c.3: Residual Transpiler Gaps

> **Last verified:** 2026-04-16. Cross-check `api_mappings.py` and `code_transpiler.py` before acting on prescriptions.

Symptom-indexed debugging for transpiler misses that 4b upfront rules didn't cover. Read only when the validator flags an issue or the converted game has a matching visible bug.

**Any gap here that becomes reliably automatable moves to `phase-4b-transpile-rules.md`.** This file shrinks over time.

## `#instance.items` → "attempt to get length of a function value"

Property getter emitted as a class-level alias (`MyClass.items = MyClass.getItems`). Lua resolves on the class table, returns the function. Fix: `__index` metamethod (see `phase-4b-transpile-rules.md` §2). **Apply to every class with properties** — cascading failures arise when only the top class uses `__index`.

## `if not instance.isReady` always false

Same cause. Function values are truthy.

## `instance.score + 1` → "attempt to perform arithmetic on a function value"

Same cause.

## `Module.instance` returns a function

Module exports `instance = getInstance` (a function). Fix: emit `Module.instance()` at call sites, or wrap with `__index` so property-style access calls the getter. Audit every `Module.instance` use.

## `self.dataField` is nil at runtime

Inspector-serialized reference. Transpiler wrote `self.dataField = config.dataField`, but config never has it (no Inspector). Fix: route through the database (`self.dataField = Database.GetEntry(<name>)`) for ScriptableObject refs; for prefab/GameObject refs, resolve through Templates in ReplicatedStorage. Bootstrap or constructor does the wiring.

## State-managed scene object is nil

`public GameObject` on a state class — a **scene object** (not a prefab or data). Bootstrap must `workspace:FindFirstChild(...)` and pass via config. `Enter`/`Exit` calls `SetActive(obj, true/false)`, never `obj.Parent = nil`.

## `SetActive` crashes with nil

Missing helper, or `obj.Parent = nil` was emitted. Provide a `SetActive(obj, active)` helper that toggles `Transparency=1`/`CanCollide=false`. Never detach from the scene tree.

## Method call silently does nothing

Method name in caller doesn't exist in target. Luau returns `nil`. Grep the target for the exact name. Common renames: `UpdateX` ↔ `SetX`, `OnTriggerEnter` ↔ `HandleTrigger`. Fix in ALL callers.

## `require()` returns something `:new()` doesn't exist on

Module exports `{ ClassName = ClassName }` but caller wrote `local M = require(...)` then `M.new(...)`. Fix: use the `unwrap()` helper (see `phase-4c-bootstrap-emit.md`) or standardize to flat exports (4b rule 4).

## `task.wait()` inside `:Connect` silently stops

Roblox signal callbacks cannot yield. Wrap the yielding body in `task.spawn(function() ... end)`.

## Mesh invisible despite correct MeshId/Size/Transparency

Skinned mesh uploaded as static MeshPart. Run `assimp info <file>.fbx` for `Bones: N > 0`. Pipeline strips skinning during FBX conversion — re-run if bones are present.

## Player takes damage from invisible geometry

`GetPartsInPart` returns transparent parts (prefab shadow planes, invisible collision boxes). Skip `Transparency >= 1.0` in the overlap loop.

## `Changed:Wait()` on MeshLoaderDone hangs

`Changed:Wait()` fires on the *next* change — if the value was already set, never fires. Poll: `while not done.Value do task.wait(0.1) end`. Pattern in `phase-4b-universal-rules.md` ("Asset loading").

## Character snaps to T-pose during an action

Missing Roblox equivalent for a Unity animation state. Audit `.controller` YAML; every state needs an AnimationId in the bootstrap. See `phase-4a-runtime-plan.md`.

## Game appears frozen

`Update()` not wired to `Heartbeat:Connect`. Add `RunService.Heartbeat:Connect(function(dt) obj:Update(dt) end)`. Check `obj:Update` actually exists (not renamed).
