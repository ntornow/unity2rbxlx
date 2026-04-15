---
name: asset-safety-validation
description: Validate a Unity project's assets against Roblox's published safety standards before the unity2rbxlx converter uploads them. Invoke this skill during the `extract_assets` phase of the converter pipeline (before `upload_assets`) whenever a conversion is about to publish a game. It first gathers and summarizes every Roblox safety standard that applies to uploaded content, then performs a per-asset check against those standards, flags any offending assets, and halts the export so the user can review before publishing.
---

# Asset Safety Validation

This skill guards the unity2rbxlx converter against publishing assets that would violate Roblox's Community Standards, Terms of Use, or content moderation policies. It runs in two phases:

1. **Gather & summarize** the safety standards Roblox publishes.
2. **Check each asset** in the project's asset manifest against those standards, flag offenders, and block the export if anything is offending.

## When to invoke

Invoke this skill whenever the converter is about to upload assets to Roblox — specifically after `unity.asset_extractor.extract_assets()` has built the `AssetManifest` and before `converter.converter.pipeline.Pipeline.upload_assets()` begins. The converter pipeline has a hook in the `extract_assets` phase that delegates to this skill. If any asset is flagged, the pipeline must NOT proceed to `upload_assets`; report findings to the user and stop.

This skill is also runnable on demand, e.g. "validate the assets in `test_projects/SimpleFPS` against Roblox safety standards".

---

## Phase 1 — Gather all available Roblox safety standards

Before checking anything, load the full set of standards into context. Start from the summaries below (which are always authoritative for this skill's defaults), and also fetch the linked pages for the latest wording if the user is online. If any fetch fails (e.g. 403), fall back to the embedded summaries — do NOT skip validation just because a page is unreachable.

### Primary Roblox safety documents

| # | Document | Link |
|---|----------|------|
| 1 | Roblox Community Standards (the umbrella policy) | https://en.help.roblox.com/hc/en-us/articles/203313410-Roblox-Community-Standards |
| 2 | Roblox Community Standards (about.roblox.com landing page) | https://about.roblox.com/community-standards |
| 3 | Content Moderation on Roblox (how uploads are screened) | https://en.help.roblox.com/hc/en-us/articles/21416271342868-Content-Moderation-on-Roblox |
| 4 | Restricted Content Policy (what's allowed only in 17+ experiences) | https://en.help.roblox.com/hc/en-us/articles/15869919570708-Restricted-Content-Policy |
| 5 | Content Maturity Labels (Minimal / Mild / Moderate / Restricted tiers) | https://en.help.roblox.com/hc/en-us/articles/8862768451604-Content-Maturity-Labels |
| 6 | Content Maturity and Compliance (creator docs) | https://create.roblox.com/docs/production/promotion/content-maturity |
| 7 | Advertising Standards | https://en.help.roblox.com/hc/en-us/articles/13722260778260-Advertising-Standards |
| 8 | Safety & Civility at Roblox | https://en.help.roblox.com/hc/en-us/articles/4407444339348-Safety-Civility-at-Roblox |
| 9 | Roblox Terms of Use | https://en.help.roblox.com/hc/en-us/articles/115004647846-Roblox-Terms-of-Use |
| 10 | Asset privacy (creator docs) | https://create.roblox.com/docs/projects/assets/privacy |
| 11 | Creator Store distribution rules | https://github.com/Roblox/creator-docs/blob/main/content/en-us/production/creator-store.md |
| 12 | Audio assets rules (creator docs) | https://github.com/Roblox/creator-docs/blob/main/content/en-us/audio/assets.md |
| 13 | Safety Tools and Policies | https://about.roblox.com/safety-tools |

If a document is updated and contradicts the summary below, prefer the live document and note the delta in the report.

### Summary of the standards (authoritative defaults)

Roblox organizes its Community Standards into four pillars — **Safety, Civility, Integrity, and Security** — plus a separate **Restricted Content Policy** that defines what may appear only in 17+ experiences, and layered **Terms of Use / DMCA / Advertising** rules. Everything below applies to uploaded assets (images, meshes, audio, video, text, scripts).

**Pillar 1 — Safety.** Prohibits content that sexualizes, exploits, or endangers minors (including any CSAM); content that glorifies, promotes, or instructs real-world violence, terrorism, or self-harm; depictions of suicide or eating disorders framed as aspirational; illegal drugs; and dangerous "challenge" content. This is the category used for child safety image classifiers on upload.

**Pillar 2 — Civility.** Prohibits hate speech, slurs, and content that dehumanizes or discriminates against protected groups (race, ethnicity, national origin, religion, gender identity, sexual orientation, disability, veteran status). Also prohibits harassment, bullying, threats, and doxxing.

**Pillar 3 — Integrity.** Prohibits scams, phishing, impersonation, cheating/exploits, misleading content, off-platform trade of Robux or items, and copyright / trademark / IP infringement (including DMCA violations). Audio uploads are scanned for IP infringement specifically.

**Pillar 4 — Security.** Prohibits content that leaks or solicits personally identifiable information, account credentials, or that encourages users to move to unmoderated off-platform spaces. Covers malicious scripts that attempt to steal cookies, tokens, or bypass Roblox's trust & safety systems.

**Restricted Content Policy — content maturity tiers.** Each Roblox experience is labeled on a four-tier scale. The tiers are cumulative (higher tiers allow everything in lower tiers):

- **Minimal (all ages):** Occasional mild unrealistic violence, light cartoon blood, occasional mild fear.
- **Mild (9+):** Repeated mild violence, heavy unrealistic blood, mild crude humor, repeated mild fear.
- **Moderate (13+):** Moderate violence, light realistic blood, moderate crude humor, unplayable gambling references, moderate fear.
- **Restricted (17+, ID-verified):** Strong violence, heavy realistic blood, romantic themes, presence of alcohol, strong language, unplayable gambling content, moderate fear. Private-space settings (bedrooms, bathrooms, bars, clubs) are restricted to this tier.

**Never allowed, at any tier:** CSAM; sexual content involving minors or between players; sexually explicit content of any kind in the main (non-17+) catalog; real-world hate symbols targeting protected classes; glorification of real-world terrorism or mass violence; content that enables self-harm; illegal-drug promotion; content that exposes real personal information; and content infringing third-party IP.

**Advertising Standards.** Advertising assets (decals, thumbnails, icons, banners) carry the strictest bar: no shocking or misleading imagery, no "dark patterns," no references to real-world brands the creator doesn't own, no sensational violence, no romantic/suggestive imagery regardless of the target experience's maturity tier.

**Terms of Use / DMCA.** Everything uploaded must be (a) owned by the creator or properly licensed, (b) not infringing on any third party's copyright, trademark, or right of publicity, and (c) not a derivative of another Roblox creator's work without permission.

**Asset-type-specific notes:**
- **Images / textures / decals:** screened for CSAM, nudity, gore, hate symbols, real-world brand logos, QR codes that lead off-platform, and text containing slurs, PII, or off-platform links.
- **Meshes / models:** screened for sexual anatomy, weapon replicas that model real firearms with intent to glorify violence, hate symbols baked into geometry or UVs, and IP-infringing likenesses (characters, vehicles, architecture).
- **Audio:** screened for copyrighted music (IP infringement), slurs, profanity above the experience's maturity tier, and sexual content. Audio distribution to other creators additionally requires an ID-verified account.
- **Video:** same bar as images plus audio; additionally flagged for flashing content that may trigger photosensitive epilepsy.
- **Scripts / text:** screened for slurs, PII harvesting, off-platform links, obvious malicious payloads (cookie/token exfiltration), and references to prohibited real-world content.
- **Filenames & metadata:** the file name itself, its folder path, and any metadata strings count as "text" and are screened the same way as in-asset text.

---

## Phase 2 — Check the project's assets against the standards

Once Phase 1 has finished, walk the Unity project's `AssetManifest` (built by `unity.asset_extractor.extract_assets`) and check every asset. The manifest groups assets by `kind` (`texture`, `mesh`, `audio`, `video`, `script`, `material`, `font`, `other`).

### Inputs

- `manifest: AssetManifest` — produced by `extract_assets()`.
- `unity_project_path: Path` — the Unity project root.
- Optionally, the user's self-declared maturity tier (default: `Minimal`).

### Procedure

For each asset in `manifest.assets`:

1. **Record identity** — `relative_path`, `kind`, `size_bytes`, and a short hash or id so the report can reference it unambiguously.
2. **Screen filename & path.** Check the filename, folder segments, and (if present) `AssetEntry.metadata` / `.meta` labels against the standards in Phase 1. Apply to every asset regardless of kind.
3. **Screen by asset kind**, using the asset-type-specific notes from Phase 1:
   - `texture` / image → visually inspect the image for the disallowed visual categories (CSAM, nudity, gore, hate symbols, real-brand logos, QR codes, disallowed text). Use the Read tool on the image file so the multimodal model can actually look at it. Do not rely on filename alone.
   - `mesh` → inspect the mesh's embedded textures (if any FBX/OBJ materials reference baked textures, screen those too) and the mesh name. Flag obvious weapon/IP replicas by name; geometry-level inspection is best-effort.
   - `audio` → read the file header with Read and, if feasible, play or transcribe a sample; at minimum check the filename, id3/vorbis metadata tags, and the asset's GUID reference in the project for likely copyrighted-music names.
   - `video` → same as audio plus flag flashing/strobe content if metadata indicates it.
   - `script` / text → open the file and scan for slurs, PII-harvesting patterns (`HttpService` posts to arbitrary URLs, cookie/token exfil, off-platform links), and disallowed content references.
   - `material` → check textures it references (via its `.mat` guid resolution) and its name.
   - `font` → check the font's filename and any glyph samples available.
4. **Classify each finding** as one of:
   - `OK` — nothing flagged.
   - `WARNING` — borderline; requires human review (e.g. ambiguous brand logo, stylized weapon, mild language).
   - `VIOLATION` — clearly offending under Phase 1's standards (e.g. real-world brand logo, likely copyrighted song, slur in filename, depiction of real violence).
5. **Attach evidence.** For every `WARNING` or `VIOLATION`, record the specific standard it violates (cite the pillar — Safety / Civility / Integrity / Security — and, if applicable, the specific document from the Phase 1 table).

### Output format

Return a structured report with these fields:

```
{
  "project": "<relative path to unity project>",
  "manifest_size": <int>,
  "checked": <int>,
  "counts": {"ok": <int>, "warning": <int>, "violation": <int>},
  "findings": [
    {
      "relative_path": "...",
      "kind": "texture|mesh|audio|video|script|material|font|other",
      "classification": "OK|WARNING|VIOLATION",
      "standards": ["Safety", "Integrity / DMCA", ...],
      "evidence": "<short human-readable explanation>",
      "source_document": "<link from Phase 1 table>"
    }
  ],
  "summary_for_user": "<1-3 sentence plain-English summary the converter prints to the terminal>"
}
```

### Halting behaviour

- If `counts.violation > 0`: the skill MUST tell the converter to **stop the export**. The converter prints the violations to the terminal and does not proceed to `upload_assets`. The user has to acknowledge / remove offending assets (or explicitly override with a flag like `--skip-safety-check`, which is logged) before retrying.
- If `counts.violation == 0` and `counts.warning > 0`: the skill prints the warnings and asks the user to confirm before the converter proceeds. In non-interactive mode the converter stops by default and requires `--accept-warnings` to continue.
- If `counts.violation == 0` and `counts.warning == 0`: the converter proceeds to `upload_assets` normally.

### Determinism & logging

- Write the full JSON report to `<output_dir>/asset_safety_report.json` so it can be inspected after the fact.
- Also append a plain-text summary to the converter's log with the prefix `[asset_safety]` for grep-ability.
- Never upload anything, never make external network calls for the assets themselves, and never cite a standard the skill hasn't loaded in Phase 1.

---

## Notes for Claude when running this skill

- Use Read to load any image / audio / script asset you need to look at directly. The multimodal model can inspect images in-context.
- If the asset set is large (thousands of files), process by `kind` in the order above and summarize per-kind before moving to the next. Prefer small focused reads.
- Be honest about uncertainty. If you cannot verify an asset (e.g. binary audio that you cannot transcribe), classify as `WARNING` with evidence `"could not verify, manual review required"` rather than marking `OK`.
- Never delete or modify assets — only report.
- Cite a specific Phase 1 document link in every finding's `source_document` field.
