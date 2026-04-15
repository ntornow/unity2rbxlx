# Phase 4.5h: Transpiler Semantic Gaps

> **Last verified:** 2026-04-12 against commit `e19a342`. Some prescriptions may be stale — cross-check against the current `luau_validator.py` and `api_mappings.py` before acting on them. See the 2026-04-12 audit in TODO.md for known discrepancies.

The AI transpiler translates C# syntax but can miss platform-level semantic differences. Each category below is a known failure mode where 1:1 translation produces broken Luau. Use this file as a **symptom-indexed debugging reference** while writing modules and during validation.

## 1. MonoBehaviour construction vs Inspector wiring

Unity components are never `new()`-ed in code — they're attached to GameObjects, and their fields are populated by the Inspector (serialized scene references). The transpiler converts these to `ClassName.new(config)` constructors, but callers may not know what config to pass (the info is in `.unity` YAML, not C# source).

**Fix:** All constructors must start with `config = config or {}` and default every field. The bootstrap wires references after construction, the same way Unity's Inspector does.

## 2. C# properties — Luau has no `property()`

C# `get`/`set` accessors have no direct Luau equivalent. If a property is trivial (wraps a backing field), use a direct field. If it has side effects, use getter/setter methods. Never emit `property()` calls.

## 3. Binary serialization → table fields

Unity often persists data via `BinaryWriter`/`BinaryReader`. Roblox uses DataStore (JSON via Lua tables). Replace `writer.Write(x)` / `reader.Read()` with `data.field = x` / `x = data.field`.

## 4. Cross-module exports

When a module returns `{ ClassA = ClassA, EnumB = EnumB }`, access exports directly: `Module.EnumB`, not `Module.ClassA.EnumB`. The export table is flat — classes don't own sibling exports.

## 5. `GetComponent<T>()` on cloned objects

Unity's `GetComponent` finds a component on a GameObject. In Roblox, cloned Instances don't have "components" — the object IS the thing. Adapt to Roblox's Instance hierarchy (`FindFirstChild`, `:IsA()`, or direct construction).

## 6. Singleton accessor — function vs value

In C#, `SomeClass.instance` is a static property (getter returns the singleton). The transpiler converts this to a module export `instance = getInstance` — a **function**, not a value. But call sites still emit `SomeClass.instance` (property-style access), which returns the function itself instead of calling it.

**Fix:** Either (a) emit `Module.instance()` at call sites, or (b) use a metatable `__index` so property-style access transparently calls the getter. Until then, audit every `Module.instance` usage — if the module exports `instance = someFunction`, every access must have `()`.

## 7. Unity lifecycle → Luau explicit calls

Unity implicitly calls lifecycle methods in a specific order. Roblox has no equivalent — all lifecycle calls must be explicit in the bootstrap or via RunService connections. The transpiler preserves the method bodies but the bootstrap must actually call them. **Never invent method names** — always verify the method exists in the transpiled output before calling it.

| Unity method | When Unity calls it | Roblox equivalent |
|---|---|---|
| `Awake` | Once, on object creation (before Start) | Call in `.new()` or immediately after |
| `OnEnable` | When the object becomes active | Call explicitly after construction + wiring |
| `Start` | Once, on the first frame the object is active | Call explicitly after `OnEnable`, or merge into it |
| `Update` | Every frame | `RunService.Heartbeat:Connect(...)` |
| `FixedUpdate` | Every physics step | `RunService.Stepped:Connect(...)` |
| `LateUpdate` | Every frame, after all Updates | `Heartbeat:Connect` with lower priority |
| `OnDisable` | When the object is deactivated | Call explicitly during cleanup / state exit |
| `OnDestroy` | When the object is destroyed | Call explicitly, or use `Instance.Destroying` |
| `OnTriggerEnter/Exit` | Physics trigger events | `.Touched` / `.TouchEnded` (see wiring notes below) |
| `OnCollisionEnter/Exit` | Physics collision events | `.Touched` / `.TouchEnded` |

**Pitfall:** The transpiler may rename or merge lifecycle methods inconsistently. Some modules keep `OnEnable`, others rename to `Start`, others have both. The bootstrap must read each module's actual method names — never assume a standard name exists. **`Update`/`FixedUpdate` require explicit Heartbeat wiring** — see `phase-4.5-universal-rules.md`.

## 8. C# property getters as function aliases — the silent killer

C# properties like `public float speed { get { return _speed; } }` get transpiled as a getter method (`getSpeed()`) plus a class-level alias: `MyClass.speed = MyClass.getSpeed`. This makes `instance.speed` return **the function itself**, not the value.

**This must be applied to EVERY class with properties, not just the main class.** If ClassA uses `__index` metamethods but ClassB (used by ClassA) doesn't, `classB.someProp` returns nil even though `getSomeProp()` exists. Cascading failures:

- `#instance.items` → "attempt to get length of a function value"
- `if not instance.isReady` → always false (function is truthy), skipping critical init
- `instance.score + 1` → "attempt to perform arithmetic on a function value"

**Fix:** Replace class-level aliases with `__index`/`__newindex` metamethods that call getters/setters automatically:

```lua
local _getters, _setters = {}, {}
MyClass.__index = function(self, key)
    local g = _getters[key]
    if g then return g(self) end
    return MyClass[key]
end
MyClass.__newindex = function(self, key, value)
    local s = _setters[key]
    if s then s(self, value) return end
    rawset(self, key, value)
end
-- Register: _getters.speed = MyClass.getSpeed
--           _setters.speed = function(self, v) self._speed = v end
```

Class-level aliases (`MyClass.prop = MyClass.getProp`) are fundamentally broken because Lua resolves them on the *class table*, not the instance.

## 9. Inspector-serialized ScriptableObject references are nil

In Unity, `public SomeData dataField` is populated by dragging a ScriptableObject asset onto it in the Inspector. At runtime, it has a valid reference. In Roblox, the transpiler writes `self.dataField = config.dataField`, but the config never has the value (no Inspector). The field stays nil; any code reading it crashes.

**Fix:** For data references that point to ScriptableObjects now converted to `_Data` ModuleScripts, wire them through a database lookup: `self.dataField = <Database>.GetEntry(<name>)`. For Inspector refs to prefabs/GameObjects, resolve through the Templates folder in ReplicatedStorage. The bootstrap or the module constructor must do this wiring — the transpiler cannot, because the GUID→name mapping lives in the scene file.

## 10. State-managed scene objects require explicit wiring

State machines often toggle scene objects' visibility as part of state transitions — a menu backdrop shown during loadout, hidden during gameplay. These `public GameObject` scene references are nil in Roblox. Unlike data references (#9), they are **3D objects already in workspace** that need to be found by name and passed through config.

**Fix:** The bootstrap must `workspace:FindFirstChild("ObjectName")` for each state-managed scene object and pass it via the state's config table. The state's `Enter`/`Exit` calls a `SetActive(obj, true/false)` helper which toggles `Transparency` and `CanCollide` — the object stays in workspace, never reparented. **Never use `obj.Parent = nil`** — this nulls the parent and causes cascading errors when other code reads `.Parent`.

To identify which objects need wiring: look for `public GameObject` fields on state classes that aren't prefabs or UI — they're scene environment objects toggled by `SetActive(true/false)`.

## 11. SetActive must use a helper, never Parent assignment

Unity's `GameObject.SetActive(bool)` toggles visibility. The transpiler must convert `obj.SetActive(false)` → a `SetActive(obj, false)` helper that sets `Transparency=1` and `CanCollide=false`. **Never emit** `obj.Parent = nil` or `obj.Parent = ReplicatedStorage` as a visibility toggle — this detaches the object from the scene tree, and any subsequent code reading `obj.Parent` (including the MeshLoader replacement pattern) gets nil, causing silent failures or crashes. Provide the SetActive helper at the top of any module that calls it, or share one via a common runtime module.
