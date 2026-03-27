"""
unity_types.py -- Data models for parsed Unity projects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


AssetKind = Literal[
    "texture", "mesh", "audio", "video", "material", "animation",
    "shader", "font", "prefab", "scene", "script", "assembly_definition",
    "data", "preset", "lighting", "terrain", "input", "timeline",
    "directory", "unknown",
]


# ---------------------------------------------------------------------------
# Component / Scene types
# ---------------------------------------------------------------------------

@dataclass
class ComponentData:
    """Raw key/value data for a Unity component attached to a GameObject."""
    component_type: str
    file_id: str
    properties: dict[str, Any]


@dataclass
class PrefabInstanceData:
    """A PrefabInstance document found in the scene."""
    file_id: str
    source_prefab_guid: str
    source_prefab_file_id: str
    transform_parent_file_id: str
    modifications: list[dict[str, Any]]
    removed_components: list[Any] = field(default_factory=list)


@dataclass
class SceneNode:
    """A single GameObject in the Unity scene hierarchy."""
    name: str
    file_id: str
    active: bool
    layer: int
    tag: str
    components: list[ComponentData] = field(default_factory=list)
    children: list[SceneNode] = field(default_factory=list)
    parent_file_id: str | None = None

    # Transform shorthand
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)

    # Mesh reference
    mesh_guid: str | None = None
    mesh_file_id: str | None = None

    # Prefab tracking
    from_prefab_instance: bool = False
    source_prefab_name: str | None = None


@dataclass
class ParsedScene:
    """Top-level result of parsing a Unity scene file."""
    scene_path: Path
    roots: list[SceneNode] = field(default_factory=list)
    all_nodes: dict[str, SceneNode] = field(default_factory=dict)
    raw_documents: list[dict[str, Any]] = field(default_factory=list)
    referenced_material_guids: set[str] = field(default_factory=set)
    referenced_mesh_guids: set[str] = field(default_factory=set)
    prefab_instances: list[PrefabInstanceData] = field(default_factory=list)
    skybox_material_guid: str | None = None
    render_settings: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Prefab types
# ---------------------------------------------------------------------------

@dataclass
class PrefabComponent:
    """Component attached to a prefab node."""
    component_type: str
    file_id: str
    properties: dict[str, Any]


@dataclass
class PrefabNode:
    """A single GameObject within a prefab."""
    name: str
    file_id: str
    active: bool
    children: list[PrefabNode] = field(default_factory=list)
    parent_file_id: str | None = None
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    mesh_guid: str | None = None
    mesh_file_id: str | None = None
    components: list[PrefabComponent] = field(default_factory=list)


@dataclass
class PrefabTemplate:
    """Parsed representation of a .prefab file."""
    prefab_path: Path
    name: str
    root: PrefabNode | None = None
    all_nodes: dict[str, PrefabNode] = field(default_factory=dict)
    raw_documents: list[dict] = field(default_factory=list)
    referenced_material_guids: set[str] = field(default_factory=set)
    referenced_mesh_guids: set[str] = field(default_factory=set)
    is_multi_root: bool = False

    # Prefab variant fields
    source_prefab_guid: str | None = None
    variant_modifications: list[dict[str, Any]] = field(default_factory=list)
    variant_removed_components: list[Any] = field(default_factory=list)
    variant_added_objects: list[Any] = field(default_factory=list)
    is_variant: bool = False
    variant_resolved: bool = False


@dataclass
class PrefabLibrary:
    """Collection of all parsed prefabs."""
    prefabs: list[PrefabTemplate] = field(default_factory=list)
    by_name: dict[str, PrefabTemplate] = field(default_factory=dict)
    by_guid: dict[str, PrefabTemplate] = field(default_factory=dict)
    referenced_material_guids: set[str] = field(default_factory=set)
    referenced_mesh_guids: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# GUID index types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GuidEntry:
    """A single entry in the GUID index."""
    guid: str
    asset_path: Path
    relative_path: Path
    kind: AssetKind
    is_directory: bool = False


@dataclass
class GuidIndex:
    """Complete GUID <-> path bidirectional index for a Unity project."""
    project_root: Path
    guid_to_entry: dict[str, GuidEntry] = field(default_factory=dict)
    path_to_guid: dict[Path, str] = field(default_factory=dict)
    duplicate_guids: dict[str, list[Path]] = field(default_factory=dict)
    orphan_metas: list[Path] = field(default_factory=list)
    total_meta_files: int = 0
    parse_errors: list[str] = field(default_factory=list)

    def resolve(self, guid: str) -> Path | None:
        entry = self.guid_to_entry.get(guid)
        return entry.asset_path if entry else None

    def resolve_kind(self, guid: str) -> AssetKind | None:
        entry = self.guid_to_entry.get(guid)
        return entry.kind if entry else None

    def resolve_relative(self, guid: str) -> Path | None:
        entry = self.guid_to_entry.get(guid)
        return entry.relative_path if entry else None

    def guid_for_path(self, asset_path: Path) -> str | None:
        return self.path_to_guid.get(asset_path.resolve())

    def filter_by_kind(self, kind: AssetKind) -> dict[str, GuidEntry]:
        return {g: e for g, e in self.guid_to_entry.items() if e.kind == kind}

    @property
    def total_resolved(self) -> int:
        return len(self.guid_to_entry)


# ---------------------------------------------------------------------------
# Asset manifest
# ---------------------------------------------------------------------------

@dataclass
class AssetEntry:
    """A discovered asset in the Unity project."""
    path: Path
    relative_path: Path
    kind: AssetKind
    guid: str | None = None
    size_bytes: int = 0
    hash: str | None = None


@dataclass
class AssetManifest:
    """Complete inventory of assets in a Unity project."""
    project_root: Path
    assets: list[AssetEntry] = field(default_factory=list)
    by_kind: dict[str, list[AssetEntry]] = field(default_factory=dict)
    by_guid: dict[str, AssetEntry] = field(default_factory=dict)
    total_size_bytes: int = 0
