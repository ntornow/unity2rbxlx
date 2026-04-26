"""
conversion_context.py -- Mutable state across pipeline phases.

Serializable to JSON for pause/resume support.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, TYPE_CHECKING, TypedDict

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

    # Phase 4.9: per-script serialized field references.  Shape:
    #   { relative_cs_path: { field_name: prefab_name_or_audio_ref } }
    # Populated after parse, before transpile_scripts. Consumed by the
    # transpiler (so the AI prompt knows which fields point at prefabs)
    # and by 4.10 prefab packages (to know which prefabs to emit).
    serialized_field_refs: dict[str, dict[str, str]] = field(default_factory=dict)

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

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # Fields that reveal Roblox creator identity, universe/place ownership,
    # or uploaded asset URLs — anything that should be stripped before
    # sharing a conversion_context.json outside the repo (bug reports,
    # gists, forums, etc.).
    _SENSITIVE_FIELDS: "tuple[str, ...]" = (
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
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)
