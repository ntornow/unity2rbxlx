# PR2 follow-ups (deferred from Codex review)

Codex reviewed PR2 in two parallel passes (`codex exec` for architecture
conformance + `codex review --base origin/main` for diff bug-hunting) on
the first commit. PR2 absorbed both P1s and the actionable P2 in the
amended commit. The findings below are queued for follow-up so PR2
ships clean.

Ordered by load-bearing-ness for downstream consumers (PR3a / PR3b /
PR4).

## 1. Inactive-object retention (PR3b scope, called out by codex)

**Where:** `converter/converter/scene_converter.py` — the per-root loop
that calls `_convert_and_index`, and `converter/converter/ui_translator.py`
— `_convert_ui_element`'s `if not node.active: return None` short-circuit.

**Why:** Piece 3 of the design doc requires that "inactive objects must
survive conversion under `generic`. ... emit inactive objects when
runtime-bearing or referenced — in a dormant state (parented under an
inactive holder / `Enabled = false`), stamped with `_SceneRuntimeId`,
with `active: false` in the plan." The PR2 implementation only stamps;
the dormant-emission path is missing. Codex flagged this as a PR2 gap;
the design doc explicitly assigns it to **PR3b's row** (Piece 4's
"inactive retention" sub-concern under "generic-only `scene_converter`
/`ui_translator` changes"). Leaving it deferred matches the doc.

**Scope (in PR3b):** add a runtime-bearing-or-referenced set computed
from the scene_runtime plan, then under `runtime_mode=generic` keep
those inactive nodes alive in a dormant container with the stamp
attached. Legacy mode unchanged. Picks up the existing PR2 stamping —
no new ID schema.

## 2. PR1 ↔ PR2 namespace divergence for outside-project scenes/prefabs

**Where:** `converter/converter/scene_converter.py:227-256` —
`_scene_namespace` returns the empty string when the scene path is
outside the project root or no project root was supplied. PR1's
`scene_runtime_planner._scene_namespace` falls back to
`scene_path.as_posix()` (the absolute path) in the same situation.

**Why:** Codex flagged the absolute-path fallback in PR2 as a P2
(machine-specific roots + Windows drive-letter colons breaking the
`<scene>:<fileID>` parse). PR2 fixed it by skipping stamping rather
than emitting an unstable value. PR1's planner still emits the
absolute path into its artifact JSON. Result: a scene outside the
project root produces a PR1 plan entry with an absolute-path key
but no PR2 stamp on the Roblox instance — PR4's host-runtime lookup
won't find it. In practice every scene lives under `Assets/` so this
never fires; still worth aligning the two helpers before PR4 lands.

**Scope:** unify on the conservative "skip on outside-root" rule by
also returning empty from PR1's `_scene_namespace` and
`_prefab_stable_id` outside-root fallbacks (and likewise from any other
PR1 helper that gates on these). Touches: PR1 worktree's
`scene_runtime_planner.py:139-187`. Tests: update the existing
"outside project root" planner tests to expect empty + a corresponding
empty `instances` block when the scene is unreachable.

## 3. `_current_ctx` persistence after `convert_scene` returns

**Where:** `converter/converter/scene_converter.py` — `_current_ctx`
is intentionally left live after `convert_scene` returns (see comment
at the top of the module; downstream callers like `prefab_packages`
invoke scene_converter helpers post-conversion).

**Why:** Codex P3 — any out-of-band call to `_convert_node()` /
`_convert_prefab_node()` between two `convert_scene()` invocations
inherits the prior scene's `scene_runtime_namespace` and would
mis-stamp. Currently latent (only `_convert_prefab_node` is reachable
from `prefab_packages` and it now takes an explicit
`runtime_namespace`); a future helper added to the post-conversion
fan-out could regress this silently.

**Scope:** either clear `_current_ctx` at `convert_scene` exit (forces
post-conversion callers to set up their own context — likely needs
plumbing for `prefab_packages`), or thread `scene_runtime_namespace`
into every helper that constructs an `RbxPart`. The cleaner fix is the
latter — `_stamp_scene_runtime_id` already takes an explicit
`namespace` argument; consumers can opt in.

## 4. UI event wiring asymmetry (codex P3 carry-over)

**Where:** `converter/roblox/rbxlx_writer.py` emits a `UIEventWiring`
LocalScript that walks `screen_guis` for `_OnClick` and generates
`Activated:Connect()` handlers (`_generate_ui_event_script`). The
headless `luau_place_builder` now emits the `_OnClick` attribute
(PR2 fix to the UI attribute drop) but never generates the wiring
script.

**Why:** Codex called this "surface parity, not click-behavior
parity". The headless builder is currently used for the Luau
Execution publish path; players see clickable buttons that don't fire
because there's no wiring. The two writers should be parity in
behavior, not just attribute surface.

**Scope:** factor `_generate_ui_event_script` out of `rbxlx_writer`
into a writer-agnostic helper (or duplicate the logic into
`luau_place_builder`) and emit the resulting LocalScript from the
headless builder too. Tests: assert that both writers produce
`Activated:Connect` calls for a button with `_OnClick`.

## 5. No round-trip byte-equality assertion for legacy `unity_file_id`

**Where:** `converter/tests/test_scene_runtime_id_stamping.py` —
`test_unity_file_id_constraint_referent_untouched` checks that
constraint `Part1` referents still resolve. The PR2 test matrix line
says "`unity_file_id` byte-unchanged" which is stricter.

**Why:** Codex notes the coverage gap. The current test guarantees
referent resolution but not byte-identical output for a constraint
chain. A future change that shuffles attribute ordering inside the
`AttributesSerialize` blob (or changes referent allocation) would
slip past.

**Scope:** snapshot test — convert a fixed scene + constraint chain
to rbxlx pre-PR2 and post-PR2, diff bytes outside the new
`_SceneRuntimeId` entries. Likely needs a small "filter out
`_SceneRuntimeId`" helper on the snapshot.

## 6. Runtime-spawned prefab templates emit unstamped (PR4 lookup asymmetry)

**Where:** `converter/converter/prefab_packages.py:generate_prefab_packages`
— call site of `_convert_prefab_node`.

**Why:** `generate_prefab_packages()` invokes `_convert_prefab_node()`
WITHOUT `runtime_namespace`, so the `ReplicatedStorage.Templates`
copies of every prefab ship with no `_SceneRuntimeId` at all. The
same prefab cloned at runtime via `instantiatePrefab` should expose
the same lookup surface PR4 needs as a scene-pre-instantiated prefab
does — otherwise the host has two incompatible reflection paths for
"is this object's GameObject id X." Surfaced by the round-2 codex
review as a P2.

**Scope:** in `prefab_packages.py`, compute the same `_prefab_stable_id`
the scene-instantiation path uses (`_convert_prefab_instance`'s
`prefab_namespace`) and pass it through `_convert_prefab_node`'s
`runtime_namespace` kwarg. Add a fixture asserting root + descendant
`_SceneRuntimeId` values on a `ReplicatedStorage.Templates` entry
match the value the scene-stamped clone would carry.

## 7. Nested `Canvas` children routed through the wrong UI path

**Where:** `converter/converter/ui_translator.py:find_canvas_nodes`
(and the `_convert_ui_element` consumer of its output).

**Why:** `find_canvas_nodes()` stops descending once it hits a
`Canvas`, so a child `Canvas` under another `Canvas` never becomes
its own `RbxScreenGui`. Instead `_convert_ui_element()` sees the
nested `Canvas` component on a child node and emits it through the
generic `RbxUIElement` serializer with `class_name="ScreenGui"`. The
nested canvas then loses its own `CanvasScaler` extraction and both
writers treat a top-level Roblox concept (`ScreenGui`) like a normal
GUI element. Unity Canvas hierarchies legally nest; the surface
asymmetry will pin PR4 lookup into either a special-case path or a
silent drop. Round-2 codex P2.

**Scope:** in `find_canvas_nodes`, recurse INTO nested Canvas
subtrees (don't return on the outer match), then in the converter
emit each nested Canvas as a separate `place.screen_guis` entry
rather than letting the recursive element converter see it.
