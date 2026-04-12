# Phase 4: Code Transpilation

AI-assisted C# → Luau translation. Each file is translated independently; cross-file semantic gaps are resolved in Phase 4.5.

## Command

```bash
python3 convert_interactive.py transpile <unity_project_path> <output_dir> --api-key <key> 2>/dev/null
```

After review, validate:

```bash
python3 convert_interactive.py validate <output_dir> --write 2>/dev/null
```

The validator (`converter/luau_validator.py`) auto-fixes common Luau quality issues across 50+ categories. `--write` rewrites the files in `<output_dir>/scripts/` in place; omit it to dry-run.

## Structured errors

The transpile command returns JSON. Handle specific error codes instead of blindly retrying:

- `"insufficient_credits"` — Anthropic account is out of credits. Do NOT retry. Surface the message to the user and stop.
- `"auth_failure"` — API key is missing, wrong, or revoked. Do NOT retry. Ask the user to verify the key.
- `"rate_limited"` — Transient. Wait the suggested backoff, then retry once.
- `"batch_review_suggested": true` — Many scripts were flagged. Offer batch review modes before walking per-file.

## Decision: per-script review

**Question:** For each low-confidence script, what should the agent do?

**Factors:**
- How large the diff is. A 10-line helper is cheap to accept; a 500-line state machine is not.
- Whether the script is load-bearing for gameplay (state machine, controller, manager) or peripheral (UI tween, debug logger).
- Whether the transpiler's confidence flags match the actual problems visible in the Luau output.

**Options:**
- **Accept.** Transpiler confidence is high, diff looks clean, or the script is peripheral.
- **Retry with AI.** Clear translation miss (e.g., dropped method, garbled control flow). One retry max before falling back.
- **Edit manually.** Small, localized fix the agent can make without another round trip.
- **Skip.** Script is unused by the primary scene or is pure editor tooling.

**Escape hatch:** If review volume is unmanageable, trust validator results over per-file inspection — let `validate` flag structural issues and focus review on those.

## Review UX

When showing a flagged script:

1. Lead with the C# source and the Luau output side-by-side.
2. Call out the specific lines the transpiler flagged.
3. Name the semantic gap category if it matches one from `phase-4.5-transpiler-gaps.md` (property-as-function, singleton accessor, Inspector ref, etc.) — these are the common failure modes.
4. Decide Accept / Retry / Edit / Skip based on the factors above.
