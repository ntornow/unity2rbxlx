# Phase 5: Moderate & Assemble

First moderates all project assets against Roblox's published safety standards, then uploads them, resolves real MeshIds, converts the scene tree, and writes the `.rbxlx` with `rbxassetid://` URLs embedded. **Moderation runs before any upload** — if any asset violates Roblox's standards, halt and do not run `assemble` until the offending assets are removed or the user explicitly overrides.

## Sub-step 1: Moderate assets (before upload)

Screen every asset in `AssetManifest` (built by Phase 2) against the standards below. Do not run `assemble` if you find a violation.

### Standards to enforce

| # | Document | Link |
|---|---|---|
| 1 | Roblox Community Standards | https://en.help.roblox.com/hc/en-us/articles/203313410-Roblox-Community-Standards |
| 2 | Content Moderation on Roblox | https://en.help.roblox.com/hc/en-us/articles/21416271342868-Content-Moderation-on-Roblox |
| 3 | Restricted Content Policy | https://en.help.roblox.com/hc/en-us/articles/15869919570708-Restricted-Content-Policy |
| 4 | Content Maturity Labels | https://en.help.roblox.com/hc/en-us/articles/8862768451604-Content-Maturity-Labels |
| 5 | Content Maturity & Compliance (creator docs) | https://create.roblox.com/docs/production/promotion/content-maturity |
| 6 | Roblox Terms of Use | https://en.help.roblox.com/hc/en-us/articles/115004647846-Roblox-Terms-of-Use |

Fetch live pages if online; fall back to the summary below if a page is unreachable. Never skip screening because a fetch failed.

### Summary (authoritative defaults)

Roblox organises its Community Standards into four pillars, plus a maturity tier system and layered Terms of Use / DMCA rules. All apply to uploaded assets (images, meshes, audio, scripts, text, filenames).

- **Safety** — no content sexualising or endangering minors (CSAM is an absolute block); no glorification of real-world violence, terrorism, self-harm, or illegal drugs.
- **Civility** — no hate speech, slurs, or dehumanising content targeting protected classes; no harassment or doxxing.
- **Integrity** — no scams, phishing, impersonation, cheating, or IP infringement (copyright / trademark / DMCA). Audio is scanned for music IP specifically.
- **Security** — no PII leaks, no credential / cookie / token exfiltration patterns in scripts, no pushing users off-platform.
- **Maturity tiers** — Minimal / Mild / Moderate / Restricted. Strong realistic violence, heavy gore, romantic themes, alcohol, and strong language require the 17+ Restricted tier.
- **Never allowed at any tier:** CSAM, sexual content with minors, real hate symbols, glorification of mass violence, real PII exposure, IP infringement.

### Per-kind screening

- **Textures / decals / sprites** — Read the image (the multimodal model can inspect it). Flag CSAM, nudity, gore, hate symbols, real-world brand logos, QR codes, and readable text containing slurs, PII, or off-platform URLs.
- **Meshes / models** — screen the asset name for weapon-replica or IP-infringing terms; if embedded textures are referenced, screen those too.
- **Audio** — Read file header + ID3 / Vorbis tags; flag filenames that look like copyrighted songs (title + artist), slurs, or profanity above the target maturity tier.
- **Scripts / text** — open and scan for slurs, PII-harvesting patterns (HttpService POST to arbitrary URLs, cookie / token exfil, off-platform links), and disallowed content references. Include filenames and folder segments in the text screen.

### Classification

For each asset emit one of:
- `OK` — nothing flagged.
- `WARNING` — borderline (ambiguous brand logo, stylised weapon, mild language). Requires human review.
- `VIOLATION` — clearly offending under the standards above.

Every `WARNING` / `VIOLATION` must cite the pillar (Safety / Civility / Integrity / Security / Maturity / ToU-DMCA) and the source-document row from the table above.

### Report

Write a JSON report to `<output_dir>/asset_safety_report.json`:

```json
{
  "project": "...",
  "checked": 0,
  "counts": {"ok": 0, "warning": 0, "violation": 0},
  "findings": [
    {"relative_path": "...", "kind": "texture",
     "classification": "VIOLATION", "standards": ["Integrity"],
     "evidence": "real-world brand logo", "source_document": "#1"}
  ]
}
```

Also log a `[moderation]` line with counts and the first few violations.

### Halting rules

- `violation > 0` → **STOP**. Do not run `assemble`. Surface findings to the user and wait for them to remove offending assets or explicitly ask you to skip moderation.
- `warning > 0`, `violation == 0` → surface the warnings and ask the user to confirm. In non-interactive mode, stop by default.
- All clear → proceed to Sub-step 2.

## Sub-step 2: Assemble

```bash
python3 convert_interactive.py assemble <unity_project_path> <output_dir> \
  --api-key ../apikey --creator-id ../creator_id 2>/dev/null
```

Use `--no-upload` for a dry-run with placeholder URLs, or `--no-resolve` to skip the headless mesh resolver.

### Pipeline phases run

1. **upload_assets** (`roblox/cloud_api.py`) — textures (Image), meshes (Model), audio. Returns `rbxassetid://` URLs embedded directly in the .rbxlx.
2. **resolve_assets** (`roblox/studio_resolver.py`) — headless mesh resolver via Luau Execution API; calls `CreateMeshPartAsync` to capture real `MeshId` + `InitialSize` + sub-mesh hierarchy.
3. **convert_animations** — `.anim` / `.controller` → TweenService scripts or animator config. Auto-injects runtime modules (animator, nav mesh, event system, physics bridge, cinemachine, sub-emitter, pickup).
4. **convert_scene** — walks the parsed tree, produces typed `RbxPlace` (`core/roblox_types.py`).
5. **write_output** — serialises to `<output_dir>/converted_place.rbxlx`.

## Terrain

SmoothGrid encoding is reverse-engineered (6-bit material + occupancy + RLE, axis swap, 22 materials), so terrain renders directly in Studio without a runtime FillBlock loader. If extraction fails, a FillBlock fallback script is generated.

## LFS requirement

If a terrain `.asset` is a Git LFS pointer (starts with `version https://git-lfs`), the pipeline warns but cannot extract data. Run `git lfs install && git lfs pull`. LFS pointers for textures, meshes, and audio are detected and skipped.

## Decision: asset upload failures

**Question:** If some uploads fail mid-stage, what should the agent do?

**Factors:**
- Failure type. Rate-limit errors are transient; content-policy rejections are permanent (and should have been caught in Sub-step 1 — if they weren't, tighten the moderation pass).
- Criticality. Hero meshes matter; background props don't.
- Percentage failed.

**Options:**
- **Retry.** Transient errors only — wait and retry once.
- **Continue without.** Low failure rate on non-critical assets.
- **Abort.** High failure rate or critical assets failing — investigate.

**Escape hatch:** Per-asset status is in `conversion_context.json`. Re-running `assemble` reuses already-uploaded assets from the context.

## Decision: terrain verification

If terrain was found and processed, open the assembled `.rbxlx` in Studio and verify visually. Terrain bugs (wrong scale, wrong materials, missing water) are much easier to catch here than post-upload.
