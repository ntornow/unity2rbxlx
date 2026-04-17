# Phase 4c.3: Residual Transpiler Gaps

> **Last verified:** 2026-04-16. Cross-check against current `luau_validator.py` and `api_mappings.py` before acting on prescriptions.

Symptom-indexed debugging reference for transpiler misses that the 4b upfront rules couldn't cover. Load this file **only if** the validator flags an issue or the converted game has a visible bug matching a symptom below.

**Migration policy:** any gap here that becomes a reliable automatable rule should move to `phase-4b-transpile-rules.md`. This file shrinks over time.

## Symptom: `#instance.items` raises "attempt to get length of a function value"

**Cause:** C# property getter was emitted as a class-level alias (`MyClass.items = MyClass.getItems`). Lua resolves this on the class table, returning the function itself.

**Fix:** Replace the class-level alias with a `__index` metamethod (see `phase-4b-transpile-rules.md` section 2). Register each getter in a `_getters` table. This applies to **every class with properties, not just the main class** — cascading failures arise when ClassA uses `__index` but ClassB (used by ClassA) doesn't.

## Symptom: `if not instance.isReady` always false

**Same cause as above.** A function value is truthy, so `not <function>` is always false. Apply the same fix.

## Symptom: `instance.score + 1` raises "attempt to perform arithmetic on a function value"

**Same cause.** Same fix.

## Symptom: `Module.instance` returns a function, not a value

**Cause:** The module exports `instance = getInstance` — a function. Property-style access returns the function itself.

**Fix:** Option (a) emit `Module.instance()` at call sites. Option (b) add a metatable `__index` so property-style access transparently calls the getter. Audit every `Module.instance` usage.

## Symptom: `self.dataField` is nil at runtime

**Cause:** Inspector-serialized reference. The transpiler wrote `self.dataField = config.dataField`, but config never has the value (no Inspector in Roblox).

**Fix:** For references to ScriptableObjects (now `_Data` ModuleScripts), wire through a database lookup: `self.dataField = <Database>.GetEntry(<name>)`. For Inspector refs to prefabs/GameObjects, resolve through the Templates folder in ReplicatedStorage. The bootstrap or module constructor must do this wiring.

## Symptom: state-managed scene object is nil

**Cause:** `public GameObject` field on a state class that toggles visibility. These are **3D objects already in workspace**, not prefabs or data.

**Fix:** Bootstrap must `workspace:FindFirstChild("ObjectName")` for each state-managed scene object and pass it via the state's config table. `Enter`/`Exit` calls `SetActive(obj, true/false)` — never `obj.Parent = nil`.

## Symptom: calls to `SetActive` crash with nil

**Cause:** Missing helper, or `obj.Parent = nil` was emitted instead.

**Fix:** Provide a `SetActive(obj, active)` helper at the top of any module that calls it (or share via a common runtime module). Helper sets `Transparency=1`/`CanCollide=false` on off; restores on on. Never detach from scene tree.

## Symptom: method call silently does nothing

**Cause:** Caller uses a method name the target module doesn't have. Luau returns `nil`, `nil(...)` raises — but `if self:foo() then ... end` silently skips when `foo` is nil... actually it errors too. If it truly silently does nothing, the method exists but returns early.

**Fix:** Grep the target module for the exact method name. Common renames: `UpdateX` ↔ `SetX`, `OnTriggerEnter` ↔ `HandleTrigger`. Fix in ALL callers.

## Symptom: `require()` returns something `:new()` doesn't exist on

**Cause:** Module exports `{ ClassName = ClassName }` but caller wrote `local M = require(...)` then `M.new(...)`.

**Fix:** Use the `unwrap()` helper from `phase-4c-bootstrap-emit.md`. Or standardize transpile-time exports to flat (4b rule 4).

## Symptom: yielded `task.wait()` inside `Heartbeat:Connect` silently stops

**Cause:** Roblox signal callbacks cannot yield.

**Fix:** Wrap the yielding body in `task.spawn(function() ... end)`. Audit every `:Connect` call that uses `task.wait` or RPC calls.

## Symptom: mesh invisible despite correct MeshId/Size/Transparency

**Cause:** Skinned mesh (FBX with bone data) uploaded as a static MeshPart is invisible.

**Fix:** Check `assimp info <file>.fbx` for `Bones: N > 0`. Pipeline strips skinning during FBX conversion — re-run extraction if bones are present.

## Symptom: player takes damage from invisible geometry

**Cause:** `GetPartsInPart` returns transparent parts (prefab shadow planes, invisible collision boxes).

**Fix:** In the per-frame overlap pattern, skip parts with `Transparency >= 1.0`.

## Symptom: `Changed:Wait()` on MeshLoaderDone hangs forever

**Cause:** `Changed:Wait()` on a BoolValue only fires on the next change — if the value was already set, it never fires.

**Fix:** Poll: `while not done.Value do task.wait(0.1) end`. Wrap in a WaitForChild timeout. Pattern in `phase-4b-universal-rules.md` under "Asset loading".

## Symptom: character snaps to T-pose during an action

**Cause:** Missing Roblox equivalent for a Unity animation state.

**Fix:** Audit the `.controller` YAML; verify every state has a Roblox AnimationId mapped in the bootstrap's state table. See `phase-4a-runtime-plan.md` Part 1.

## Symptom: game appears frozen, no movement/spawning/scoring

**Cause:** `Update()` method not wired to `Heartbeat:Connect`.

**Fix:** In the bootstrap (or module init), add `RunService.Heartbeat:Connect(function(dt) obj:Update(dt) end)`. Also check `obj:Update` exists (not renamed).
