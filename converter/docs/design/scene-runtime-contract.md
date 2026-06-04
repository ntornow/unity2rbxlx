# Design: Generic Scene-Runtime Contract

> Status: **architecturally sound** (2026-05-20). Replaces the regressed
> `generate_bootstrap_script()` and the per-genre `--scaffolding` hacks
> with one generic mechanism. PR1–PR2 are independently landable with the
> schema locked here (stable prefab IDs; no `target_domain` in the
> reference schema). Begin implementation with PR1.

## Problem

Unity runs a lifecycle: instantiate every scene MonoBehaviour, inject
Inspector-serialized references, call `Awake → OnEnable → Start →
Update/FixedUpdate/LateUpdate`, then `OnDisable/OnDestroy` on teardown.
Roblox does none of it.

Today the converter has **no generic mechanism**:

- The old `generate_bootstrap_script()` was deleted and was never generic
  anyway (hardcoded class names).
- `--scaffolding=fps` injects a genre-specific FPS controller/HUD; slated
  for removal.
- The pipeline fallback `ClientBootstrap` only `require()`s side-effect
  modules; it never instantiates or starts anything.
- The skill expects an agent to hand-author the bootstrap — silently
  skippable, non-reproducible, unavailable to CI.

## Principle

**Reproduce Unity's runtime generically. Never look for a "game manager".**
Instantiate every runtime-bearing MonoBehaviour, wire every serialized
reference, run the lifecycle. The Unity scene YAML — not the AI-generated
Luau — is the source of truth.

## Architecture — six pieces

```
 Unity scenes ──► (2) scene_runtime_planner ──► scene_runtime_plan
                         │                          │
 transpiled modules ◄─(1) runtime contract           │
       │                  (verify + reprompt)        │
       ▼                                             ▼
 (4) module packaging ──► ModuleScripts ──► (5) host runtime ──► running game
       │                  in containers         (consumes plan,
 (3) ID stamping ─────────────────────────────►  binds by ID)
                                                     ▲
                                                     │
                          (6) runtime object-model services
                          (built-in components, global lookups,
                           scheduler lifetime, cross-domain policy)
```

### Piece 1 — Transpiler runtime contract (`runtime_contract.py`)

**Module shape:**
- A `ModuleScript` that `return`s a class table.
- **No side-effects at module scope** — no top-level lifecycle call,
  `Heartbeat:Connect`, or state push.
- **Pure constructor** `new(config) -> instance`. `config` carries
  Inspector serialized scalars. `new` must not touch peer components or
  the DataModel — purity is what makes reference-cycle injection safe.
- Optional lifecycle methods: `Awake / OnEnable / Start (self)`,
  `Update / FixedUpdate / LateUpdate (self, dt)`,
  `OnDisable / OnDestroy (self)`.

**Injected host surface** (set on the instance by the host before `Awake`):

- `self.gameObject` — the Roblox instance for this component's GameObject.
- `self.transform` — same instance (Roblox has no separate Transform).
- `self.instance` — alias for raw-Instance code.
- `self.enabled` — per-component flag; writes fire `OnEnable`/`OnDisable`.
- `self:GetComponent(name)` — peer-component lookup via the host's
  component table. **Built-in fallback:** if `name` is not a converted
  MonoBehaviour (e.g. `Rigidbody`, `Collider`, `AudioSource`, `Animator`),
  falls through to a Roblox class search on `self.gameObject`. Piece 6.
- `self.host` — engine handle:
  - `host.instantiatePrefab(prefab_id, parent, cframe, externalRefs?)` —
    replaces `Instantiate`; clones a template by stable `prefab_id` and
    runs its lifecycle.
  - `host.addComponent(go, scriptId, config?)` — replaces `AddComponent<T>()`.
    Piece 6.
  - `host.destroy(target)` — DFS, runs `OnDisable→OnDestroy` deepest-first.
    Piece 6.
  - `host.setActive(gameObject, bool)` — toggles `activeInHierarchy` and
    fires `OnEnable`/`OnDisable` down the subtree.
  - `host.findObjectOfType(name)` / `host.findGameObject(name)` /
    `host.findGameObjectsWithTag(tag)` — global lookup against host
    registry; sees inactive objects. Piece 6.
  - `host.invoke(self, method, delay)` / `invokeRepeating(...)` /
    `cancelInvoke(self, method?)` / `startCoroutine(self, fn)` —
    lifecycle-scoped; cancels on `OnDestroy`. Piece 6.
  - `host.connect(self, signal, fn)` — lifecycle-scoped event wiring.
    Dispatch is gated on `active && enabled` (same condition as
    `OnEnable`); the host rearms on the gate's false→true transition
    and disconnects on `OnDestroy`. Call site: `Awake` only — the
    wrapper handles the enable cycling, so re-registering in
    `OnEnable` would duplicate. Required for Unity-message-callback
    wiring (rule f). Piece 6.

**Non-scalar serialized fields.** `config` carries only scalars. Asset /
prefab / ScriptableObject fields are `references` rows (Piece 2); the
host injects a resolved value before `Awake`, keyed by `target_kind`:

- `target_kind: asset` — host injects the `rbxassetid://…` string. The
  planner persists the Unity GUID structurally; the post-upload subphase
  `_rewrite_scene_runtime_asset_refs` rewrites every asset `target_ref`
  to its `rbxassetid://…` using `ctx.uploaded_assets` (mirrors how mesh
  ids work today). Refs whose GUID is unresolved keep the raw GUID so
  the operator sees the unresolved reference rather than a silent drop.
  Under generic, the serialized-field extractor does **not** materialize
  child `Sound`/`Model` instances (`runtime_mode`-gated; legacy unchanged).
- `target_kind: prefab` — host injects the stable `prefab_id` in
  `scene_runtime.prefabs`; modules pass it to `instantiatePrefab(...)`.
  Never a bare `Clone()`. The entrypoint resolves `prefab_id` →
  `template_name` via `plan.prefabs[prefab_id].template_name`, then
  looks the bare name up under `ReplicatedStorage.Templates` (the folder
  `prefab_packages` emits into).
- `target_kind: scriptable_object` — host injects the `require()`d SO
  table; SO converter unchanged. The planner persists the Unity GUID;
  the artifact also carries a `scene_runtime.scriptable_objects`
  map (`guid → dotted DataModel module path`) populated by
  `_build_scriptable_object_module_map`. The host runtime resolves each
  SO ref via that map.

**Singleton pattern (`static Instance = this`) is supported natively.**
The Lua equivalent (`function X:Awake() X.Instance = self end`) is inside
a function body, not module-scope; the verifier accepts it.

**Prompt change is additive.** `_AI_SYSTEM_PROMPT`'s sha256 is a
transpile-cache key — editing it would silently re-transpile every legacy
project. PR3a adds a *second* prompt, `_GENERIC_RUNTIME_PROMPT`, selected
only under `--scene-runtime=generic`. Independent cache namespaces for
free (prompt hash is already part of the cache key).

**Generic transpilation is a full per-script switch.** A generic-mode
script needs **both** the generic prompt **and** a `ModuleScript` target,
or the system prompt says "return a class table" while the user message
says "Target: Server Script". PR3a:

1. Threads a `runtime_mode` argument from the transpiler's public
   boundary (`code_transpiler.transpile_scripts`) down to prompt selection
   and cache key.
2. Identifies runtime-bearing MonoBehaviours pre-transpile and sets their
   transpile-time type to `ModuleScript` and prompt to
   `_GENERIC_RUNTIME_PROMPT`.

The runtime-bearing predicate is "attached to a scene GameObject **or** a
prefab" — computed once over the union of all scenes + prefabs in the PR1
planner data. Prefab-only behaviours must be included or
`instantiatePrefab` can't drive their clones.

**Generic post-transpile is an allowlist.** The legacy pipeline runs a
large repair layer (`shared_state_linter`, `fix_require_classifications`,
`_guard_client_code_in_modules`, `script_coherence_packs`,
`_subphase_patch_setup_sounds`) to fix legacy-shaped AI output — problems
the contract prevents by construction. Enumerating them to skip is
whack-a-mole. Generic **inverts the default**: only an explicit allowlist
runs:

- **Asset-reference rewriting** — raw Unity paths → `rbxassetid://…`.
- **Module-require-path resolution.** Project-class imports are
  code-level `require`s, not component references. The prompt teaches
  `local X = require("@scene_runtime/<stem>")`; a generic pass resolves
  each `<stem>` against the planner graph. Ambiguous stem or
  non-ModuleScript target → fail closed. PR1 strengthens `_RE_CLASS` for
  base-less classes and builds the planner's **stem-keyed** require graph
  so helpers without `class_name` still resolve.
- **Contract verifier** — final step before host emit.

All legacy repair passes are **off**. The discipline extends to
`write_output`'s emit-time subphases (`_bind_scripts_to_parts`,
`ClientBootstrap` injection, autogen, prefab-template attachment,
runtime-library injection, `_subphase_patch_setup_sounds`) — all off. The
host runtime replaces them wholesale; the only library injected is
`runtime/scene_runtime.luau`.

**The allowlist is a permanent deterministic lowering layer — not "zero
post-transpile transforms."** Asset-reference rewriting and
module-require-path resolution both transform emitted module source; they
are allowlisted because they are *deterministic, spec-owned lowering*, not
*ad-hoc identity-gated repair*. That distinction — **not** "does it touch
emitted Luau" — is the boundary generic enforces:

- **Coherence packs** (`script_coherence_packs.py`, `run_packs()`):
  ad-hoc, often per-game identity-gated (`s.name == "Door"`), regex
  surgery over AI output. This is the mechanism generic eliminates.
- **Lowering passes** (the allowlist): deterministic, generic
  (structural-fingerprint-gated, never per-game), spec-owned transforms
  that bridge a *stable, statically-recognizable* Unity→Roblox primitive.
  This layer is **permanent and expected to grow modestly.**

Generic retires the *coherence-pack mechanism*, not all post-transpile
fidelity logic. There is no behavioral oracle (the verifier is structural
only — §"verify + reprompt"), and some semantic-fidelity gaps are
statically recognizable but **not preventable by prompt + structural
verifier alone** (prompt teaching is a hint, not enforcement). Those gaps
get a deterministic home — a lowering pass and/or a host-runtime service —
never a per-game pack. Planned additions to this layer: `unity_instantiate`
object-ref lowering (see `FUTURE_IMPROVEMENTS.md`) and the camera-facet
lowering that backs the camera/input runtime service (PR5, above).
A new allowlist entry must clear the bar: **deterministic; generic — gated
on the STRUCTURE of a stable Unity→Roblox primitive (a single canonical
token the prompt emits, e.g. `rbxassetid://` / `require("@scene_runtime/…")`,
or a multi-signal structural fingerprint of a primitive's shape), NEVER on
script identity (`s.name`); and the transform must be a faithful lowering of
that primitive, not output-spelling chasing.** The distinguishing line from
a coherence pack is *structure-gated vs identity-gated*, not "touches
emitted Luau." If an entry can only be expressed as per-game,
identity-gated repair, it is a pack and does not belong in generic.

**Enforcement = verify + reprompt + fail-closed.** No mechanical
relocation of module-scope side-effects (that's semantic rewriting).
Instead `runtime_contract.py`:

1. **Structurally verifies** with a conservative lexical check. Flags:
   - (a) a top-level statement that is not a comment, a bare `function`,
     the final table return, or a side-effect-free `local`;
   - (b) a top-level `local x = EXPR` whose `EXPR` is not side-effect-free
     (side-effect-free = literal, function definition, contract-compliant
     `require`, or table constructor whose every value is recursively
     side-effect-free — `local t = {c = …:Connect(…)}` is a violation);
   - (c) a lifecycle key assigned a non-function value;
   - (d) missing top-level table return;
   - (e) **constructor-purity** — `new(...)` body reading `self.host` or
     calling `self:GetComponent` (the host surface isn't bound until
     after `new()` returns; access in `new` breaks reference-cycle
     injection).
   - (f) **Unity-message-callback binding on the class table** — any
     binding of a Unity-message-callback name on the class table, in
     any syntactic shape: `function Class:Name(...)`, `Class.Name =
     function(...)`, or a `Name = function(...)` entry inside the
     returned table-literal. Names: `OnTriggerEnter / Exit / Stay`,
     `OnCollisionEnter / Exit / Stay`, `OnMouseDown / Up / Enter /
     Exit / Over / Drag`. Roblox does not name-dispatch these, and
     raw `:Connect`-wired equivalents don't preserve Unity's
     enable/disable/teardown semantics. The AI must wire them via
     `host:connect(signal, fn)` in `Awake` (Piece 6). Reprompt teaches
     the pattern; still-failing fails closed to `legacy`.

   Best-effort backstop, not a security boundary. Over-rejects pure
   top-level calls; reprompt resolves those.
2. On failure, **reprompts** the AI once with the specific violation.
3. On still-failing, **fails closed**: the whole project converts under
   `legacy`. Modes never share an output dir (Piece 4), so the fallback
   is a clean legacy conversion, not salvage. Per-module hybrid fallback
   is deferred (NOT in scope).

### Piece 2 — `scene_runtime_planner.py`

A deterministic artifact built from parsed Unity scenes. Reuses
`serialized_field_extractor`'s reference-walking primitives but emits
per-instance data (not its first-seen-wins aggregate).

**Output — `scene_runtime` key in `conversion_plan.json`:**

```
scene_runtime:
  modules:                                     # keyed by script_id
    "<script_id>":
      stem:        "<emitted file stem>"        # canonical require key
      class_name:  "<C# class name>"            # informational; may be empty
      module_path: "<dotted DataModel path>"    # e.g. "ReplicatedStorage.Foo"
                                                # OR "StarterPlayer.StarterPlayerScripts.Bar".
                                                # The SceneRuntime entrypoints split this on "."
                                                # and walk game:FindFirstChild() down the chain.
                                                # R2-P1.1 contract resolution.
      runtime_bearing: bool                     # true = host instantiates
      domain:    "client" | "server"            # Piece 4; runtime_bearing only
      container: "<Roblox container path>"      # Piece 4
  scenes:
    "<scene_path>":
      instances:
        - instance_id:    "<scene>:<component_fileID>"
          script_id:      "<canonical script id>"
          game_object_id: "<scene>:<gameobject_fileID>"
          active:  bool          # GameObject m_IsActive
          enabled: bool          # Behaviour m_Enabled
          config:  { field: literal_value }
      references:
        - from: "<instance_id>"
          field: "<field>"
          index: <int|null>
          target_kind: "component" | "gameobject" | "prefab"
                     | "scriptable_object" | "asset"
          target_ref:  "<instance_id | game_object_id | prefab_id | asset_id>"
                       # For asset: GUID at plan time, rewritten to
                       # rbxassetid://NNN by _rewrite_scene_runtime_asset_refs
                       # before the plan ModuleScript is emitted. R2-P1.3.
                       # For scriptable_object: Unity GUID; the host resolves
                       # via scene_runtime.scriptable_objects below.
          target_is_ui: bool      # target inside a converted Canvas subtree
      lifecycle_order: ["<instance_id>", ...]   # scene-hierarchy DFS
  prefabs:                                      # per-template namespace
    "<prefab_id>":                              # STABLE ID — .prefab GUID +
                                                # project-relative path,
                                                # NOT bare name (collides
                                                # across folders like stems)
      name:           "<prefab name>"           # informational
      template_name:  "<bare prefab name>"      # R2-P1.2: resolves prefab_id ->
                                                # ReplicatedStorage.Templates[name].
                                                # Equals PrefabTemplate.name; the per-folder
                                                # distinction lives in prefab_id (above).
                                                # The entrypoints' _resolveTemplate(prefab_id)
                                                # reads this to clone the right template.
      instances:      [ ... ]                   # prefab-local
      references:     [ ... ]                   # intra-prefab + externalRefs
      lifecycle_order: [ ... ]                  # prefab-local DFS
  scriptable_objects:                           # R2-P1.3: guid -> dotted DataModel
    "<unity_guid>": "<dotted module path>"      # module path (e.g.
                                                # "ReplicatedStorage.Settings").
                                                # Populated by
                                                # _build_scriptable_object_module_map
                                                # before the plan is encoded.
                                                # The host runtime resolves every
                                                # scriptable_object ref through this map.
  domain_overrides:                             # agent overrides, sub-key of
    "<script_id>": "client" | "server"          # scene_runtime (NOT top-level)
```

**`domain_overrides` lives inside `scene_runtime`** — not as a top-level
sibling. `_classify_storage` does a wholesale rewrite of
`conversion_plan.json` with a fixed 4-key dict; PR1 adds a merge that
preserves `scene_runtime`. Sticky `domain_overrides` survives verbatim;
recomputed `domain`/`container` refresh. A top-level sibling key would be
silently dropped on every `assemble`/resume.

**`script_id` is canonical** (`.cs` GUID or project-relative source path)
because emitted module names collide on stem. `module_path` carries the
unique DataModel path for **every** module in the generic require graph
— runtime-bearing MonoBehaviours *and* plain helper classes. Helpers get
`runtime_bearing: false`, no domain, but a `module_path` so
`require("@scene_runtime/<stem>")` resolves. A helper that
`_classify_script_type` would type `LocalScript` (UI/input-flavored) is
force-typed `ModuleScript` under generic; if that retype is unsafe the
project fails closed.

**Stem collisions fail closed.** Physical script identity today is the
emitted stem; two scripts sharing a stem overwrite each other on disk.
If the planner detects any two scripts in the generic require graph
sharing an emitted stem — runtime-bearing or helper — the project fails
closed to `legacy`. Project-wide stem-collision cleanup is a separate
effort (NOT in scope).

**Require graph is stem-keyed.** PR1 strengthens `_RE_CLASS` for
base-less classes and builds the planner's require graph keyed by emitted
stem (not `class_name`), so helpers without `class_name` are still nodes.
The legacy `class_name`-keyed graph stays untouched (it feeds only legacy
prompt context).

**Phase placement.** PR1 adds a `plan_scene_runtime` phase that runs
after all scenes + prefabs are parsed and before `transpile_scripts`, in
both the single- and multi-scene drivers, and writes the one
project-level `scene_runtime` artifact.

**Why `domain` is per-module.** A transpiled class is packaged as one
physical ModuleScript in one container. Unity has no per-instance
client/server concept; a per-instance domain would have no source of
truth. Runtime-spawned prefabs are the one exception — execution side is
decided by which runtime calls `instantiatePrefab` — and the host
handles that naturally.

### Piece 3 — Unity ID stamping (`scene_converter.py`)

The host runtime binds plan entries to Roblox instances by ID. Stamp a
`_SceneRuntimeId` attribute (`<scene>:<gameobject_fileID>`) on **only the
logical GameObject host** — never on every converted instance. Wrapped
geometry copies the same Unity fileID onto both outer Model and inner
`*_Mesh` child; stamping both would make lookup ambiguous. The existing
`unity_file_id` writer path is **left intact** — `_SceneRuntimeId` is
additive.

**UI targets need the same ID path.** A serialized field can point at a
UI element. PR2 stamps `_SceneRuntimeId` on converted UI instances — the
`ScreenGui` root and each descendant `RbxUIElement` — and **both**
writers (`rbxlx_writer`, `luau_place_builder`) gain UI-attribute
emission.

**Inactive objects must survive conversion under `generic`.** The
lifecycle model has `active: bool` per instance and `host.setActive(go,
true)` to activate later. Current converters prune inactive objects.
Under `generic`, the scene and UI converters **emit** inactive objects
when runtime-bearing or referenced — in a dormant state (parented under
an inactive holder / `Enabled = false`), stamped with `_SceneRuntimeId`,
with `active: false` in the plan. Pruning stays default for inactive
objects that are neither runtime-bearing nor referenced. `runtime_mode`-
gated; legacy unchanged.

### Piece 4 — Module packaging + execution-domain classification

Two sub-concerns mapping to the PR3a/PR3b split:

- **Execution partitioning** (`domain`) → PR3b
- **Packaging/isolation** (`container` + mode-stamped output + scene
  converter changes) → PR3b

Runtime-bearing MonoBehaviours are transpiled directly as `ModuleScript`
under generic (pre-transpile, Piece 1). This step assigns **placement**:

- **`container` and `domain` are separate decisions.**
  - **`container`** (file placement) — from the storage classifier as
    today. Never client-only; modules are not `LocalScript`s.
  - **`domain`** (which runtime instantiates) — new, post-transpile,
    drawing on two signals:
    1. **API-surface scan on a new generic-only classifier table.** The
       legacy pattern tables in `storage_classifier` and
       `script_coherence` are near-duplicates and miss signals this
       converter emits (`RenderStepped`, `:FireServer(`, `.OnClientEvent`,
       `game.Workspace.CurrentCamera`, `StarterGui` variants). PR3b
       defines a new table for the domain detector only; legacy tables
       stay byte-frozen.
    2. **Wired-reference UI signal.** A module whose serialized
       references resolve into a converted Canvas/UI subtree is a UI
       controller → `client`. The planner stamps `target_is_ui: bool`
       on each reference row (per-instance, per-field); the class
       verdict aggregates over instances — any UI-bearing ref in any
       instance contributes the signal.
  - Two values, no `shared`:
    - `client` — touches client-only API or drives UI.
    - `server` — touches server-only API, or touches neither and drives
      no UI (authoritative default — Roblox gameplay is
      server-authoritative; one side avoids double-execution).
      Neither-signal modules are flagged in the report as
      low-confidence for agent review.
  - **No `shared` execution domain.** Running on both double-executes
    ordinary components; `shared` describes only `container =
    ReplicatedStorage`. A both-side-API module is a contract conflict
    → `legacy`.
  - **Reachability rule.** A client-domain module's transitive require
    graph must not reach `ServerStorage`. Helpers required by client
    modules are forced to `ReplicatedStorage`; conflicts (same helper
    required by both sides and the classifier wants `ServerStorage`)
    fail closed to `legacy`.
  - **Intra-class instance-domain conflict.** A transpiled class is one
    ModuleScript in one container — one `domain` — but its instances
    live in different scene contexts. When instances of the same
    `script_id` produce conflicting per-instance evidence (some carry
    UI-bearing refs, others have none and host on GameObjects in no UI
    subtree) **and** the API-surface scan doesn't pin the class to one
    side, the class is multi-context. Without `domain_overrides` →
    fail closed to `legacy`. With override → honor it, and list the
    displaced-side instances in the conversion-time report so the
    operator sees which instances won't execute their lifecycle. (Dual
    to both-side-API: that rule is code disagreeing with itself; this
    is instances disagreeing about the same code.)
- **Mode is part of output-directory identity.** A generic run differs
  from legacy on three persisted surfaces (`storage_plan`, `.luau`
  bodies, `conversion_plan.json`); per-surface isolation is whack-a-mole.
  Instead the requested `--scene-runtime` mode is recorded in a sidecar
  stamp at the output root: `output/.scene-runtime-mode` (plain text,
  *not* under `scripts/`, because rehydration globs `scripts/**/*.luau`).
  At **CLI entry**, before any phase runs, the requested mode is compared
  to the stamp; on mismatch the run refuses to proceed incrementally.
  Guard installed at every front door (`u2r.py` convert/publish/eval,
  `convert_interactive.py` assemble/upload). PR3b adds a `--clean` flag
  for the remediation.
- `domain` is overridable per module via `scene_runtime.domain_overrides`
  (`script_id → domain`) — **not** `storage_overrides` (which is
  `script_name → container` and can't express execution side).
- `_bind_scripts_to_parts` is bypassed for contract-compliant modules —
  the host injects `self.gameObject`.

### Piece 5 — Host runtime + thin entrypoints (`autogen.py` + a runtime module)

- `runtime/scene_runtime.luau` — game-agnostic engine: reads the plan,
  `require`s modules, calls `new(config)`, injects host surface +
  references, runs the lifecycle, drives update loops, exposes
  `instantiatePrefab`, etc. (Piece 6 covers the service surface.)
- `SceneRuntimeClient` (LocalScript, StarterPlayerScripts) and
  `SceneRuntimeServer` (Script, ServerScriptService) — thin entrypoints
  that `require` the engine, filter the plan by `domain`, and start it.
  They expose two resolver closures the engine reads through `services`:
  - `resolveModule(scriptId, modulePath)` — splits `modulePath` on `"."`
    and walks `game:FindFirstChild(...)` down the chain to load the
    ModuleScript. The planner stamps `modulePath` as the dotted
    DataModel path (R2-P1.1).
  - `clonePrefabTemplate(prefabId, parent, cframe)` — looks up the
    plan's `prefabs[prefabId].template_name` and clones
    `ReplicatedStorage.Templates[template_name]` (R2-P1.2). The
    `Templates` folder is what `prefab_packages` actually emits;
    feeding the stable `prefabId` directly would never resolve because
    templates are keyed by bare name.
- The plan is embedded once as a ReplicatedStorage data ModuleScript,
  generated from `conversion_plan.json` at `write_output`. The encoder
  emits the host-relevant keys only:
  `modules / scenes / prefabs / domain_overrides / scriptable_objects`.

These replace `ClientBootstrap`. `runtime/scene_runtime.luau` is reusable
and never modified per-game.

### Piece 6 — Runtime object-model services

Game-agnostic services the host runtime provides to a contract-compliant
module. Lives inside `runtime/scene_runtime.luau` (Piece 5's home) but
named separately so the surfaces are reviewable.

**Component registry.** A live `<gameObject_id, components[]>` table the
host maintains; mutated by `instantiatePrefab` / `addComponent` /
`destroy`. `self:GetComponent(name)` is a registry lookup with a
Roblox-instance fallback for built-in types (`Rigidbody` / `Collider` /
`AudioSource` / `Animator` / …) — `self.gameObject:FindFirstChildWhichIsA(...)`
preserves today's child-search lowering for those types only.

**Dynamic component creation.** `host.addComponent(go, scriptId,
config?)` covers `AddComponent<T>()` — instantiates a `runtime_bearing`
MonoBehaviour at runtime, registers it, injects host surface +
references, runs `Awake → OnEnable → Start`. Returns the instance.

**Global lookup registry.** `host.findObjectOfType(name)`,
`host.findGameObject(name)`, `host.findGameObjectsWithTag(tag)` —
host-maintained by-class / by-name / by-tag index, seeded from the plan
and updated on `instantiatePrefab` / `destroy` / `addComponent`. **Sees
inactive objects** (which under generic exist as dormant Roblox
instances — see Piece 3), which `workspace:FindFirstChild` cannot.

**Lifecycle-scoped scheduler.** `host.invoke / invokeRepeating /
cancelInvoke / startCoroutine`. The host tracks task handles per-component
and cancels all of them on the owning component's `OnDestroy` — the
lifetime coupling Unity's `MonoBehaviour.Invoke` provides natively, which
raw `task.delay` / `task.spawn` lowerings do not.

**Lifecycle-scoped event wiring.** `host.connect(self, signal, fn)` is
the event-subscription analog of `host.invoke` — the lifecycle
coupling Unity's component-dispatch of
`OnTriggerEnter`/`OnCollisionEnter`/etc. provides natively, which raw
`signal:Connect(...)` calls do not. The host keeps a per-component
subscription table; dispatch is gated on `active && enabled` (same
condition as `OnEnable`). Gate true → reconnects with the same `fn`;
gate false → disconnects the `RBXScriptConnection`; `OnDestroy`
disconnects all subscriptions and discards the table. Call site is
`Awake` (one-time registration; the wrapper handles cycling).
Raw `signal:Connect(...)` remains available for cases that want
connection-survives-disable semantics, but rule (f) forbids it for
Unity-message-callback names.

**Recursive teardown.** `host.destroy(target)` walks the subtree DFS,
runs `OnDisable → OnDestroy` deepest-first, unregisters from registries,
removes the instance. Both lifecycle methods are guaranteed-once and
skipped if `OnEnable` never ran. Idempotent.

**Cross-domain reference policy.** When the host walks a reference and
the source module's `domain` differs from the target's:

- **v1 policy: fail loudly.** Inject `nil`, log a structured warning,
  append a `UNCONVERTED.md` entry (`<from_class>.<field> → <to_class>`).
  Any deref crashes loudly rather than silently misbehaving.
- **Conversion-time report.** PR4's `write_output` walks the plan +
  module table and emits a cross-domain edge count + per-edge listing
  in the conversion summary — operator sees the boundary before runtime.
- **Brokering is deferred** (RemoteEvent stubs for fire-and-forget,
  attribute-mirror for property reads) — see NOT in scope. Adding
  `target_domain` per reference row is a non-breaking later schema
  extension.

**Plan size / sharding.** The plan is one ReplicatedStorage ModuleScript
today. If a scene's subplan exceeds Roblox's per-script budget, Piece 6
shards by scene namespace under a `ScenePlans` folder. PR1's per-scene
namespacing already enables this; PR4 implements the loader.

**Singleton / static fields.** No special host machinery — handled by
the contract directly (Piece 1).

## Lifecycle execution order (converter-defined, deterministic)

Unity's order is partly undefined; the converter pins it:

1. `new(config)` for **all** instances (constructors are pure — no peer
   access).
2. Inject host surface (`self.gameObject` …) and **all** references.
   Cycles are safe because step 1 touched nothing.
3. `Awake` — scene-hierarchy DFS, then per-GameObject component order.
4. `OnEnable` — same order, **only** `active + enabled` instances.
5. `Start` — **next tick** (`task.defer`), same order. `Start` fires
   **once** on the FIRST satisfaction of the `active && enabled` gate
   — at boot for live instances, OR later via `setActive(true)` /
   `setEnabled(true)` for instances booted dormant (R2-P1.5). Subsequent
   true→false→true cycles do NOT re-fire `Start`.
6. `Update`/`LateUpdate` on `Heartbeat`; `FixedUpdate` on a **fixed-step
   accumulator** (Unity's `FixedUpdate` is fixed-timestep).
7. `OnDisable`/`OnDestroy` on teardown / `SetActive(false)` / `Destroy`.
   `setActive(go, bool)` cascades to every component owned by a
   descendant of `go` (DFS via `services.collectDescendantIds`), so a
   parent toggled inactive suspends every descendant component's
   `host.connect` subscriptions and bookkeeping. Matches Unity's
   `activeInHierarchy` semantic (R2-P1.4).

## Runtime-spawned prefabs

The planner emits a **prefab-local runtime subplan** per template,
persisted under `scene_runtime.prefabs` — `instances`, `references`,
`lifecycle_order` in the same shape as a scene, but prefab-local. The
host's `instantiatePrefab(prefab_id, parent, cframe, externalRefs?)`
looks the template up by stable `prefab_id`, clones it, builds component
instances for the clone, injects intra-clone references +
caller-supplied externals, runs `Awake → OnEnable → Start`. The AI never
constructs `prefab_id`; it passes through the injected value. Emitting
the subplan is PR1; host-side consumption is PR4.

## Multi-scene projects

The pipeline already converts multi-scene projects into **one `.rbxlx`
per scene** and treats `SceneManager.LoadScene` as unsupported. The
generic runtime keeps the boundary: `scene_runtime` is per-scene
namespaced; at `write_output` each emitted place embeds only its own
scene's subplan. No "active scene selection"; the embedded scene *is*
the place. `lifecycle_order` runs once at place load. In-place scene
transitions stay unsupported (surfaced to `UNCONVERTED.md`).

## Mode semantics (`legacy` / `auto` / `generic`)

Set per conversion via `--scene-runtime`:

- **`legacy`** — pre-contract pipeline. System prompt byte-frozen,
  post-transpile repair layer runs, host runtime not emitted. Default
  until PR7; remains as escape hatch after.
- **`generic`** — contract-only path. Generic prompt selected, contract
  verifier enforced, host runtime emitted, all legacy repair passes
  off. On any **fail-closed trigger** (verifier still failing after one
  reprompt; stem collision; both-side-API class; client-domain require
  graph reaches `ServerStorage`; intra-class instance-domain conflict
  without override; unsafe `LocalScript` retype of a helper), the run
  rejects with a diagnostic — never silently downgrades.
- **`auto`** — try `generic`; on any fail-closed trigger, fall back to
  clean `legacy` (mode-isolated output dir, no salvage) and record the
  trigger in the conversion summary.

**`auto`'s fallback boundary is converter-completion, not playability.**
A successful `generic` conversion under `auto` may still degrade
playability through three vectors the converter cannot detect at
build time:

1. **Nil-injected cross-domain refs** — Piece 6's v1 policy. Operator
   sees the count + per-edge listing in the conversion-time report;
   each appears in `UNCONVERTED.md`. Not a fallback trigger.
2. **Low-confidence single-side classifications** — neither-signal
   modules default to `server` and are flagged in the report (Piece 4).
   Multi-context classes with `domain_overrides` resolution carry a
   displaced-instance listing. Not a fallback trigger; agent review.
3. **Unity message callbacks** — closed at converter time by rule (f)
   + `host.connect` (Piece 1, Piece 6): the verifier rejects every
   class-table binding shape of `OnTriggerEnter`/`OnCollisionEnter`/etc.
   and the AI is reprompted to wire via `host:connect` in `Awake`,
   which preserves Unity's enable/disable/teardown semantics. PR5
   includes a trigger/collision canary against AI emission drift.

Vectors 1–2 are visible in the report / `UNCONVERTED.md` but don't
trigger fallback. A successful-generic conversion is by construction
contract-compliant and structurally correct; playability degradation is
a reportable artifact, not a converter failure. Operators wanting hard
guarantees against any degradation use `--scene-runtime=legacy`.

**PR6's guard rail is a converter-completion check**, not a playability
check. It fires when runtime-bearing MonoBehaviours exist with no valid
plan+host — i.e., when the converter itself emitted broken output. The
PR6→PR7 cut point is defensible because:

- Under `auto`, every fail-closed trigger routes to clean `legacy`.
- Under `auto`, every successful-generic run passes PR6's guard.
- Playability degradation is reported, not silent.
- `--scene-runtime=legacy` is always available as a per-run override.

## PR breakdown — 9 PRs, flag-gated

`--scene-runtime=legacy|auto|generic` gates the rollout (semantics
above). `legacy` default until PR7. PR1, PR2, PR3a are independently
landable (inert by default); PR3b onward form a chain.

| PR | Scope | Depends on | Risk |
|----|-------|-----------|------|
| PR1 | New `plan_scene_runtime` phase + `scene_runtime_planner.py`; `scene_runtime` artifact (per-module `modules`; per-scene `instances`/`references` incl. `target_is_ui`; per-prefab subplan **keyed by stable `prefab_id`**, NOT bare name; runtime-bearing set covers prefab-attached MonoBehaviours) with persistence (merge in `_classify_storage` preserving sticky `domain_overrides` vs recomputed `domain`/`container`). Strengthen `_RE_CLASS` for base-less classes; build stem-keyed require graph. Schema commits to **no `target_domain` per reference** (host-at-start enforcement — Piece 6); annotation remains a non-breaking later extension. | — | Med |
| PR2 | `_SceneRuntimeId` stamping on logical GameObject hosts **and** UI instances (`ScreenGui` + descendant `RbxUIElement`s); UI-attribute emission in `rbxlx_writer` + `luau_place_builder`; round-trip both writers; `unity_file_id` path untouched. | — | Med |
| PR3a | Runtime **contract**: additive `_GENERIC_RUNTIME_PROMPT` (legacy prompt byte-unchanged) covering full host surface (Pieces 1 + 6); `runtime_mode` threaded through transpiler; pre-transpile `ModuleScript` target switch for runtime-bearing MonoBehaviours; lexical verifier (incl. constructor-purity rule e) + reprompt + fail-closed; generic allowlist (asset-rewrite + stem-keyed require resolution + verifier; all legacy repair passes off, incl. `write_output` emit-time subphases). `--scene-runtime` flag at every front door; `generic` rejected at CLI until PR4. Default output byte-identical. Ends with **compliance spike** — trash-dash + SimpleFPS verifier pass rate (pre/post-reprompt) recorded; PR3b/PR4 don't start until rate clears agreed threshold. | PR1 | High |
| PR3b | Packaging + execution partitioning (Piece 4's two sub-concerns): domain classifier (new generic-only table; per-instance UI-reference signal aggregation) → fills `scene_runtime.modules`; **reachability rule** (client require graph must not reach `ServerStorage`); **intra-class instance-domain-conflict fail-closed** (multi-context class without `domain_overrides` → legacy; with override → honor + emit displaced-instance report); `.scene-runtime-mode` stamp + mismatch guard at all front doors; `--clean` remediation flag; generic-only `scene_converter`/`ui_translator` changes (inactive retention, asset/prefab serialized-field child suppression). | PR3a (incl. passing gate) | Med |
| PR4 | Host runtime (`runtime/scene_runtime.luau` + `SceneRuntimeClient/Server`) + Piece 6: component registry (with built-in fallback), `instantiatePrefab`, `addComponent`, global lookup registry, lifecycle-scoped scheduler, **lifecycle-scoped event wiring (`host.connect`)**, recursive destroy, cross-domain policy (v1: nil + log + UNCONVERTED.md), conversion-time cross-domain edge report, plan-sharding loader if needed. | PR3b | Med |
| PR5 | `auto` mode + canary: trash-dash (state-machine, ~12 cross-domain edges expected) + SimpleFPS (~6–8 edges) + one cross-domain-heavy project (UI-controller / server-gameplay split — stresses Piece 6 policy + report) + one trigger/collision-driven project (regresses rule-(f) emission and the `host:connect`-in-`Awake` pattern, per Mode-semantics vector 3) + one multi-scene project under `generic`, MCP-verify, compare against legacy. **Includes the minimal first-person camera/input runtime service + camera-facet lowering pass** — a hard prerequisite of the PR5→PR7 "play correctly" gate (SimpleFPS cannot yaw/pitch under generic without it). Scope-capped (see "Camera / input" under NOT in scope). | PR4 | Med |
| PR6 | **Hard guard rail (lands before default flip):** `write_output` fails if runtime-bearing MonoBehaviours exist with no valid plan+host — generic/auto only; `--allow-nonplayable-output` escape. | PR5 | Low |
| PR7 | **Flip default to `auto`:** legacy retained as per-run escape hatch. PR6 lands first so the guard rail protects the default before it flips. | PR6 | Med |
| PR8 | **Retirement only** (the camera/input service already shipped in PR5, so generic SimpleFPS stays playable across the cut): retire FPS scaffolding + dependency web — `--scaffolding=fps`, `converter/scaffolding/`, `detect_fps_game`, `is_fps_game` and its `LockFirstPerson` camera wiring, and the four legacy FPS coherence packs (`fps_camera_yaw_from_player_pivot`, `fps_camera_pitch_inversion`, `fps_default_controls_off`, `fps_e2e_mouse_channel`) whose jobs now live in the service. Rewrite skill Phases 4a/4c as structured plan overrides. | PR7 | Med |

**PR3a → PR3b/PR4 gate.** The verifier pass rate is the single biggest
unquantified risk; PR4 is the largest build. PR3a ends with a compliance
spike — trash-dash + SimpleFPS runtime-bearing MonoBehaviours transpile
under the contract; pass rate recorded pre- and post-reprompt. PR3b and
PR4 don't start until the rate clears a threshold agreed at the gate
(not pre-committed here). A low rate sends `_GENERIC_RUNTIME_PROMPT`
back for iteration. The PR3 split sharpens this: PR3a measures contract
compliance uncontaminated by the new classifier tables.

## Test matrix

- **PR1** — planner unit tests: synthetic `ParsedScene` → plan shape;
  array order preserved; local + cross-asset refs resolved; duplicate-stem
  scripts get distinct `script_id`; multi-scene namespacing. **Stable
  prefab IDs:** two prefabs sharing a bare name in different folders
  produce distinct `prefab_id`s; a `prefab` ref resolves to the correct
  one. Persistence: `scene_runtime` + nested `domain_overrides` survive a
  `_classify_storage` rewrite + resume.
- **PR2** — ID stamping: wrapped-geometry asserts no duplicate
  `_SceneRuntimeId`; round-trips both writers; `unity_file_id` byte-
  unchanged. UI: `ScreenGui` root + nested `Button` both stamped through
  both writers; value is well-formed `<scene>:<fileID>`. (Host-side
  resolution exercised in PR4.)
- **PR3a** — verifier fixtures: rejects top-level side-effects (incl.
  `local c = …:Connect(…)`, `…:Clone()`, side effects nested in top-level
  table constructors) and non-method lifecycle members; accepts the
  adapter; reprompt path exercised; fail-closed → clean legacy asserted.
  **Constructor-purity (rule e):** `new(config)` calling `self:GetComponent`
  or reading `self.host` rejected; same access in `Awake` passes.
  **Unity-message-callback rejection (rule f):** all three shapes
  rejected — `function X:OnTriggerEnter(other)`,
  `X.OnTriggerEnter = function(...)`, and a table-literal `return {
  OnTriggerEnter = function(...) ... end }` form; same logic wired via
  `self.host:connect(self.gameObject.Touched, function(...) ... end)`
  inside `Awake` passes; reprompt + fail-closed path exercised.
  **Prompt isolation:** `sha256(_AI_SYSTEM_PROMPT)` byte-unchanged from
  `main`; `_GENERIC_RUNTIME_PROMPT` + `ModuleScript` target selected only
  for runtime-bearing MonoBehaviours under generic. **Predicate:** a
  prefab-only MonoBehaviour caught by the pre-transpile switch.
  **Require resolution:** `require("@scene_runtime/<stem>")` resolves to
  the dep's `module_path`; base-less helpers resolve by stem; stem
  collisions fail closed. **Allowlist isolation:** generic module
  byte-identical from transpile→emit except asset/require rewrites; no
  legacy repair pass fired. **Compliance spike (gate):** trash-dash +
  SimpleFPS pass rates recorded.
- **PR3b** — **Domain:** client-only API → client; new generic-only
  table patterns classified, **and legacy tables assert byte-unchanged**;
  `target_is_ui` ref → client; neither-side + no UI → server (flagged
  low-confidence); both-side → conflict → legacy. **Reachability:** a
  client-domain module whose require graph reaches a `ServerStorage`-
  routed module fails closed; same graph routed through
  `ReplicatedStorage` passes. **Intra-class instance-domain conflict:**
  fixture class with two instances — one carrying a UI-bearing ref, one
  with no refs and hosted under a non-UI subtree — and neither-side
  code: without `domain_overrides` fails closed to legacy; with override
  passes and the conversion-time report enumerates the displaced-side
  instances. **Inactive objects:** inactive but runtime-referenced
  object emitted dormant with `_SceneRuntimeId`; unreferenced inactive
  still pruned; legacy pruning byte-unchanged. **Mode isolation:**
  generic-stamped dir rebuilt under legacy (and vice-versa) triggers
  the mismatch guard at every front door before `scripts_cache_intact()`.
- **PR4** — host runtime: 2-MonoBehaviour synthetic scene wired
  end-to-end; reference-cycle fixture; `instantiatePrefab` lifecycle;
  `FixedUpdate` fixed-step. **Piece 6:** `addComponent` registers + runs
  lifecycle; `findObjectOfType` returns inactive objects; `host.invoke`
  cancels on owning component's `OnDestroy`; `host.destroy(parent)` runs
  `OnDisable→OnDestroy` deepest-first, idempotent. **`GetComponent`
  fallback:** `self:GetComponent("Rigidbody")` returns the BasePart-shaped
  instance; `self:GetComponent("MyMB")` returns the peer module.
  **`host.connect` lifecycle scoping:** subscribed callback fires only
  while `active && enabled`; flipping `self.enabled = false` or
  toggling the GameObject inactive suspends dispatch; gate false→true
  reconnects with the same `fn`; `OnDestroy` disconnects all of the
  component's subscriptions. Raw `signal:Connect(...)` still permitted
  for connection-survives-disable cases.
  **Cross-domain policy:** cross-domain reference → host injects nil +
  logs + appends `UNCONVERTED.md`; same-domain → live instance.
  **Conversion-time report:** count correct on the cross-domain canary.
- **PR5** — canary: trash-dash + SimpleFPS + cross-domain canary +
  **trigger/collision-driven canary** (regresses rule-(f) emission and
  the `Awake`-wired `:Connect` pattern end-to-end; gameplay must
  observably fire on contact) + multi-scene project run under
  `generic`; conversion-time cross-domain report enumerates edges; v1
  policy fires at start. **PR5 → PR7 gate:** all five canaries must
  play correctly under `--scene-runtime=generic` (and route through
  `auto`'s generic branch with no fail-closed fallback) before PR7 is
  cut — symmetric to PR3a → PR3b/PR4. **SimpleFPS "play correctly"
  includes first-person look (yaw AND pitch), so the minimal camera/input
  service + camera-facet lowering pass is a prerequisite of THIS gate** —
  it lands in PR5, not PR8 (PR8 only retires the now-redundant legacy FPS
  machinery). Regression: legacy byte-identical to pre-PR output (legacy
  prompt + cache key untouched).
- **PR6** — guard rail fires only under `generic`/`auto`; legacy +
  resume/preserved-script unaffected; `--allow-nonplayable-output` passes.
  Verified to land **before** PR7 by running PR7's test pre-PR6 and
  asserting it would fail.
- **PR7** — no-flag conversion of trash-dash + SimpleFPS routes
  `auto → generic` (PR6 backstops); `--scene-runtime=legacy` still works.
- **PR8** — no `--scaffolding`, `detect_fps_game`, `is_fps_game`, or FPS
  coherence-pack references remain; SimpleFPS still playable via generic
  (proves FPS scaffolding was redundant), incl. first-person camera.

## What already exists (reuse, do not rebuild)

- `ComponentData` (`unity_types.py`) — direct planner input for scene MBs.
- `prefab_parser.py` — prefab-node component lists; PR1 reuses the parse.
  Prefab-variant inheritance is the one wrinkle to confirm in PR1.
- Storage classifier — **container** authority for Piece 4. Its
  client/server helpers are the structural model for the `domain` scan,
  but hardwired to the frozen legacy `_CLIENT_ONLY_PATTERNS` table — PR3b
  adds a parallel generic-only helper over the new table.
- `serialized_field_extractor.py` — reuse low-level reference-walking
  primitives, not aggregated output.
- `unity_file_id` writer path — kept; `_SceneRuntimeId` is additive.
- `code_transpiler` luau-analyze + reprompt loop — PR3a's verifier hooks
  it.
- `autogen.py` — host entrypoint scripts emitted here.
- `runtime/` — 8 game-agnostic Luau modules already; `scene_runtime.luau`
  joins this set. `instantiatePrefab` reuses `object_pool.luau` for
  clone pooling.

## NOT in scope (deferred)

- **Camera / input platform-divergence** — split. The MonoBehaviour host
  runtime ships first (Pieces 1–6) with no camera/input surface. The
  **minimal first-person camera/input fidelity service lands in PR5** — it
  is a prerequisite of the PR5→PR7 "play correctly" gate (SimpleFPS can't
  yaw under generic without it), NOT a post-PR8 deferral. Scope of the
  minimal service is capped (camera pose composition world-yaw∘local-pitch,
  pitch clamp, recoil API, default-controls-off + body-hide + spawn-snap,
  rig/viewmodel anchoring, read-only `CurrentCamera.CFrame`, the E2E mouse
  channel) — it must NOT absorb movement, weapon, or CharacterController
  logic ("owning locomotion = rebuilding Unity"). The **broader**
  camera/input config surface (full input remapping, camera modes beyond
  first-person, third-person follow) stays deferred past PR8.

  **Timing contradiction (resolved).** Earlier drafts said camera/input is
  "a generic config layer after PR8," yet the PR5→PR7 gate requires
  SimpleFPS *playable* under generic and PR8 retires the FPS scaffolding +
  FPS packs that currently provide first-person fidelity. Those cannot all
  hold: the fidelity replacement cannot land *after* the gate that already
  demands it. Since generic is intended to *replace* legacy, the resolution
  is that the minimal first-person service lands **in PR5 (before the gate)**;
  **PR8 is retirement-only** — the legacy FPS scaffolding/packs retire
  because their jobs already moved into the PR5 service.
- **A bundled Luau AST parser** — verifier uses lexical detection; AST
  would only sharpen edge cases, separate evaluation.
- **In-place scene transitions (`SceneManager.LoadScene`)** — one place
  per scene; live scene-swap stays unsupported.
- **Per-module hybrid fallback** — one failed module fails the whole
  project to legacy. Mixing host-driven + legacy-bound in one place is a
  real future option; deferred (doubles host wiring paths).
- **Project-wide stem-collision cleanup** — several legacy passes are
  keyed by stem rather than `script_id`. Generic fails closed on detected
  collision; re-keying those passes is a separate cleanup.
- **Skinned/Mecanim animation** — unchanged; surfaced to `UNCONVERTED.md`.
- **Cross-domain brokering** — v1 is fail-loudly + report; automatic
  RemoteEvent / attribute-mirror stubs are post-v1. Adding `target_domain`
  per reference is a non-breaking later schema extension.
- **Plan visualization / static-analysis tooling** — out of scope; PR4
  ships a CLI text report. Later tooling can layer on the existing plan.

## Open questions (resolved by review)

1. **Contract verification** → conservative lexical detector (rules
   a–f, including constructor-purity and Unity-message-callback
   rejection); over-rejects pure top-level calls; reprompt resolves.
   Best-effort backstop, not a security boundary. Verifier failure →
   clean legacy fallback.
2. **Execution domain** → per-module post-transpile, generic-only API
   scan + per-instance UI signal aggregated to the class. Two values:
   `client` (touches client-only API or drives UI) and `server`
   (touches server-only API or neither — authoritative default). No
   `shared` execution domain. Both-side-API and intra-class instance-
   domain conflict → fail closed to `legacy` (the latter overridable
   via `domain_overrides`). See Piece 4.
3. **Runtime-spawned prefabs** → core scope; `instantiatePrefab` host API.
4. **`Awake` ordering** → all `new()` → inject → `Awake` (DFS) →
   `OnEnable` (active + enabled) → `Start` (next tick). `FixedUpdate` =
   fixed-step accumulator.
5. **Reference cycles** → inject after all `new()`, before `Awake`. Safe
   only with the constructor-purity rule.
6. **Incrementality** → `--scene-runtime=legacy|auto|generic`; legacy
   default until PR7; guard rail (PR6) lands first, generic/auto only.
   See "Mode semantics".
7. **Cross-domain references** → host-at-start enforcement. Planner
   emits blind to domain; host applies policy at start (v1: nil + log +
   `UNCONVERTED.md`). PR4 emits conversion-time edge report. Brokering
   deferred; `target_domain` is a non-breaking later extension.
8. **Built-in Unity components in `GetComponent`** → host fallback to
   Roblox class search on `self.gameObject` for non-MonoBehaviour names.
   Piece 6.
9. **`AddComponent<T>()`** → `host.addComponent(go, scriptId, config?)`.
   Piece 6.
10. **`FindObjectOfType` / `GameObject.Find` / tag queries** → host-
    maintained global registry; sees inactive objects. Piece 6.
11. **`Invoke` / `InvokeRepeating` / coroutine lifetime** → host
    scheduler tracks per-component; cancels on `OnDestroy`. Piece 6.
12. **Recursive `host.destroy(parent)`** → DFS, `OnDisable → OnDestroy`
    deepest-first, idempotent; both skipped if `OnEnable` never ran.
    Piece 6.

## Review history

Round-by-round log:
[`scene-runtime-contract-review-log.md`](scene-runtime-contract-review-log.md).
Architecture has been stable since round 1; subsequent rounds tightened
Piece 4 (intra-class instance-domain conflict), Piece 6 (`host.connect`
lifecycle wrapper), Mode semantics (`auto`'s fallback boundary), the
PR3 split, and the PR6↔PR7 ordering.
