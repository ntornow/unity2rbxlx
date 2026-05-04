# Phase 6: Polish — Execution Plan (v2 post-Codex)

**Branch:** `phase-6-implementation`
**Source plan:** [`MERGE_PLAN.md`](https://github.com/jiazou/unity-roblox-game-converter/blob/main/MERGE_PLAN.md) Phase 6 section
**Date:** 2026-05-02
**Status:** Codex-reviewed; ready to execute

Phase 6 is the closeout pass after Phase 5. The original plan listed six items. Codex review of the v1 draft (consult mode, session `019de6f0…`) flagged a stack of stale references and overclaims that v1 missed. This v2 plan addresses every Codex pushback. Each work item has reproducible acceptance criteria.

---

## Audit results vs original Phase 6 list

| # | Original item | v2 status |
|---|---|---|
| 1 | Port docs from source `docs/` | **3 of 5 done; 2 superseded** — `UNSUPPORTED.md`, `KNOWN_ISSUES.md`, `FUTURE_IMPROVEMENTS.md` exist. `GAME_LOGIC_PORTING.md` and `MODULE_STATUS.md` superseded; reasoning recorded in P6.6. |
| 2 | Final README + CLAUDE.md + ARCHITECTURE.md | **PARTIAL** — README has stale "Luau validator" + "VFX Graph and advanced particle sub-emitters" + "Skeletal/bone animations not supported" claims. CLAUDE.md "Key milestones achieved" still claims `Luau validator: 6,950 lines`. ARCHITECTURE.md missing `moderate_assets`, `luau-analyze`, design-decisions section. |
| 3 | Verify both modes work end-to-end | **PARTIAL** — fast suite green via PR CI; e2e/smoke jobs are nightly-only. Phase 6 must run a local SimpleFPS conversion through both modes (P6.7). |
| 4 | Clean up dead code | **PARTIAL** — Python source is clean (TODO.md has zero stale refs to deleted modules). But `UNSUPPORTED.md:117` and `:188-194` reference the deleted `mesh_splitter` as if it still exists. P6.4 fixes this. |
| 5 | C2 closeout | **PARTIAL** — runtime warning emitted in `convert_interactive.py:1011`. Decision rationale not yet in any user-facing repo doc. v1 missed that `u2r.py publish` already has a cached-chunks fast path that semantically does publish the assembled state; the doc must distinguish the two paths. |
| 6 | Verify dead-path cleanup | **DONE** — `report_generator` (5 call sites in `pipeline.py`), `rbxl_binary_writer` (`pipeline.py:2206`), `vertex_color_baker` (`pipeline.py:1024`). All three Phase 2 modules wired in Phase 3 (binary writer, report generator) or Phase 4.8 (vertex color baker). |

### Wiring proof (item 6) — reproducible

```
$ rg -n "from converter\.report_generator|from roblox\.rbxl_binary_writer|from converter\.vertex_color_baker" converter --type py | rg -v __pycache__
converter/convert_interactive.py:1047:    from converter.report_generator import augment_report
converter/converter/pipeline.py:1024:            from converter.vertex_color_baker import bake_vertex_colors_batch
converter/converter/pipeline.py:2206:                from roblox.rbxl_binary_writer import xml_to_binary
converter/converter/pipeline.py:2271:        from converter.report_generator import generate_report
converter/converter/pipeline.py:2368:        from converter.report_generator import ScriptSummary
converter/converter/pipeline.py:2396:        from converter.report_generator import (
converter/tests/test_rbxl_binary_writer.py:12:from roblox.rbxl_binary_writer import MAGIC, xml_to_binary
converter/tests/test_vertex_color_baker.py:9:from converter.vertex_color_baker import (
converter/tests/test_report_generator.py:11:from converter.report_generator import (
```

No Phase 2 module is unwired.

---

## Remaining Phase 6 work items

Order: P6.1 → P6.5 are doc edits in any order. P6.6 is a closing index entry. P6.7 is verification — runs last.

### P6.1 — C2 closeout: document upload semantics, distinguishing both paths

**Why:** The interactive `upload` subcommand rebuilds `rbx_place` from source rather than reading the local `converted_place.rbxlx`. A runtime warning ships today (`convert_interactive.py:1011`). What v1 missed: `u2r.py publish` (`u2r.py:208–263`) has a cached-chunks fast path — it replays `<output>/place_builder_chunks.json` if present and only rebuilds on cache miss. So C2's "fresh rebuild not reviewed .rbxlx" applies fully to interactive `upload` and partially to `u2r.py publish`. The doc must distinguish.

**Files edited:**

1. `converter/.claude/skills/convert-unity/references/phase-6-upload.md` — add `## What gets published` section explaining interactive `upload` rebuild semantics. Cross-link to (2).

2. `converter/CLAUDE.md` — add a short "Upload semantics" subsection under § Architecture (or just before "Running Tests") that documents both paths in the two-mode language used elsewhere in CLAUDE.md:
   - **Interactive `upload`**: re-runs `parse → … → convert_scene` in-memory; publishes the freshly-rebuilt `rbx_place`. Hand-edits to `converted_place.rbxlx` between `assemble` and `upload` are silently dropped. Surface the existing runtime warning.
   - **`u2r.py publish`**: replays `place_builder_chunks.json` if present (preserves the assembled state byte-for-byte). Falls back to a fresh Pipeline rebuild only on cache miss. Use this when you want to publish without re-running the converter.
   - **Hand-edited `.rbxlx`**: open in Studio, File → Publish to Roblox. There is no `.rbxlx` reader on the dest side.

3. `converter/docs/FUTURE_IMPROVEMENTS.md` — add an entry under a new "Upload" or "Tooling" subsection for ".rbxlx reader for direct publish-from-disk", written in the existing prose style of that doc (no P-level tag — the doc uses prose status, not P-buckets).

**Acceptance (reproducible):**
- `rg -n "fresh rebuild" converter/CLAUDE.md converter/.claude/skills/convert-unity/references/phase-6-upload.md` returns ≥2 hits (one in each file).
- `rg -n "place_builder_chunks" converter/CLAUDE.md converter/.claude/skills/convert-unity/references/phase-6-upload.md` returns ≥1 hit.
- `rg -n "rbxlx reader" converter/docs/FUTURE_IMPROVEMENTS.md` returns ≥1 hit.
- The CLAUDE.md "Upload semantics" subsection enumerates both `interactive upload` and `u2r.py publish` paths separately.

### P6.2 — ARCHITECTURE.md: align with current pipeline

**Why:** `converter/ARCHITECTURE.md:69–81` lists pipeline phases without `moderate_assets`. Scripts entry doesn't mention `luau-analyze`. There is no design-decisions section linking to `inline-over-runtime-wrappers.md`. Codex flagged that delegating the Supported Features list to CLAUDE.md just shifts drift; v2 keeps ARCHITECTURE.md self-contained but trims overlap.

**Edits to `converter/ARCHITECTURE.md`:**

1. Pipeline Phases (lines 69–81): insert `Moderate Assets` between `Extract Assets` and `Upload Assets` with a one-line description (screen filenames, scripts, audio against Roblox Community Standards; auto-blocklist violations).
2. Scripts phase entry: append "syntax-gated by `luau-analyze` + AI reprompt loop (replaces the former `luau_validator.py`, removed 2026-04-18)".
3. Insert new "## Design Decisions" section after "## Design Principles" with two pointers:
   - **Inline over runtime wrappers** — see `docs/design/inline-over-runtime-wrappers.md`. One-sentence summary.
   - **Conversion plan rehydration** — `conversion_plan.json` provides lossless script rehydration across the three flows (Phase 3, item 12).
4. Supported Features list (lines 136–170): the list is internally OK but slightly out of date (e.g., line 138 says binary scenes "require UnityPy" which is now landed). Update line 138 to "Text + binary YAML scene parsing (binary via UnityPy, including terrain `.asset`)". Other lines are accurate. **Do not delegate to CLAUDE.md** — that just moves drift.
5. Line 40 (`references/upload-patching.md` pointer) — replace with a short paragraph that names the user-facing knowledge (cached-chunk fast path + interactive rebuild + Studio manual-publish escape hatch) and cross-links the new CLAUDE.md "Upload semantics" subsection from P6.1. Keep the skill-file pointer as a "for full skill-internal detail" parenthetical, not the primary pointer.

**Acceptance:**
- `rg -n moderate_assets converter/ARCHITECTURE.md` returns ≥1 hit.
- `rg -n luau-analyze converter/ARCHITECTURE.md` returns ≥1 hit.
- `rg -n "## Design Decisions" converter/ARCHITECTURE.md` returns 1 hit.
- ARCHITECTURE.md does not say "binary requires UnityPy" any more.
- Pointer at line 40 is to a repo doc (not only to a `.claude/skills/` path).

### P6.3 — README.md: fix three stale claims + add Documentation section

**Why:** README has three concrete inaccuracies that contradict the rest of the docs:
- Line 111: `# 4c. Validate — run the Luau validator over transpiled output` — but `convert_interactive.py validate` runs `luau-analyze`, not the deleted regex validator.
- Line 180: "Skeletal/bone animations are not yet supported" — UNSUPPORTED.md:83 says R15-mappable skeletal animation IS supported via Motor6D + `animator_runtime.luau`.
- Line 183: "VFX Graph and advanced particle sub-emitters are not yet converted" — UNSUPPORTED.md:157 says particle sub-emitters ARE auto-converted via `sub_emitter_runtime.luau`. (VFX Graph is correctly listed as not supported.)

**Edits to `README.md`:**

1. Line 111: replace `# 4c. Validate — run the Luau validator over transpiled output` with `# 4c. Validate — run luau-analyze over transpiled output`.
2. Line 180: replace with "Skeletal animation supported for R15-mappable rigs (Motor6D + `animator_runtime.luau`); binary `.anim` / `.controller` files are not yet parsed (text-YAML works). See `converter/docs/UNSUPPORTED.md` for the full list."
3. Line 183: replace with "VFX Graph is not converted (no node-graph primitive on Roblox); particle sub-emitters are auto-converted via `sub_emitter_runtime.luau` when `_HasSubEmitters` is detected."
4. Insert new "## Documentation" section just before "## Limitations" with bullet links to:
   - `converter/CLAUDE.md` (engineering overview)
   - `converter/ARCHITECTURE.md` (pipeline architecture)
   - `converter/docs/UNSUPPORTED.md` (what the converter cannot do)
   - `converter/docs/KNOWN_ISSUES.md` (architectural debt)
   - `converter/docs/FUTURE_IMPROVEMENTS.md` (long-horizon work)
   - `converter/docs/design/inline-over-runtime-wrappers.md` (key design decision)
   - `converter/TODO.md` (active PR-scoped work)

**Acceptance:**
- `rg -n "Luau validator" README.md` returns 0 hits.
- `rg -n "Skeletal/bone animations are not yet supported" README.md` returns 0 hits.
- `rg -n "advanced particle sub-emitters are not yet converted" README.md` returns 0 hits.
- `rg -n "## Documentation" README.md` returns 1 hit; the section contains all 7 doc links.

### P6.4 — UNSUPPORTED.md + CLAUDE.md: scrub stale module names

**Why:** Two specific stale references that contradict "dead code cleanup done":
- `UNSUPPORTED.md:117`: "Multi-material meshes must be split (handled by `mesh_splitter`)" — `mesh_splitter` was deleted. Sub-mesh hierarchy is now handled by `scene_converter`.
- `UNSUPPORTED.md:188–194`: entire "Multi-material mesh splitting (FBX edge cases)" section narrates `mesh_splitter`'s decomposition behavior. Needs a rewrite to reference `scene_converter`'s sub-mesh hierarchy path.
- `CLAUDE.md:58`: "Luau validator: 6,950 lines, 50+ fix categories, format specifier preservation" sits in the "Key milestones achieved" section as if the validator is current. Validator was removed 2026-04-18.

**Edits:**

1. `UNSUPPORTED.md:117` — replace `(handled by mesh_splitter)` with `(handled by scene_converter's sub-mesh hierarchy)`.
2. `UNSUPPORTED.md:188–194` — rewrite the section to read along the lines of: "`scene_converter` decomposes multi-material FBX into a sub-mesh hierarchy with one MeshPart per submesh material. Edge cases: FBX files where the asset moderator can't extract per-submesh materials fall back to first-material-only with a warning in `UNCONVERTED.md`; sub-mesh material ordering mismatches are surfaced via `UNCONVERTED.md` for manual review."
3. `CLAUDE.md:58` — replace with: "Luau syntax gate: `luau-analyze` + AI reprompt loop in `code_transpiler.py` (replaced regex `luau_validator.py`, removed 2026-04-18)."

**Acceptance:**
- `rg -n mesh_splitter converter/docs/UNSUPPORTED.md` returns 0 hits (or only clearly-marked historical mentions like "(deleted 2026-XX-XX)").
- `rg -n "Luau validator: 6,950 lines" converter/CLAUDE.md` returns 0 hits.
- `rg -n "luau_validator" converter/CLAUDE.md README.md converter/docs converter/ARCHITECTURE.md` returns only historical mentions (none present-tense).

### P6.5 — merge-plan-phase-3-augmented.md: refresh Phase 4 closeout status

**Why:** This design doc still lists items 2, 9, 10, 11 as "Deferred" with a forward-link to TODO.md. Per the source MERGE_PLAN.md success criteria, all four were closed in Phase 4 (vertex_color_baker — 4.8; extract_serialized_field_refs — 4.9; generate_prefab_packages — 4.10; disk rewrite — 4.11). Stale design doc → confusing audit trail.

**Edits to `converter/docs/design/merge-plan-phase-3-augmented.md`:**

1. Item 2 row: change to "**Landed in Phase 4.8** (`pipeline.py:1024`); MaterialMapping `uses_vertex_colors` flag from 4.2 drives wiring."
2. Item 9 row: change to "**Landed in Phase 4.9.** Persisted to `conversion_context.json`; consumed by 4.10."
3. Item 10 row: change to "**Landed in Phase 4.10.** Per-prefab `packages/` emission with variant-chain preservation (5.13)."
4. Item 11 row: change to "**Landed in Phase 4.11.** Disk rewrite covers `animation_data/`, `packages/`, scriptable-object module paths."
5. "## Out of scope" section: remove the "Items 2, 9, 10, 11 — deferred" line; reword to "All items closed."

**Acceptance:**
- `rg -n "Deferred" converter/docs/design/merge-plan-phase-3-augmented.md` returns 0 hits (or only the original Phase 3 narrative context, never as a current status).
- All 12 rows show **Landed** or **Superseded**.

### P6.6 — Mark superseded source docs (closing index entry)

**Why:** The original Phase 6 plan named `docs/GAME_LOGIC_PORTING.md` and `docs/MODULE_STATUS.md` as port targets. After dest's inline-over-runtime-wrappers adoption (2026-04-14, deleted 9 runtime bridges), porting these verbatim would re-introduce stale architectural narrative. A reviewer auditing the merge needs to find a 1-paragraph answer to "why isn't `GAME_LOGIC_PORTING.md` in dest?" without git archaeology.

**Edits:** Append a short "## Source docs not ported (and why)" section to `converter/docs/design/merge-plan-phase-3-augmented.md` (the existing closeout index — it's already there, just needs another section).

Content: two bullets only.
- `GAME_LOGIC_PORTING.md` — describes a 9-module Unity bridge layer that was deleted in dest. The Step 4.5 game-logic-porting playbook lives in `.claude/skills/convert-unity/references/phase-4b-*.md`.
- `MODULE_STATUS.md` — 2026-04-07 status snapshot subsumed by `TODO.md` + per-PR conversion reports. Status snapshots should not be duplicated as canonical docs.

(`material_mapping_research.md` was named in v1 but is out of scope: it's a research note, not in the original Phase 6 list.)

**Acceptance:**
- `rg -n "GAME_LOGIC_PORTING" converter/docs/design/merge-plan-phase-3-augmented.md` returns 1 hit (the new section).
- `rg -n "MODULE_STATUS" converter/docs/design/merge-plan-phase-3-augmented.md` returns 1 hit.

### P6.7 — End-to-end verification (mandatory)

**Why:** Original Phase 6 item 3 says "verify both modes work end-to-end". PR CI only runs the fast suite. A doc-only PR shouldn't break tests, but Phase 6 closeout requires concrete dual-mode evidence. v1 made this optional + accepted `xmllint --noout` as proof of "works"; that's not enough.

**Steps (all mandatory):**

1. Fast pytest suite green:
   ```bash
   cd /Users/jiazou/workspace/unity2rbxlx/converter && python -m pytest tests/ -m "not slow" -v 2>&1 | tail -20
   ```
   Expect 1020+ passed.

2. `u2r.py` non-interactive end-to-end (no upload, no AI):
   ```bash
   cd /Users/jiazou/workspace/unity2rbxlx/converter && rm -rf /tmp/p6smoke_u2r && \
     python u2r.py convert ../test_projects/SimpleFPS -o /tmp/p6smoke_u2r --no-upload --no-ai --scene main.unity
   ```
   Expect exit 0 + `/tmp/p6smoke_u2r/converted_place.rbxlx` non-empty.

3. `convert_interactive.py` mode end-to-end (no upload, no AI):
   ```bash
   cd /Users/jiazou/workspace/unity2rbxlx/converter && rm -rf /tmp/p6smoke_int && \
     python convert_interactive.py preflight ../test_projects/SimpleFPS /tmp/p6smoke_int && \
     python convert_interactive.py discover ../test_projects/SimpleFPS /tmp/p6smoke_int && \
     python convert_interactive.py inventory ../test_projects/SimpleFPS /tmp/p6smoke_int && \
     python convert_interactive.py materials ../test_projects/SimpleFPS /tmp/p6smoke_int && \
     python convert_interactive.py transpile ../test_projects/SimpleFPS /tmp/p6smoke_int --no-ai && \
     python convert_interactive.py assemble ../test_projects/SimpleFPS /tmp/p6smoke_int --no-upload
   ```
   Expect every subcommand exit 0 + `/tmp/p6smoke_int/converted_place.rbxlx` non-empty.

4. Acknowledge known Phase-5-leftover that does **not** gate Phase 6: the `test_three_flows_produce_identical_rbxlx` xfail (`tests/test_byte_equivalence.py:182`, tracked in `TODO.md`). The interactive-fresh and CLI flows produce different `RbxScript` containers; v2 closeout does not regress this.

**Acceptance:**
- All three commands exit 0.
- `wc -c /tmp/p6smoke_u2r/converted_place.rbxlx` and `wc -c /tmp/p6smoke_int/converted_place.rbxlx` both ≥ 100 KB.
- `xmllint --noout` (sanity, not the primary signal) on both files exits 0.
- Fast pytest suite is green on the Phase 6 PR commit.

---

## What this plan deliberately does NOT include

- **Implementing the `.rbxlx` reader.** Locked decision: defer to roadmap. P6.1 documents it; P6.1's `FUTURE_IMPROVEMENTS.md` entry roadmaps it.
- **Closing any Phase 5 P2 items still open in `TODO.md`** (5.1 byte-equivalence xfail, 5.2 real-upload secrets, 5.4 visual baseline, 5.6 binary `.anim`/`.controller` parser, etc.). All explicitly tagged Phase-5 follow-ups; do not gate Phase 6.
- **Material-mapping research note port.** Out of scope (not in original Phase 6 list).
- **Resurrecting source-repo MODULE_STATUS.md content.** P6.6 records the supersession; doesn't re-port.
- **Single-source-of-truth cross-doc dedup.** v1 proposed delegating to CLAUDE.md; Codex correctly flagged that as just-moving-the-drift. Accept some duplication; fix all stale claims in place.
- **MERGE_PLAN.md edits in the source repo.** That's a separate PR on `jiazou/unity-roblox-game-converter`.

## Risk register

| Risk | Mitigation |
|---|---|
| Stale claim slips back in via cross-doc copy | P6.4 acceptance grep checks; CI not strengthened beyond what's already there. Accept residual risk. |
| End-to-end smoke times out / fails on a clean checkout | Run with `--no-upload --no-ai` — both flags eliminate network + LLM dependencies. SimpleFPS is the primary fixture; if smoke fails, that's an actual regression to fix before merging. |
| `u2r.py publish` cached-chunks claim is wrong | P6.1 step 2 says read `u2r.py:208–263` first to verify the doc statement is true; revise if the cache key or fall-through behavior differs. |
| Codex flags more issues on v2 | This plan addresses every v1 finding. If v2 still has gaps, fix them inline rather than another full review round. |
