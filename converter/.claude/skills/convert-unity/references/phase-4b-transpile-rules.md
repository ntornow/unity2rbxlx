# Phase 4b.2: Transpile Rules

> **Last verified:** 2026-04-16. Cross-check `api_mappings.py` before acting on prescriptions.

Rules fed to the transpiler upfront, not applied post-hoc. Residual cases live in `phase-4c-residual-gaps.md`. **When a 4c gap becomes reliably automatable, move it here.** When this file crosses ~120 lines, split by category (`api-gaps`, `control-flow`, `inspector-refs`).

## 1. MonoBehaviour constructors

Unity components are never `new()`-ed; fields come from the Inspector. The transpiler emits `ClassName.new(config)`, but callers may not know the right config (it's in `.unity` YAML, not C# source).

Constructors start with `config = config or {}` and default every field. Bootstrap (4c) wires references after.

## 2. C# properties

No Luau `property()` primitive.

- **Trivial getter** → direct field, no method.
- **Getter with side effects** → method (`getSpeed()`). **Never emit class-level aliases** (`MyClass.speed = MyClass.getSpeed`) — Lua resolves these on the class table, returning the function instead of the value. Use `__index`/`__newindex`:

  ```lua
  local _getters, _setters = {}, {}
  MyClass.__index = function(self, key)
      local g = _getters[key]
      if g then return g(self) end
      return MyClass[key]
  end
  _getters.speed = MyClass.getSpeed
  ```

## 3. Singleton accessor

Emit `SomeClass.instance` as a value, not a function. For lazy construction, wrap with metatable `__index`:

```lua
setmetatable(Module, { __index = function(self, k)
    if k == "instance" then return self:getInstance() end
end })
```

Call sites use `Module.instance` (no parens).

## 4. Cross-module exports

Emit flat export tables: `{ ClassA = ClassA, EnumB = EnumB }`. Call sites use `Module.EnumB`, never `Module.ClassA.EnumB`. Never nest sibling classes.

## 5. Binary serialization

Unity uses `BinaryWriter`/`BinaryReader`. Roblox uses DataStore (JSON):

- `writer.Write(x)` → `data.fieldName = x`
- `reader.Read()` → `x = data.fieldName`

## 6. `GetComponent<T>()` on cloned objects

Roblox Instances don't have "components" — the object IS the thing.

- `GetComponent<Transform>()` → the instance itself.
- `GetComponent<Collider>()` → `FindFirstChildOfClass("BasePart")` or descendant search.
- `GetComponent<T>()` for custom MonoBehaviour types → corresponding module instance, passed via config at bootstrap.

## 7. Unity lifecycle → explicit calls

Roblox doesn't auto-call lifecycle methods. Preserve the bodies; the bootstrap calls them. **Never invent method names** — verify before emitting a call.

| Unity | Roblox |
|---|---|
| `Awake` | In `.new()` or right after |
| `OnEnable` | Explicit, after construction + wiring |
| `Start` | Explicit, after `OnEnable` (or merge) |
| `Update` | `RunService.Heartbeat:Connect` |
| `FixedUpdate` | `RunService.Stepped:Connect` |
| `LateUpdate` | `Heartbeat:Connect` lower priority |
| `OnDisable` | Explicit, in cleanup |
| `OnDestroy` | Explicit, or `Instance.Destroying` |
| `OnTriggerEnter/Exit` | `.Touched`/`.TouchEnded` (physics) or `GetPartsInPart` (CFrame-driven) |
| `OnCollisionEnter/Exit` | `.Touched`/`.TouchEnded` |

## 8. SetActive helper

Unity `GameObject.SetActive(bool)` toggles visibility. Emit a `SetActive(obj, bool)` helper that toggles `Transparency`/`CanCollide`. **Never emit `obj.Parent = nil`** — detaches from the scene; subsequent `obj.Parent` reads return nil and cascade. Provide the helper at the top of any module that uses it, or share via runtime.

## 9. Inspector-serialized refs

For `public SomeData dataField` (Inspector drag-and-drop):

- ScriptableObject target → `self.dataField = Database.GetEntry(<name>)` with a bootstrap-wiring comment.
- Prefab/GameObject target → `self.dataField = nil -- wired by bootstrap` plus a bootstrap entry.

The transpiler can't resolve these — the GUID→name mapping lives in scene YAML, not C# source. Mark them explicitly so 4c knows what to wire.

## 10. State-managed scene refs

State machines toggle scene objects' visibility (menu backdrop, etc.). Emit `public GameObject` fields as `nil` with a bootstrap comment. Never auto-search `workspace` inside the module — bootstrap's job. `Enter`/`Exit` calls `SetActive(obj, true/false)`, never `obj.Parent = nil`.

## 11. Consume the conversion plan

The transpiler receives `conversion_plan.json`. For each C# file:

- Look up the entry in `module_boundaries`.
- Use `suggested_type` and `suggested_container` from `storage_plan` to emit the script type and `parent_path`.
- If flagged as requiring client-only APIs, emit LocalScript idioms directly — don't wait for `script_coherence.py` to reclassify.

The plan tells the transpiler *where the code lives*; the transpiler emits idiomatic code from the start.
