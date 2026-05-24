# Conversion plan fixtures (Step 4a)

Project-specific `conversion_plan.json` content that the offline
assembly tests seed BEFORE `Pipeline.run_all()` runs, so the test
covers Step 4a of the `/convert-unity` workflow (which is otherwise
agent-driven and skipped by direct Pipeline invocation).

## What the pipeline actually consumes today

The pipeline writes its own `conversion_plan.json` during a run, then
the next phase reads selected keys back from it. Only a subset of the
4a-overview's keys are wired through:

| Key | Wired? | How |
|---|---|---|
| `storage_plan` | partial | `_load_storage_plan_for_rehydration` reads it during the preserved-scripts rehydration path. Classifier rerun overwrites it. |
| `scene_runtime.domain_overrides` | yes | `_merge_scene_runtime(plan_path)` PRESERVES operator-authored overrides across classifier reruns. THIS is the load-bearing 4a knob today. |
| `scene_runtime.modules` | yes (read-only) | Migration of pre-v2 `"legacy"` domain values runs against on-disk content. |
| `architecture_map` | NO | Documented in `phase-4a-overview.md` but pipeline does not consume. |
| `divergence_overrides` | NO | Same — aspirational. |
| `templates_manifest` | NO | `storage_classifier.py:139` notes "Forward-looking: templates_manifest is not yet wired through the pipeline." |
| `module_boundaries` | NO | Aspirational. |

So a fixture in this directory should put its real 4a leverage in
`scene_runtime.domain_overrides`. The other keys are forward-looking
placeholders — fine to include for documentation but won't change
conversion output today.

## Format

```jsonc
{
  "scene_runtime": {
    "domain_overrides": {
      // <script_stem>: "client" | "server" | "helper" | "excluded"
      // e.g. "Turret": "server", "HudControl": "client"
    }
  },

  // Forward-looking — not wired through pipeline yet but documented
  // so they live alongside the operative override:
  "architecture_map": { ... },
  "divergence_overrides": { ... },
  "templates_manifest": { ... },
  "module_boundaries": { ... }
}
```

## How to refresh

1. Run `/convert-unity` interactively against the project, executing
   the full Step 4a sub-phase walk per `references/phase-4a-*.md`.
2. Save the resulting `<output>/conversion_plan.json` here as
   `<ProjectName>.plan.json`, stripping pipeline-generated keys
   (`storage_plan`, `script_paths`, `animation_routing`,
   `scene_runtime.modules/scenes/prefabs/...`) — keep ONLY the
   agent-authored overrides + the aspirational documentation keys.
3. Run `pytest tests/test_offline_assembly.py -m slow` to verify the
   test still passes with the new plan.
