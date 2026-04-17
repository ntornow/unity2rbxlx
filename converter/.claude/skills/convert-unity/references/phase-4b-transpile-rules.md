# Phase 4b.2: Transpile Rules (migrated-upfront rules)

> **Last verified:** 2026-04-16. Cross-check against `api_mappings.py` before acting on prescriptions.

Rules fed to the transpiler as **upfront hints**, not applied post-hoc. These are semantic translations the transpiler should get right on first emission given context. Residual cases that require emitted Luau to exist live in `phase-4c-residual-gaps.md`.

**Migration policy:** when a gap category in 4c becomes a reliable, automatable rule, move it here. When `phase-4b-transpile-rules.md` crosses ~120 lines, split by category (`phase-4b-api-gaps.md`, `phase-4b-control-flow-gaps.md`, `phase-4b-inspector-ref-gaps.md`).

## 1. MonoBehaviour constructors — config defaults

Unity components are never `new()`-ed in code; fields are populated by the Inspector. The transpiler converts these to `ClassName.new(config)`, but callers may not know what config to pass (info is in `.unity` YAML, not C# source).

**Rule:** All constructors start with `config = config or {}` and default every field. Emit a `defaults` table at the top of the constructor. The bootstrap (4c) wires references after construction.

## 2. C# properties — no `property()`

C# `get`/`set` accessors have no direct Luau equivalent. Emit one of:

- **Trivial getter (wraps backing field):** direct field access. No getter method.
- **Getter with side effects:** getter method (`getSpeed()`). Never emit a class-level alias (`MyClass.speed = MyClass.getSpeed`) — this resolves on the class table, not the instance, returning the function instead of the value. Use `__index`/`__newindex` metamethods:

  ```lua
  local _getters, _setters = {}, {}
  MyClass.__index = function(self, key)
      local g = _getters[key]
      if g then return g(self) end
      return MyClass[key]
  end
  _getters.speed = MyClass.getSpeed
  ```

Never emit a `property()` helper — Luau has no such primitive.

## 3. Singleton accessor — value, not function

C# `SomeClass.instance` is a static property (getter returning the singleton). Emit as a **value** export, not a function. If the singleton requires lazy construction, wrap with a metatable `__index` so property-style access transparently calls the getter:

```lua
setmetatable(Module, { __index = function(self, k)
    if k == "instance" then return self:getInstance() end
end })
```

Call sites use `Module.instance` (no parens).

## 4. Cross-module exports — flat

When a module returns `{ ClassA = ClassA, EnumB = EnumB }`, call sites access exports directly: `Module.EnumB`, not `Module.ClassA.EnumB`. Emit flat export tables; never nest sibling classes under one.

## 5. Binary serialization → table fields

Unity often persists data via `BinaryWriter`/`BinaryReader`. Roblox uses DataStore (JSON via Lua tables). Replace:

- `writer.Write(x)` → `data.fieldName = x`
- `reader.Read()` → `x = data.fieldName`

## 6. `GetComponent<T>()` on cloned objects

Unity's `GetComponent` finds a component on a GameObject. In Roblox, cloned Instances don't have "components" — the object IS the thing. Translate to Roblox's Instance hierarchy:

- `GetComponent<Transform>()` → the instance itself (for position/rotation).
- `GetComponent<Collider>()` → `FindFirstChildOfClass("BasePart")` or descendant search.
- `GetComponent<T>()` on custom MonoBehaviour types → the corresponding module instance, passed via config during bootstrap.

## 7. Unity lifecycle → explicit calls

Unity implicitly calls lifecycle methods. Roblox has no equivalent. Emit lifecycle method bodies preserved, but the bootstrap (4c) must actually call them. **Never invent method names** — verify the method exists before emitting a call.

| Unity method | Roblox equivalent |
|---|---|
| `Awake` | Call in `.new()` or immediately after |
| `OnEnable` | Call explicitly after construction + wiring |
| `Start` | Call explicitly after `OnEnable`, or merge |
| `Update` | `RunService.Heartbeat:Connect(...)` |
| `FixedUpdate` | `RunService.Stepped:Connect(...)` |
| `LateUpdate` | `Heartbeat:Connect` with lower priority |
| `OnDisable` | Call during cleanup / state exit |
| `OnDestroy` | Call explicitly, or use `Instance.Destroying` |
| `OnTriggerEnter/Exit` | `.Touched` / `.TouchEnded` (physics-driven) or `GetPartsInPart` (CFrame-driven) |
| `OnCollisionEnter/Exit` | `.Touched` / `.TouchEnded` |

## 8. SetActive helper — never Parent assignment

Unity's `GameObject.SetActive(bool)` toggles visibility. Emit as a `SetActive(obj, bool)` helper that sets `Transparency=1`/`CanCollide=false` (off) or restores them (on). **Never emit** `obj.Parent = nil` or `obj.Parent = ReplicatedStorage` as a visibility toggle — this detaches the object from the scene, and any subsequent code reading `obj.Parent` gets nil, causing silent failures. Provide the helper at the top of any module that uses it, or share one via a common runtime module.

## 9. Inspector-serialized reference placeholders

For `public SomeData dataField` populated by Inspector drag-and-drop:

- If the target is a ScriptableObject asset → emit `self.dataField = Database.GetEntry(<name>)` with a comment flagging bootstrap wiring.
- If the target is a prefab/GameObject → emit `self.dataField = nil -- wired by bootstrap` and emit a bootstrap wiring entry.

The transpiler cannot resolve these because the GUID→name mapping lives in the scene file, not the C# source. But it should mark them explicitly so 4c knows what to wire.

## 10. State-managed scene object references

State machines often toggle scene objects' visibility (menu backdrop shown during loadout, hidden during gameplay). Emit `public GameObject` fields as `nil` with a bootstrap wiring comment. Never auto-search `workspace` inside the module — that's bootstrap's job.

The state's `Enter`/`Exit` should call `SetActive(obj, true/false)`, never `obj.Parent = nil`.

## 11. Consume the conversion plan

The transpiler receives `conversion_plan.json` as input. For each C# file being transpiled:

- Look up the file's module entry in `module_boundaries`.
- Use `suggested_type` and `suggested_container` from `storage_plan` to emit the correct script type header and `parent_path` metadata.
- If the module is flagged as requiring client-only APIs, emit LocalScript idioms (e.g., `Players.LocalPlayer`) directly; don't wait for `script_coherence.py` to reclassify.

The plan tells the transpiler *where the code lives*; the transpiler emits code idiomatic to that container from the start.
