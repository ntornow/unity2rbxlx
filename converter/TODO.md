# Converter TODO

Active work items only. Completed work + PR execution logs live in `TODO_archive.md`.

Priority: **P0** = blocks gameplay, **P1** = significant quality, **P2** = nice to have.

---

## Pipeline / runtime gaps

- [ ] **P2 — Persistent prefab/asset cache.** Prefab library is in-memory only.
  SQLite or pickle cache keyed by `(GUID, mtime)` would halve pipeline time
  for multi-scene projects and large games.

## Materials & meshes

- [ ] **P2 — Full SurfaceAppearance round-trip through templates.** PR 5
  deferred. The smoke ran with `--no-upload` so real asset IDs never wired
  through `ReplicatedStorage.Templates`. Verify on a full upload run.
## Infrastructure

- [ ] **P2 — Three-flow `rbx_place` byte-equivalence test.** Codex review
  P1-6 deferred. Source-level + behavior-level parity is now tested
  (commit `420b01e`). Byte-equivalence requires a real Unity fixture.
- [ ] **P2 — Standalone `.rbxm` file output per prefab.** PR 5 deferred.
  Toolbox convenience; no runtime dependency on this format.

## Type-strictness debt (forward-only gate landed; cleanup separate)

The no-Any gate prevents new smuggling. Existing-offender cleanup has
landed in dedicated PRs (#10 gate, storage_plan, ported-module signatures
PR #34, PipelineState PR #36, trivial 3-fix + ConversionContext final 4).

Remaining items:

- [ ] **P2 — `scene_converter.py:180` `_mesh_hierarchies: dict[str, list[dict]]`.**
  Module-level cache; the bare `dict` is missing-type-arg, not Any.
  Tighten to `dict[str, list[MeshHierarchyEntry]]` for consistency
  with `ConversionContext.mesh_hierarchies` (the TypedDict is in
  `core/conversion_context.py`). Not flagged by the no-Any gate;
  cleanup-only.

---

For platform limitations, Unity features with no Roblox equivalent, and Open
Cloud API limits, see [`docs/UNSUPPORTED.md`](docs/UNSUPPORTED.md). For
architectural debt and bug-shaped gaps, see [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md).
For long-horizon strategic work, see [`docs/FUTURE_IMPROVEMENTS.md`](docs/FUTURE_IMPROVEMENTS.md).
