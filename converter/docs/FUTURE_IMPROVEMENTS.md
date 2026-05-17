# Future Improvements

Long-horizon, multi-PR or strategic work. For active PR-scoped items, see
[`TODO.md`](../TODO.md). For documented limitations, see
[`UNSUPPORTED.md`](UNSUPPORTED.md). For architectural debt and bug-shaped
gaps, see [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md).

This file captures vision-level work — items that span multiple PRs, require
architecture decisions before code, or are nice-to-have improvements not yet
committed to as work.

---

## Animation system completion

**Status:** Phases 0–2 and 4 landed. Animator controllers transpile to
self-contained per-controller `Anim_*_StateMachine` scripts
(`animation_converter.py:generate_state_machine_script` — inline
TweenService/RunService, no runtime `require`); other clips ship as inline
`Anim_*` TweenService scripts. Phases 3 and 5 are open.

> **Orphan-module debt.** `runtime/animator_runtime.luau` (the
> `AnimatorRuntime` state-machine ModuleScript and its 1D blend-tree
> support) and the `AnimationData_*` data modules are injected into output
> when `HasAnimator` is set, but **no generated script `require`s or
> instantiates them** — they are dead code in every converted project. The
> Unity `Animator.*` API is translated inline by `api_mappings.py` to
> `:SetAttribute` / `AnimationTrack:Play()`, bypassing the module entirely.
> Resolve by either wiring a real `require(...AnimatorRuntime)` consumer or
> deleting the orphan module + its injection. The same overstatement is
> stale in `CLAUDE.md` ("5 runtime Luau modules auto-injected") and
> `docs/design/inline-over-runtime-wrappers.md`.

### Phase 3 — `KeyframeSequence` export

Generate `KeyframeSequence` XML nodes in the rbxlx so animations are part
of the place file rather than referenced by external asset ID. Closest to
self-contained output. The alternative (bulk-uploading via `cloud_api.py`
to get asset IDs) works but adds an upload step and asset count.

Trade-off: KeyframeSequence is a heavy XML structure, but it's deterministic
and round-trips correctly.

### Phase 5 — Advanced features (out of scope until needed)

- **2D blend trees (freeform)** — Cartesian / directional blending with
  Delaunay triangulation. Currently surfaced to `UNCONVERTED.md` with the
  first leaf clip used as fallback.
- **Animation layers + avatar masks** — Roblox has no per-bone masking;
  would need per-bone track splitting or `AnimationTrack.Priority` games.
- **Root motion extraction** — Separate root bone curves → apply as
  `HumanoidRootPart` movement.
- **Inverse kinematics** — Would require a full IK solver in Luau.
  Out of scope.

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

## Generic FPS weapon-mount metadata extraction

The `fps_weapon_mount_inject` patch pack in
[`script_coherence_packs.py`](../converter/script_coherence_packs.py) is
data-driven over a one-entry `WEAPON_MOUNTS` registry that today matches
SimpleFPS exactly. Adding a second FPS test project surfaces two open
questions worth resolving with a real signal:

- **Mount metadata source.** Today's entry hardcodes prefab name, view
  offset, and scale. A second project would justify extracting these
  from Unity prefab YAML (Player MonoBehaviour fields + the weapon-mount
  Transform's `localPosition` × `STUDS_PER_METER`) and persisting them
  into `conversion_context.json` as an `fps_weapon_mounts` block, so
  the registry becomes derived state rather than hand-authored.
- **Detection generality.** The current detector anchors on function
  name (`GetRifle`) and prefab name (`riflePrefab`). Two AI-transpiled
  projects with different naming would justify anchoring on body shape
  instead (a Clone() of a Templates child followed by a sentinel flag
  flip) and using metadata as the source of truth.

Not worth building speculatively — the hardcoded entry costs ~10 lines
and covers the only FPS consumer. Revisit when a second FPS Unity
project enters the test bench.
