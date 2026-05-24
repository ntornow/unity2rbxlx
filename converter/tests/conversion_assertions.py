"""conversion_assertions.py -- Shared post-conversion validation helpers.

This module factors out the artifact-level assertion helpers that started
life as test-private (``_assert_*``) functions inside ``test_offline_assembly.py``.
After PR #143 wired ``tools/validate_e2e_conversion.py`` to call the same
checks against a /convert-unity output dir (so /e2e-test inherits the
conversion-half assertions that the previous pytest-based path
enforced), those helpers became dual-use: pytest from
``test_offline_assembly.py``, and non-pytest from the validator + the
/e2e-test SKILL.md seed snippet.

Two reasons for the extraction:

  1. **Visibility.** Production-adjacent code (the validator)
     importing underscore-prefixed test internals is a real smell;
     codex R1 flagged it as a P2 carry-over.
  2. **Import-time side effects.** ``test_offline_assembly.py``
     resolves ``SIMPLEFPS_PROJECT`` / ``TRASHDASH_PROJECT`` at module
     load via ``_resolve_unity_project`` -> ``_load_snapshot`` ->
     ``pytest.skip(...)`` if a snapshot is missing. Importing the
     helpers from outside pytest (validator) used to risk an
     ``_pytest.outcomes.Skipped`` at import time -- masked today
     because both snapshots happen to exist, but latent. This module
     has no import-time side effects, so a non-pytest caller can
     import cleanly regardless of fixture availability.

The helpers themselves are unchanged semantically -- only the names
dropped their leading underscore. ``load_snapshot`` still calls
``pytest.skip`` on a missing fixture (pytest semantics); non-pytest
callers should check ``snapshot_path.exists()`` first OR catch the
resulting ``_pytest.outcomes.Skipped`` by class name (see
``tools/validate_e2e_conversion.py`` for an example).
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures" / "upload_snapshots"


def load_snapshot(name: str) -> dict:
    """Load the committed asset-ID snapshot for *name*.

    Returns the parsed JSON dict. Calls ``pytest.skip`` (not raise)
    when the fixture file is missing -- pytest-driven callers get a
    clean skip; non-pytest callers (validator, SKILL.md seed snippet)
    should check ``_FIXTURES / f'{name}.snapshot.json'``'s existence
    BEFORE calling, or catch ``_pytest.outcomes.Skipped`` by class
    name in a ``BaseException`` filter.
    """
    path = _FIXTURES / f"{name}.snapshot.json"
    if not path.exists():
        pytest.skip(f"snapshot fixture missing: {path}")
    return json.loads(path.read_text())


def seed_output_dir(output_dir: Path, snapshot: dict) -> None:
    """Pre-populate ``output_dir`` so Pipeline sees prior upload+resolve state.

    Writes a minimal ``conversion_context.json`` carrying the snapshot's
    ``uploaded_assets`` / mesh resolution maps, and a ``.roblox_ids.json``
    carrying universe/place IDs. The pipeline loads these on init.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    ctx = {
        "uploaded_assets": snapshot["uploaded_assets"],
        "mesh_native_sizes": snapshot["mesh_native_sizes"],
        "mesh_hierarchies": snapshot["mesh_hierarchies"],
        "universe_id": snapshot.get("universe_id"),
        "place_id": snapshot.get("place_id"),
        "completed_phases": [
            "moderate_assets", "upload_assets", "resolve_assets",
        ],
    }
    (output_dir / "conversion_context.json").write_text(
        json.dumps(ctx, indent=2), encoding="utf-8"
    )

    # Mirror the shared id_cache shape so resolve_assets retarget paths
    # also find the IDs even without ctx.universe_id/place_id.
    if snapshot.get("universe_id") and snapshot.get("place_id"):
        (output_dir / ".roblox_ids.json").write_text(
            json.dumps({
                "universe_id": str(snapshot["universe_id"]),
                "place_id": str(snapshot["place_id"]),
            }),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Artifact-level assertions
# ---------------------------------------------------------------------------

def assert_no_placeholder_ids(rbxlx_path: Path) -> None:
    """Fail if the assembled rbxlx contains unresolved asset references.

    ``rbxassetid://0`` is the placeholder convert_scene emits when an
    asset key is in the scene but missing from ``uploaded_assets``. A
    fully-seeded run should never produce one -- if it does, the
    snapshot is incomplete or a converter regression dropped the lookup.
    """
    text = rbxlx_path.read_text(encoding="utf-8", errors="replace")
    bad = text.count("rbxassetid://0")
    assert bad == 0, (
        f"{rbxlx_path.name} contains {bad} placeholder rbxassetid://0 "
        "references -- either the snapshot is stale (regenerate with "
        "`python u2r.py snapshot-ids`) or a converter regression "
        "dropped an asset lookup."
    )


def assert_snapshot_covers_manifest(
    manifest_assets: list, snapshot: dict, project_name: str,
) -> None:
    """Drift gate: every uploadable asset in the manifest must be snapshotted.

    Two categories count as "covered":
      * ``uploaded_assets`` -- the asset has a real Roblox ID
      * ``asset_upload_errors`` -- the asset was legitimately rejected
        (moderation, network failure that the original run logged)

    Anything else is real drift -- usually a new Unity asset that the
    snapshot was captured before, OR a converter regression that
    started discovering assets that previous runs missed.
    """
    uploaded = snapshot["uploaded_assets"]
    eligible_exts = {
        ".png", ".jpg", ".jpeg", ".bmp", ".tga", ".tif", ".tiff", ".psd",
        ".fbx", ".obj",
        ".mp3", ".ogg", ".wav", ".flac",
    }
    # asset_upload_errors entries are formatted "<rel_path> (<reason>)".
    # Strip the trailing " (...)" suffix so we can match against the
    # bare relative path the manifest uses.
    rejected_paths: set[str] = set()
    for entry in snapshot.get("asset_upload_errors", []) or []:
        s = str(entry)
        paren = s.rfind(" (")
        rejected_paths.add(s[:paren] if paren > 0 else s)

    missing: list[str] = []
    for asset in manifest_assets:
        rel = str(asset.relative_path)
        if Path(rel).suffix.lower() not in eligible_exts:
            continue
        if rel in uploaded or rel in rejected_paths:
            continue
        missing.append(rel)

    assert not missing, (
        f"[{project_name}] snapshot is missing {len(missing)} asset(s) "
        f"the Unity project now references -- first 5: {missing[:5]!r}. "
        f"Regenerate with: python u2r.py snapshot-ids <output_dir> -o "
        f"tests/fixtures/upload_snapshots/{project_name}.snapshot.json"
    )


def assert_mesh_ids_match_snapshot(rbxlx_path: Path, snapshot: dict) -> None:
    """Every MeshId in the assembled rbxlx must be a snapshot-known ID.

    Catches the case where the assembly fabricates IDs (e.g. accidental
    string concatenation, default integer) instead of looking them up
    from the seeded ``uploaded_assets`` + ``mesh_hierarchies`` tables.
    Per-row failure surface, not just a count.
    """
    snapshot_ids = set(snapshot["uploaded_assets"].values())
    # mesh_hierarchies entries carry the real per-sub-mesh ``meshId``
    # (already in rbxassetid:// form) plus ``textureId`` for materials
    # baked into the sub-mesh. Flatten both into the legal set so the
    # MeshId references the rbxlx emits (one per sub-mesh, not one per
    # uploaded FBX) all match something the snapshot covered.
    for entries in snapshot.get("mesh_hierarchies", {}).values():
        for e in entries:
            if not isinstance(e, dict):
                continue
            for field in ("meshId", "textureId"):
                ref = e.get(field)
                if ref:
                    snapshot_ids.add(str(ref))

    text = rbxlx_path.read_text(encoding="utf-8", errors="replace")
    # MeshId is wrapped in <Content name="MeshId"><url>rbxassetid://N</url></Content>
    pattern = re.compile(
        r'<Content[^>]*name="MeshId"[^>]*>\s*<url>(rbxassetid://\d+)</url>',
        re.MULTILINE,
    )
    unknown: list[str] = []
    for m in pattern.finditer(text):
        ref = m.group(1)
        if ref not in snapshot_ids:
            unknown.append(ref)

    # Allow up to ~3 unknowns to absorb autogen meshes the converter
    # may add (FPS_Weapon_Mount viewmodel, GroundCollider, etc.) that
    # don't come from the upload pipeline. A larger number signals
    # an assembly regression.
    assert len(unknown) <= 3, (
        f"{rbxlx_path.name} references {len(unknown)} mesh IDs not in "
        f"the snapshot (first 5: {unknown[:5]!r}). Snapshot covered "
        f"{len(snapshot_ids)} known IDs. Either fabricated by the "
        f"converter or the snapshot is stale."
    )


def _module_script_source(rbxlx_path: Path, module_name: str) -> str:
    """Return the embedded Source of the ModuleScript named *module_name*.

    Parses the rbxlx with ElementTree, finds the ``<Item
    class="ModuleScript">`` whose ``<string name="Name">`` matches, and
    returns the text of its ``<ProtectedString name="Source">`` (CDATA is
    surfaced as element text). Returns ``""`` when the module is absent so
    callers can assert on emptiness with a clear message.

    Kept underscored: internal helper for ``assert_generic_scene_runtime``,
    no other caller in or out of this module.
    """
    tree = ET.parse(rbxlx_path)
    for item in tree.iter("Item"):
        if item.get("class") != "ModuleScript":
            continue
        props = item.find("Properties")
        if props is None:
            continue
        name_el = props.find('./string[@name="Name"]')
        if name_el is None or (name_el.text or "") != module_name:
            continue
        src_el = props.find('./ProtectedString[@name="Source"]')
        return (src_el.text or "") if src_el is not None else ""
    return ""


def assert_generic_scene_runtime(rbxlx_path: Path) -> None:
    """Generic-mode assertions: tier-1 prefab placements reach the host.

    1. The embedded ``SceneRuntimePlan`` ModuleScript carries
       ``scene_prefab_placements`` with > 50 placement rows (SimpleFPS has
       ~252; we count ``placement_id`` occurrences in the plan source).
    2. The embedded ``SceneRuntime`` ModuleScript source defines
       ``_constructPrefabClone`` (the boot path that instantiates the
       pre-placed prefab clones).
    """
    plan_src = _module_script_source(rbxlx_path, "SceneRuntimePlan")
    assert plan_src, (
        "generic mode produced no SceneRuntimePlan ModuleScript -- the "
        "scene-runtime host plan was never embedded."
    )
    assert "scene_prefab_placements" in plan_src, (
        "SceneRuntimePlan does not carry scene_prefab_placements -- the "
        "planner did not emit prefab placements or autogen dropped the "
        "_PLAN_KEYS_FOR_HOST key."
    )
    placement_rows = plan_src.count("placement_id")
    assert placement_rows > 50, (
        f"SceneRuntimePlan carries only {placement_rows} placement rows "
        f"(expected > 50; SimpleFPS has ~252). Prefab placements are not "
        f"reaching the embedded plan."
    )

    runtime_src = _module_script_source(rbxlx_path, "SceneRuntime")
    assert runtime_src, (
        "generic mode produced no SceneRuntime ModuleScript -- the "
        "scene-runtime host was never embedded."
    )
    assert "_constructPrefabClone" in runtime_src, (
        "SceneRuntime source is missing _constructPrefabClone -- the "
        "tier-1 prefab-clone boot path is absent from the host runtime."
    )


def assert_place_builder_chunks_publishable(rbx_place) -> None:
    """Generate the place-builder Luau chunks and assert publish viability.

    No cloud call -- just runs the same chunker the publish step would,
    then asserts the largest chunk is under the 4MB execute_luau cap
    so we'd catch a regression that ballooned the script output.
    """
    from roblox.luau_place_builder import generate_place_luau_chunked
    from roblox.place_publisher import MAX_EXECUTE_LUAU_BYTES

    chunks = generate_place_luau_chunked(rbx_place)
    assert chunks, "place builder produced zero chunks -- the builder bailed"

    max_chunk = max(len(c) for c in chunks)
    assert max_chunk < MAX_EXECUTE_LUAU_BYTES, (
        f"largest place-builder chunk is {max_chunk:,} bytes "
        f"(cap: {MAX_EXECUTE_LUAU_BYTES:,}). A real publish would fail."
    )


def run_luau_analyze(scripts_dir: Path) -> tuple[int, int, list[str]]:
    """Run luau-analyze over every .luau under ``scripts_dir``.

    Uses the shared ``utils.luau_analyze`` helper, which filters output to
    SyntaxError lines only -- TypeError noise for Roblox-specific globals
    and lint warnings (FunctionUnused, etc.) are intentionally ignored so
    the gate only fires on actual parse failures. The shared helper also
    no-ops cleanly when luau-analyze is not installed, so this test still
    runs in CI environments that don't ship it.
    """
    from utils.luau_analyze import luau_analyze_path, syntax_errors_for_file

    if not luau_analyze_path():
        return (0, 0, ["luau-analyze not installed -- skipping syntax check"])

    luau_files = list(scripts_dir.rglob("*.luau"))
    if not luau_files:
        return (0, 0, [])

    failures: list[str] = []
    passed = 0
    for lf in luau_files:
        errs = syntax_errors_for_file(lf)
        if errs:
            failures.append(
                f"{lf.relative_to(scripts_dir)}: " + "; ".join(errs[:3])
            )
        else:
            passed += 1
    return (passed, len(failures), failures)
