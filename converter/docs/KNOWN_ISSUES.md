# Known Issues

Architectural debt, bug-shaped concerns, and test gaps in the converter that
are not yet on `TODO.md` as planned work. Each entry has been cross-referenced
against the current codebase. Issues that source repo (`unity-roblox-game-converter`)
flagged but are now fixed are not listed here — see commit history for the
resolutions.

---

## Module-level

### Script type classification is heuristic

**File:** `converter/converter/code_transpiler.py`

Script type (Script vs LocalScript vs ModuleScript) is classified by the AI
based on which API calls appear in the C# source. A script that only uses
`print()` defaults to `Script` (server-side), which fails silently if the
script was meant to be client-side. Classification doesn't consider Unity's
execution context (Editor scripts, ScriptableObjects, etc.).

Mitigation in dest: client-only `require` propagation auto-reclassifies dependent
scripts to `LocalScript`. Reduces but doesn't eliminate the gap.

### No token budget guard on AI transpilation

**File:** `converter/converter/code_transpiler.py:1574`

The Anthropic call uses `max_tokens=ANTHROPIC_MAX_TOKENS` but does not check
the response's `stop_reason` to detect truncation. A large C# file's Luau
output can be silently cut off mid-function. The luau-analyze gate catches
the resulting syntax error, but the underlying truncation is opaque.

**Fix:** check `message.stop_reason == "max_tokens"` after the API call;
either retry with a higher limit or surface to `UNCONVERTED.md`.

### No texture operation rollback on partial failure

**File:** `converter/utils/image_processing.py`, `converter/converter/material_mapper.py`

Texture operations (resize, channel extract, AO bake) write files to the
output directory. If the pipeline fails partway through, partial/corrupt
texture files are left behind. Re-running may skip operations whose output
file already exists, leading to stale data.

**Fix:** write to a `.tmp` path then atomic-rename on success; remove tmp
files on phase failure.

### Modules import `config` directly

**Files:** `converter/unity/guid_resolver.py:13`, `converter/converter/material_mapper.py`, `converter/converter/asset_extractor.py`

CLAUDE.md's "no cross-import" spirit is broken by 3+ modules importing `config`
directly. Couples them to the singleton, hurts testability, and is inconsistent
with how other modules accept config via the `ConversionContext`.

**Fix:** thread relevant config values through function args / context object.

### Mesh decimator quality floor can override the Roblox face limit

**File:** `converter/converter/mesh_processor.py` (or wherever the quality floor lives)

If a mesh has 50,000 faces and `MESH_QUALITY_FLOOR=0.6`, the minimum allowed
post-decimation is 30,000 — still above Roblox's 10,000-triangle `MeshPart`
limit. The floor should never override the platform hard cap.

**Fix:** clamp the post-decimation face count to `MESH_ROBLOX_MAX_FACES`
regardless of quality floor.

### Validator catches syntax, not Roblox API semantics

**File:** transpile validation gate (`luau-analyze` + AI reprompt)

`luau-analyze` checks Luau syntax. It does not verify Roblox API correctness
(e.g. `workspace:FindFirstChild` vs `workspace.FindFirstChild`). A script
can pass validation and fail at runtime. The AI reprompt loop covers some
of this when the test harness exercises the script, but mute scripts (no
test coverage) can ship broken.

**Fix scope:** would require a Roblox API surface model (likely from rbx-dom
metadata) integrated into a custom checker. Significant scope; tracked here
as a known gap, not a planned work item.

### GUID resolver uses first-match on duplicates

**File:** `converter/unity/guid_resolver.py:79-82`

Duplicate GUIDs are now tracked in `index.duplicate_guids`, but the resolver
still picks the first occurrence. Unity's own resolution order (which depends
on internal package precedence rules) may not match filesystem traversal
order. Projects with GUID conflicts can resolve to the wrong asset.

**Fix:** surface duplicate-GUID warnings to `conversion_report.json`; document
that projects should clean up their GUID conflicts upstream.

---

## Test coverage gaps

### No real integration tests for AI transpilation

The AI transpilation path is only tested with mocks. No tests verify:
- Prompt format sent to Claude
- Handling of truncated responses (related to "no token budget guard" above)
- Handling of API errors (rate limits, invalid key, retries)
- That the parsed response actually produces valid Luau end-to-end

**Mitigation:** `TestRiflePickupChainValidator` hand-crafts AI-shape fixtures
and runs them through the validator. Covers known regressions but not the
full AI path.

### Network/upload tests are fully mocked

`test_cloud_api.py` mocks all HTTP calls. No tests for:
- Rate-limiting behavior (429 responses with retry)
- Partial upload failures (some textures succeed, some fail)
- Timeout handling
- Multipart form data construction validity
- Response parsing for different Roblox API versions

### No large-scale / stress tests

All test fixtures are minimal. No tests for:
- Projects with 100+ scenes
- Projects with 1000+ materials
- Deeply nested hierarchies (50+ levels)
- Very large C# files (5000+ lines)
- Memory usage under load

The eval suite (`u2r.py eval`) covers some of this on real projects but isn't
a unit-test-style stress test.

### Material texture operations not pixel-verified

Material mapper tests check that texture operations are *queued* but don't
verify the pixel output. A bug in channel extraction or normal-map inversion
would not be caught by current tests.

---

## Operational

### No graceful Ctrl+C handling

Neither `u2r.py` nor `convert_interactive.py` handles `KeyboardInterrupt`.
A user cancellation during material processing or mesh decimation can leave
partial output files in an inconsistent state. The interactive mode's state
file won't record the interrupted phase, so resume may behave unexpectedly.

**Fix:** wrap pipeline phases in a `try / except KeyboardInterrupt` that
flushes the context to disk and exits cleanly.

### `.asset` extension miscategorized as "unknown"

**File:** `converter/config.py` (`ASSET_EXT_TO_KIND`)

Unity `.asset` files map to `"unknown"` in the asset manifest, but
`scriptable_object_converter.py` specifically processes `.asset` files. The
manifest and report categorize ScriptableObjects as "unknown," which is
misleading.

**Fix:** add `".asset": "scriptable_object"` (or similar) to the kind map
and update the asset extractor to honor it.

---

## Top 5 most impactful

For a quick triage view:

1. **No token budget guard on AI transpilation** — silent truncation produces
   incomplete Luau scripts.
2. **No graceful Ctrl+C handling** — partial output state on user cancel.
3. **Validator catches syntax, not Roblox API semantics** — broken scripts
   can ship undetected.
4. **No real AI integration tests** — every AI-side change is unverified
   against the actual API.
5. **No texture op rollback** — pipeline retry can pick up stale partial
   outputs.

Most of these are "real but not blocking" — the converter ships working
output for the test corpus despite them. They become more painful as the
project corpus grows.
