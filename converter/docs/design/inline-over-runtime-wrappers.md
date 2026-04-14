# Inline Translation Over Runtime Wrappers

**Status:** Adopted 2026-04-14. Applies to `converter/converter/api_mappings.py`,
`converter/converter/luau_validator.py`, and the `converter/runtime/` module set.

## Decision

When translating a Unity C# API to its Roblox Luau equivalent, we do the
translation at **transpile time** (inline substitution) rather than at **run
time** (importing a wrapper module that reimplements the Unity API on top of
Roblox).

Inline mechanisms:
 - `API_CALL_MAP` (and `LIFECYCLE_MAP`) in `api_mappings.py` — direct string
   substitutions, e.g. `Time.deltaTime → dt`, `Input.GetKey → UserInputService:IsKeyDown`.
 - `UTILITY_FUNCTIONS` in `api_mappings.py` — small Luau helpers injected into
   the script header whenever a call site appears (e.g. `inputHorizontal()`,
   `mathLerp`, `setActive`). These are compile-time templates, not runtime
   modules: the helper body is copied into the generated script, not
   `require`'d from a shared module.
 - `luau_validator.py` — regex-based post-processor that catches patterns the
   mapper leaves as markers (e.g. `-- Input.GetAxis("Horizontal")` → `inputHorizontal()`)
   and fixes Luau-specific quality issues.

## Rationale

Runtime wrappers (e.g. `Time.luau`, `Input.luau`, `MonoBehaviour.luau`) look
attractive because they let transpiled scripts keep their Unity-ish surface —
the output still says `Time.deltaTime` and `Input.GetKey(...)` and requires a
`Time` module that reimplements those APIs on top of `RunService` and
`UserInputService`. We explored this approach (`converter/converter/bridge_injector.py`
+ nine `runtime/*.luau` wrappers) and rejected it for four reasons:

1. **Runtime overhead.** Every API access goes through a module lookup and
   function call. Tiny per call, but it happens every frame in every script.
   Inline substitution lowers to a bare local variable read or a direct
   service call.

2. **Hidden Roblox APIs.** A Roblox developer who opens the converted game
   in Studio to fix a bug sees `Input.GetKey("W")` and Googles "Roblox
   Input.GetKey" — nothing, because it's our custom wrapper. They have to
   first learn the wrapper, then learn the real Roblox API underneath. Inline
   substitution emits standard Roblox code (`UserInputService:IsKeyDown(Enum.KeyCode.W)`)
   that is directly Googleable and maintainable without any Unity knowledge.

3. **Parallel API surface that drifts.** Once we ship `Input.luau`, it becomes
   a third thing that is neither Unity nor Roblox. Unity adds a new input
   method? We update the wrapper. Roblox changes `UserInputService`? We update
   the wrapper. Over time it lags behind both and becomes a maintenance burden
   that only our project has to care about.

4. **Conflict with inline mappings (double translation).** `api_mappings.py`
   already translates `Time.deltaTime → dt`. If we also ship `Time.luau`, we
   now have two systems that both want to own the translation — one resolves
   the call at compile time, the other at runtime. You have to pick one, and
   if both are active you get subtle bugs like `dt` being looked up through a
   wrapper that doesn't know about it. The inline mapper and the runtime
   wrapper are mutually exclusive designs.

## What this policy does NOT cover

The `converter/runtime/` directory is not empty after this policy. It still
holds five legitimate runtime modules that are auto-injected into converted
games:

 - `animator_runtime.luau` — Unity Animator Controller runtime (state
   machines, parameters, transitions, blend trees).
 - `nav_mesh_runtime.luau` — NavMeshAgent → `PathfindingService` runtime.
 - `event_system.luau` — `UnityEvent` wiring.
 - `physics_bridge.luau` — physics constraints that Roblox doesn't expose as
   a single call.
 - `cinemachine_runtime.luau` — Cinemachine virtual camera state.

These are kept because they implement **genuinely stateful runtime behavior
that cannot be inlined into each script**. A state machine is a per-entity
object that accumulates state across frames; trying to flatten one into
every transpiled script would either duplicate the state machine in every
script or require the scripts to share a module anyway. The same is true
for pathfinding and for Cinemachine blending. Use the runtime module set
sparingly — only when the Unity feature genuinely maps to a stateful
subsystem with no single-call Roblox equivalent.

The distinction to apply when judging a new runtime module:

 - **Can the Unity API be rewritten to a single Roblox call or a small local
   expression?** → Inline it via `api_mappings.py` or a `UTILITY_FUNCTIONS`
   helper. Do not add a runtime wrapper.
 - **Does the Unity feature require accumulating state across frames and
   events, with a non-trivial amount of logic?** → A runtime module may be
   justified. Document it in this file.

## If a Unity pattern isn't covered today

Extend the inline layer, not the runtime layer:

1. Add an entry to `API_CALL_MAP` / `LIFECYCLE_MAP` in `api_mappings.py`.
2. If the mapping needs a helper function to stay readable, add the helper to
   `UTILITY_FUNCTIONS` and point the mapping at the helper name — the
   transpiler and validator already auto-inject the helper body when it is
   referenced. See `inputHorizontal`, `inputVertical`, `getSwipe`, `setActive`,
   `mathLerp` as worked examples.
3. If the mapping needs a multi-line regex fix (e.g. yielding statements,
   property getters), add it to `luau_validator.py` in the relevant
   `_fix_*` helper.
4. Add a test under `converter/tests/` with a minimal C# or Luau input and
   an assertion on the rewritten output. See the `test_fix_input_getaxis_*`
   tests in `test_integration.py` as a template.

## History

The nine wrappers that were removed under this policy:

 - `converter/runtime/Time.luau` — API already covered by `Time.deltaTime → dt`
   and siblings.
 - `converter/runtime/Coroutine.luau` — API already covered by
   `StartCoroutine → task.spawn` and the validator's yield handling.
 - `converter/runtime/physics_queries.luau` — API already covered by
   `Physics.Raycast → workspace:Raycast` and siblings.
 - `converter/runtime/GameObjectUtil.luau` — API already covered by
   `Instantiate`, `Destroy`, `GameObject.Find`, `GameObject.FindWithTag`,
   `setActive`.
 - `converter/runtime/Input.luau` — API covered by `Input.GetKey*`,
   `Input.GetMouseButton*`, `Input.mousePosition`, plus new
   `inputHorizontal`/`inputVertical`/`getSwipe` utilities.
 - `converter/runtime/MonoBehaviour.luau` — lifecycle hooks translated
   inline via `LIFECYCLE_MAP`; no runtime base class needed.
 - `converter/runtime/StateMachine.luau` — not a Unity API; speculative
   infrastructure with no test-project callers.
 - `converter/runtime/TransformAnimator.luau` — **still present, pending
   consolidation with `animator_runtime.luau`** (see TODO.md).
 - `converter/runtime/animator_bridge.luau` — **still present, pending
   consolidation with `animator_runtime.luau`** (see TODO.md).

And the orphaned scanner:

 - `converter/converter/bridge_injector.py` — the pipeline component that
   scanned transpiled Luau for bridge usage patterns and auto-injected the
   wrapper modules. Removed because the wrappers it injected are all gone
   or about to be.
