# Reactive fixup fixtures (Step 4c)

Project-specific Python modules that apply the post-transpile patches
that `/convert-unity`'s Step 4c (Reactive fixups) would normally do
by hand. Lets the offline assembly tests cover Step 4c instead of
silently skipping it.

## What 4c is (per `phase-4c-overview.md`)

> Bootstrap emission, spawner wiring, animator-on-clone bindings, and
> residual transpiler gaps. Intentionally small — anything reliably
> automatable should migrate leftward to 4b.

In other words: the things the deterministic pipeline can't quite
handle for a particular project. Each fixup module is a codification
of "what an agent would do" for one specific project, written so the
test can run it deterministically.

## Module shape

Each `<project_name>_fixups.py` exports:

```python
def apply(output_dir: Path, ctx: ConversionContext) -> None:
    """Apply project-specific reactive fixups after Pipeline.run_all().

    Called AFTER ``pipeline.run_all()`` and BEFORE the test's final
    rbxlx assertions. Safe to no-op when the project needs no
    project-specific fixups.

    Patches go in via:
      - direct file edits under ``output_dir / "scripts"``
      - rbxlx edits via parsed re-write (rare — usually a script edit
        is enough since the rbxlx embeds them)
    """
```

`<project_name>` is the lowercase project name with underscores instead
of hyphens (`SimpleFPS` → `simplefps`, `trash-dash` → `trashdash`).

## How to refresh

1. Run `/convert-unity` interactively against the project up through
   Step 4b transpile.
2. Walk Step 4c per `references/phase-4c-*.md`; record each patch
   you apply.
3. Codify the patches as deterministic file edits in this module's
   `apply()` function.
4. Run `pytest tests/test_offline_assembly.py -m slow` to verify the
   test still passes.

## Why a Python module (not patch files or coherence packs)

- **Patch files** are brittle when transpile output line numbers
  shift — and AI-generated code does shift often.
- **Coherence packs** (in `converter/script_coherence_packs.py`) are
  for UNIVERSAL converter behavior; project-specific test glue
  doesn't belong there.
- **Python module with explicit `apply()`** is composable, can do
  conditional logic, and keeps the test-only concerns out of the
  production converter.
