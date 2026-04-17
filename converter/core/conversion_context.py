"""
conversion_context.py -- Mutable state across pipeline phases.

Serializable to JSON for pause/resume support.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


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

    # Mesh resolution (Model ID -> real MeshId + native sizes)
    mesh_native_sizes: dict[str, Any] = field(default_factory=dict)

    # Mesh texture IDs (asset path -> rbxassetid:// URL for TextureID embedded in uploaded FBX models)
    mesh_texture_ids: dict[str, str] = field(default_factory=dict)

    # Full mesh hierarchy from Roblox LoadAsset:
    # fbx_path -> list of {name, meshId, size:[x,y,z], position:[x,y,z], textureId}
    mesh_hierarchies: dict[str, Any] = field(default_factory=dict)

    # FBX bounding boxes computed via trimesh (fallback for InitialSize when Studio
    # resolution is unavailable).  Maps relative asset path -> (w, h, d) in FBX units.
    fbx_bounding_boxes: dict[str, list[float]] = field(default_factory=dict)

    # Multi-scene metadata: scene_name -> {parts, scripts, ...}
    scenes_metadata: dict[str, Any] = field(default_factory=dict)

    # Comparison results
    comparison_scores: dict[str, Any] = field(default_factory=dict)

    # Storage plan (Phase 4a.5): explicit per-script container assignments.
    # Produced by converter.storage_classifier.classify_storage and written to
    # conversion_plan.json. rbxlx_writer reads each script's parent_path when
    # emitting; falls back to script_type heuristics when absent.
    storage_plan: dict[str, Any] = field(default_factory=dict)

    # Sprite extraction (Phase 3 item 3): GUID -> sliced sprite PNG path.
    # Produced by converter.sprite_extractor.extract_sprites in extract_assets.
    # SpriteRenderer/UI ImageLabel consumers can swap the GUID for the real
    # sliced PNG instead of a whole spritesheet.
    sprite_guid_to_file: dict[str, str] = field(default_factory=dict)

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
