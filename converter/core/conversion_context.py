"""
conversion_context.py -- Mutable state across pipeline phases.

Serializable to JSON for pause/resume support.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path
from typing import ClassVar, TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from converter.storage_classifier import StoragePlan


class MeshHierarchyEntry(TypedDict, total=False):
    """One sub-mesh inside an FBX, as resolved by Roblox Studio LoadAsset."""

    name: str
    meshId: str
    size: list[float]      # [x, y, z]
    position: list[float]  # [x, y, z]
    textureId: str


class SceneMetadata(TypedDict):
    """Per-scene conversion stats produced by multi-scene pipeline runs."""

    parts: int
    scripts: int
    game_objects: int


@dataclass
class ConversionContext:
    """Tracks state across conversion pipeline phases."""

    # Input
    unity_project_path: str = ""
    scene_paths: list[str] = field(default_factory=list)
    selected_scene: str = ""

    # Phase completion
    completed_phases: list[str] = field(default_factory=list)
    current_phase: str = ""

    # Asset tracking
    uploaded_assets: dict[str, str] = field(default_factory=dict)  # local_path -> rbxassetid
    asset_upload_errors: list[str] = field(default_factory=list)

    # Roblox IDs
    universe_id: int | None = None
    place_id: int | None = None
    experience_name: str = ""

    # Conversion stats
    total_game_objects: int = 0
    converted_parts: int = 0
    total_scripts: int = 0
    transpiled_scripts: int = 0
    # Count of top-level ``scripts/*.luau`` on disk AFTER the dead-module prune.
    # ``scripts_cache_intact`` compares the live on-disk count against THIS, not
    # the pre-prune ``transpiled_scripts`` (the prune deletes dead modules from
    # disk after ``transpiled_scripts`` is set, so the old comparison always
    # missed and re-transpiled a clean cache). -1 = not yet recorded (a real
    # count is always >= 0, so 0 is distinguishable from "unset"); see
    # ``expected_cached_script_count``.
    cached_script_count: int = -1
    total_materials: int = 0
    converted_materials: int = 0
    total_animations: int = 0
    converted_animations: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # Mesh resolution (Model ID -> real MeshId + native sizes).
    # Value is [x, y, z] floats (the FBX's overall bounding box).
    mesh_native_sizes: dict[str, list[float]] = field(default_factory=dict)

    # Mesh texture IDs (asset path -> rbxassetid:// URL for TextureID embedded in uploaded FBX models)
    mesh_texture_ids: dict[str, str] = field(default_factory=dict)

    # Full mesh hierarchy from Roblox LoadAsset:
    # fbx_path -> list of MeshHierarchyEntry (one per sub-mesh).
    mesh_hierarchies: dict[str, list[MeshHierarchyEntry]] = field(default_factory=dict)

    # FBX bounding boxes computed via trimesh (fallback for InitialSize when Studio
    # resolution is unavailable).  Maps relative asset path -> (w, h, d) in FBX units.
    fbx_bounding_boxes: dict[str, list[float]] = field(default_factory=dict)

    # Multi-scene metadata: scene_name -> per-scene stats.
    scenes_metadata: dict[str, SceneMetadata] = field(default_factory=dict)

    # Comparison results — populated by the comparison subsystem when a
    # visual diff runs. Shape varies by comparison type; using `object` to
    # require narrowing at consumption sites rather than smuggling Any.
    comparison_scores: dict[str, object] = field(default_factory=dict)

    # Storage plan (Phase 4a.5): explicit per-script container assignments.
    # Produced by converter.storage_classifier.classify_storage and written to
    # conversion_plan.json. rbxlx_writer reads each script's parent_path when
    # emitting; falls back to script_type heuristics when absent.
    # Reconstructed from dict shape on JSON load via __post_init__.
    storage_plan: StoragePlan | None = None

    # GUID -> sliced sprite PNG path (from sprite_extractor).
    sprite_guid_to_file: dict[str, str] = field(default_factory=dict)

    # Per-script serialized field references.  Shape:
    #   { relative_cs_path: { field_name: prefab_name_or_audio_ref } }
    # Populated after parse, before transpile_scripts. Consumed by the
    # transpiler (so the AI prompt knows which fields point at prefabs)
    # and by the prefab-package emitter (to know which prefabs to emit).
    serialized_field_refs: dict[str, dict[str, str]] = field(default_factory=dict)

    # Project-level scene runtime artifact emitted by the
    # ``plan_scene_runtime`` phase. Shape is pinned by
    # ``converter.scene_runtime_planner.SceneRuntimeArtifact`` (modules /
    # scenes / prefabs / domain_overrides). Stored loosely typed here to
    # avoid a core→converter dependency; consumers narrow at the call site.
    # Carries the structural blocks plus per-module ``domain`` / ``container`` /
    # ``module_path``. Survives ``_classify_storage`` rewrites verbatim — see
    # ``Pipeline._classify_storage``.
    scene_runtime: dict[str, object] = field(default_factory=dict)

    # Opt-in genre scaffolding requested by the caller. Persisted as a
    # sorted ``list[str]`` for JSON-friendliness; the pipeline reads it
    # back as a frozenset. Currently recognised: ``"fps"`` (auto-injected
    # FPS client controller + HUD ScreenGui + HUDController LocalScript).
    # Round-trips through ``conversion_context.json`` so resumed builds
    # (``u2r.py publish`` rebuild path, ``convert_interactive upload``
    # / ``assemble`` re-run) reproduce the same place contents instead
    # of dropping the FPS scripts because the in-memory Pipeline default
    # is empty.
    scaffolding: list[str] = field(default_factory=list)

    # Requested scene-runtime contract mode. One of
    # ``"legacy"`` / ``"auto"`` / ``"generic"``. Plumbed in from the
    # front-door commands (``u2r convert/publish/eval``,
    # ``convert_interactive transpile/assemble/upload``); read by
    # ``Pipeline._classify_storage`` to gate the domain classifier
    # so the legacy path stays byte-identical.
    scene_runtime_mode: str = "legacy"

    # Requested networking mode for the domain classifier.
    # ``"none"`` (default): single-player Unity ports — fallback = client.
    # ``"mirror"`` / ``"netcode"``: networked Unity projects — fallback =
    # server, Mirror-only signals active.
    # See ``converter/docs/design/scene-runtime-domain-signals.md``.
    networking_mode: str = "none"

    # When True, the domain classifier still runs but the
    # pipeline rejects the conversion if any runtime-bearing module
    # comes out ``"excluded"`` or ``low_confidence`` after override
    # application. Default off so iteration cycles aren't blocked.
    strict_classification: bool = False

    # Module names the post-coherence dead-module pass flagged Roblox-dead
    # on the run that transpiled. Persisted
    # as a sorted ``list[str]`` for JSON-friendliness; the pipeline reads it back
    # as a frozenset. Round-trips through ``conversion_context.json`` so a
    # no-transpile resume (preserve-scripts / ``--phase=write_output``, where
    # ``transpilation_result`` is None and the input prior cannot be recomputed)
    # reuses the prior verdict instead of re-routing the previously-dead modules
    # back into ServerStorage. Empty by default.
    dead_modules: list[str] = field(default_factory=list)

    # Cross-script C# dependency graph (class_name -> referenced class_names),
    # built only inside ``transpile_scripts`` (pipeline.py). Persisted here so a
    # transpile-skipped ``assemble`` (cache intact since #222 — separate
    # process, ``transpilation_result is None``) can rehydrate the
    # topology-quality caller graph the storage classifier needs, instead of
    # collapsing to the empty-graph legacy path that misroutes client-reachable
    # ModuleScripts into ServerStorage (-> client ``require(nil)``). Keyed on
    # ``class_name`` (stable across phases — the same identity transpile uses);
    # round-trips through ``conversion_context.json`` via ``asdict``/``cls(**data)``.
    # Old context files lacking this field load with the empty default, which
    # fails the routing gate closed to legacy (backward-compatible). Mirrors the
    # ``dead_modules`` "no-transpile resume loses a transpile-derived verdict"
    # precedent above.
    dependency_map: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # JSON load via `cls(**data)` populates storage_plan as a dict (the
        # asdict() form). Reconstruct it as a StoragePlan when present so the
        # field's declared type holds at runtime. Lazy import avoids the
        # core <-> converter dependency cycle.
        if isinstance(self.storage_plan, dict):
            from converter.storage_classifier import StoragePlan
            self.storage_plan = StoragePlan(**self.storage_plan) if self.storage_plan else None

    def mark_phase_complete(self, phase: str) -> None:
        if phase not in self.completed_phases:
            self.completed_phases.append(phase)
        self.current_phase = ""

    def expected_cached_script_count(self) -> int:
        """Count a transpile-cache check should compare on-disk scripts against:
        the recorded post-prune ``cached_script_count`` when set (>= 0), else the
        pre-prune ``transpiled_scripts`` (older contexts, or before the first
        full run recorded a count). The first assemble runs before any prune, so
        the pre-prune fallback is still correct there."""
        if self.cached_script_count >= 0:
            return self.cached_script_count
        return self.transpiled_scripts

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # Fields that reveal Roblox creator identity, universe/place ownership,
    # or uploaded asset URLs — anything that should be stripped before
    # sharing a conversion_context.json outside the repo (bug reports,
    # gists, forums, etc.). ClassVar prevents this constant from being
    # treated as a dataclass field by ``asdict()`` and ``cls(**data)``.
    _SENSITIVE_FIELDS: ClassVar[tuple[str, ...]] = (
        "universe_id",
        "place_id",
        "experience_name",
        "uploaded_assets",
        "mesh_native_sizes",
        "mesh_hierarchies",
    )

    def save_sanitized(self, path: Path) -> None:
        """Write a redacted copy of the context to ``path``.

        Strips Roblox IDs, uploaded asset URLs, and anything else that would
        tie the file to a specific creator/place/experience. Use this when
        uploading a conversion_context.json as a bug report attachment or
        sharing one outside the repo — ``save()`` preserves everything for
        pause/resume but is unsafe to expose.

        Preserves: scene stats, phase completion, errors/warnings, Unity
        project path. Removes: universe/place IDs, experience name,
        uploaded_assets mapping, mesh resolution tables.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        for k in self._SENSITIVE_FIELDS:
            if k in data:
                if isinstance(data[k], dict):
                    data[k] = {}
                elif isinstance(data[k], list):
                    data[k] = []
                else:
                    data[k] = None
        data["_sanitized"] = True
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> ConversionContext:
        """Reconstruct a ConversionContext from a JSON file written by
        ``save()`` or ``save_sanitized()``.

        Drops keys that aren't dataclass fields (e.g. the ``_sanitized``
        marker added by ``save_sanitized``, or pre-ClassVar files where
        ``_SENSITIVE_FIELDS`` was serialized) so users can load redacted
        bug-report attachments without manual JSON surgery.
        """
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(cls)}
        cleaned = {k: v for k, v in data.items() if k in known}
        return cls(**cleaned)
