# Future Improvements

Long-horizon, multi-PR or strategic work. For active PR-scoped items, see
[`TODO.md`](../TODO.md). For documented limitations, see
[`UNSUPPORTED.md`](UNSUPPORTED.md). For architectural debt and bug-shaped
gaps, see [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md).

This file captures vision-level work — items that span multiple PRs, require
architecture decisions before code, or are nice-to-have improvements not yet
committed to as work.

---

## Animation

**Transform / property animation** is supported: a clip that drives only
arbitrary transform children (no humanoid bones) converts to an inline
TweenService `Anim_*` script.

**Skeletal / character animation is out of scope — permanently.** A Unity
`SkinnedMeshRenderer` converts to a single rigid `MeshPart`, and Roblox
exposes no automated/headless path to a skinned `MeshPart` that deforms via
`Bone` instances (skinned-mesh import is Studio-3D-Importer-only). The
`character_animator` skeletal-animation runtime, its `AnimationData_*` data
modules, and the per-controller bootstrap were retired in 2026-05. Humanoid
clips, `AnimatorController` state machines, blend trees, layers, masks, root
motion, and IK are all surfaced to `UNCONVERTED.md`. See
`docs/UNSUPPORTED.md`.

---

## Asset upload pipeline refactor

The current upload path (`roblox/cloud_api.py`) does not deduplicate by
content hash. Identical textures with different filenames re-upload every
time, costing both wall time and Roblox quota.

### Content-hash deduplication

For each `.glb` / `.png` / `.ogg` file:

1. Hash content (SHA-256) before uploading.
2. Store `{content_hash: roblox_asset_id}` in `.roblox_ids.json` alongside
   the existing filename-keyed cache.
3. Before uploading, check if hash already exists → skip upload, reuse
   asset ID.

Estimated savings on a re-run of SimpleFPS: 175 meshes × ~3s upload latency
= 9+ minutes that becomes near-zero on cache hit.

---

## Custom serializer evaluation: rbx-dom or rbxmk

The current `rbxlx_writer.py` reimplements Roblox's XML place format
(`rbxl_binary_writer.py` the binary variant). The
research suggests rbx-dom is "the definitive industry standard" — our
serializer misses internal properties (confirmed: `MeshPart.MeshId` set in
XML is ignored by Studio).

### Current workaround

`InsertService:LoadAsset()` at runtime bypasses the serializer for meshes
entirely. The serializer only handles simple properties (scripts, lighting,
spawn points) which it does correctly.

### Why this is low priority

Since meshes load via InsertService, the serializer only writes the place
shell. For that use case it works fine. Replacing with rbxmk adds a Go
binary dependency for marginal benefit.

### When to revisit

- If the runtime LoadAsset approach starts hitting quota / rate-limit walls
  at scale.
- If a future Roblox API change makes more properties serializer-only.
- If we want true headless place generation without Studio MCP for asset
  resolution.

---

## Type discipline across the whole repo

The forward-only no-Any gate (PR #10) prevents new smuggling.

### Phase A — promote existing offenders (landed)

The file-by-file `Any` cleanup is complete: dedicated PRs tightened
`storage_plan`, the ported-module signatures (PR #34), `PipelineState`
(PR #36), and the trivial 3-fix + `ConversionContext` final 4. `TODO.md`
§ "Type-strictness debt" no longer tracks any remaining files.

### Phase B — repo-wide type checker in CI

CI today enforces only the forward-only no-Any gate
(`tools/check_no_any.sh`). The strategic next step is adding `pyright`
(or `mypy --strict`) as a full CI gate, initially scoped to `core/`,
`unity/`, `roblox/` — the "data" layer — then `converter/` (the
orchestration layer) once that's clean.

---

## Standalone `.rbxm` per-prefab output

Source repo had `write_rbxm_package()` for emitting prefabs as standalone
Roblox model files. Useful for Roblox Toolbox / sharing individual prefabs
without the whole place. Not needed at runtime — gameplay uses the
`ReplicatedStorage.Templates` path instead.

Tracked as a P2 in `TODO.md`; could expand into a richer per-prefab export
flow if a use case emerges (e.g. "export this Unity prefab as a Toolbox-
ready model" CLI command).

---

## Persistent prefab/asset cache

The prefab library is rebuilt from disk on every conversion. SQLite or
pickle cache keyed by `(GUID, mtime)` would halve pipeline time for
multi-scene projects and large games.

Tracked as a P2 in `TODO.md`. Captured here because the cache schema
needs a design pass before code (what gets cached, invalidation strategy,
migration).

---

## `.rbxlx` reader for direct publish-from-disk

The dest pipeline only writes `rbxlx`; it never reads one. Both publish paths therefore reconstruct `rbx_place` rather than reading the on-disk file:

- **Interactive `upload`** (`convert_interactive.py upload`) re-runs the pipeline in-memory and publishes a fresh rebuild. Hand-edits to `converted_place.rbxlx` between `assemble` and `upload` are silently dropped. A runtime warning surfaces this.
- **`u2r.py publish`** replays cached chunks (`<output>/place_builder_chunks.json`) when present, preserving the assembled state byte-for-byte. Falls back to a fresh Pipeline rebuild on cache miss.

Adding an `.rbxlx` reader would let `upload` honor hand-edits to the on-disk file directly and would unify the two publish paths. The work is non-trivial: the writer is the only round-trip in the codebase today, and the reader needs to handle Roblox's full XML schema (Refs, attributes, custom serializers) for non-trivial places.

Until a reader exists, the workaround is **open `converted_place.rbxlx` in Roblox Studio → File → Publish to Roblox**. Studio is the only path that publishes the reviewed file directly. See `converter/CLAUDE.md` § Upload semantics for the full comparison.

When to revisit:
- If user feedback shows the rebuild path is dropping meaningful hand-edits.
- If the cached-chunks fast path is insufficient for a real workflow (e.g., users want to edit `place_builder.luau` directly and re-publish).
- If we move toward a serializer-first design (see "Custom serializer evaluation" above) — at that point a reader becomes a natural side product.

---

## NavMesh advanced features

The current `nav_mesh_runtime.luau` provides `PathfindingService`-backed
movement for `NavMeshAgent`. Advanced cases not covered:

- **Off-mesh links** — Unity NavMesh supports manually-defined connections
  (jumping gaps, climbing ladders). Not preserved.
- **Area costs** — Unity NavMesh supports per-area cost tweaking
  (e.g. roads cheaper than grass). Not surfaced to PathfindingService
  modifiers.
- **Dynamic carving** — `NavMeshObstacle` with carving on. Currently the
  obstacle metadata is captured as attributes but no runtime re-bake.

Revisit if a project ships AI navigation that depends on these features.

---

## Replace `fps_weapon_mount_inject` with a generic Unity-Instantiate lowering pass

The `fps_weapon_mount_inject` patch pack in
[`script_coherence_packs.py`](../converter/script_coherence_packs.py)
is data-driven over a one-entry `WEAPON_MOUNTS` registry that today
matches SimpleFPS exactly. **Don't extend the registry weapon-by-weapon.**
A codex architecture review (PR #121, 2026-05-20) read the pack as
"compensating for missing generic prefab/object-ref lowering" and
flagged the registry approach as the wrong abstraction boundary:

- It hardcodes the weapon (`riflePrefab → Rifle`).
- It invents Roblox-side conventions Unity didn't author (the
  `_MainCameraRig` attribute walk + `FindFirstChild("WeaponSlot")`
  name search) instead of resolving the authored Inspector field.
- `Instantiate + SetParent + local-transform reset` is generic
  Unity — pickup spawners, projectile spawners, particle bursts all
  use it. The same shape is currently handled by ~5 different
  genre-aware packs (`bullet_physics_raycast`,
  `pickup_remote_event_server`, `pickup_visual_target`,
  `door_tween_open`, etc.). Each pack pattern-matches one
  manifestation of the same Unity primitive.

The repo's existing generic resolver
([`serialized_field_extractor.py`](../converter/serialized_field_extractor.py))
covers GUID-backed prefab/audio refs but **does not cover non-GUID
scene/prefab-local Transform refs** like `weaponSlot`. That gap is the
real reason this pack exists.

### Replacement blueprint

1. **Extend serialized-ref extraction** to record non-GUID object refs
   (Transform/GameObject Inspector fields) alongside the existing
   prefab/audio entries. Output: a `_AutoRef_<fieldName>` attribute on
   the converted Part (matching the existing `_AutoRef_*` convention
   the prompt already mentions for prefab refs).
2. **Stamp converted target objects** with a stable locator derived
   from Unity identity (file IDs, scene paths), not a semantic name
   search. `_MainCameraRig` works as scaffolding for the camera rig
   itself — keep that — but the WeaponSlot child should be locatable
   by its Unity Transform identity.
3. **New `unity_instantiate_lowering` pass** that detects the
   `Instantiate(<serializedPrefab>, ...) + SetParent(<serializedTransform>)
   + local-transform reset` shape and rewrites it to canonical Luau:
   clone from `ReplicatedStorage.Templates`, parent to the resolved
   `_AutoRef_<field>`, reset local pose, preserve explicit scale.
   Detection anchored on body shape (Instantiate-followed-by-SetParent),
   not function name.
4. **Keep prompt tightening as a hint only.** The transpile prompt
   already says "use `ReplicatedStorage.Templates:WaitForChild(name)`
   for prefab refs" ([`code_transpiler.py`](../converter/code_transpiler.py))
   and `Instantiate(prefab) → prefab:Clone(); clone.Parent = workspace`.
   AI doesn't reliably obey -- the lowering pass should ENFORCE the
   shape, not hope for it.

### Migration order (codex's specific recommendation)

* Build the generic instantiate/object-ref lowering path first.
* Switch the SimpleFPS rifle to it.
* Leave `fps_weapon_mount_inject` as a fallback for one cycle.
* Then delete the pack.
* **Do not** genericize `pickup_remote_event_server` /
  `bullet_physics_raycast` / `pickup_visual_target` in the same
  sweep — those solve different problems (replication, physics model,
  model-vs-trigger lowering), not Instantiate.
* **Do** keep `_MainCameraRig` + `CameraRigFollower` scaffolding
  ([`autogen.py`](../converter/autogen.py)). Codex called those out as
  a legitimately good generic replacement for bespoke weapon-follow
  code -- the hack is the weapon-mount pack, not the camera-rig
  follower.

### Trigger to do this work

The current one-entry registry costs ~10 lines and covers SimpleFPS.
The cost of NOT doing it is technical debt that compounds the next
time a project ships:

* A second weapon (even in SimpleFPS) — DON'T add a `WeaponMount`
  tuple; do the lowering pass.
* A second FPS project with different naming.
* Any project that ships a non-weapon Inspector-Transform-ref
  Instantiate pattern (item spawner referencing a `spawnSlot`,
  projectile referencing a `muzzleTransform`, etc.). The first such
  case won't have any pack catching it at all -- it just renders
  broken.

Probable trigger: the next time a SimpleFPS playtest surfaces a
broken `Instantiate(prefab) + SetParent(transformField)` site that
isn't a weapon. Or when a second FPS project enters the test bench.
