"""Phase 4.9 — extract serialized-field references from MonoBehaviour components.

Walks every MonoBehaviour on every scene node + prefab template node, resolves
the ``m_Script`` GUID to its source ``.cs`` file, and collects per-field
references to prefabs or audio clips that the C# side will need at runtime.

The result feeds two downstream consumers:

* ``code_transpiler`` — can emit ``ReplicatedStorage.Templates:WaitForChild(...)``
  calls for serialized prefab fields instead of leaving them as ``nil``.
* ``generate_prefab_packages`` (PR 5 / 4.10) — uses the same dict to know which
  prefab packages actually need to be emitted for runtime cloning.

Persisted into ``conversion_context.json`` under ``serialized_field_refs`` so
rehydration + downstream phases see the same data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.unity_types import GuidIndex, ParsedScene, PrefabLibrary

# Unity MonoBehaviour fields that never hold author-visible serialized data —
# they're engine-internal. Skipping them avoids false positives.
_MONO_INTERNAL_PROPS: frozenset[str] = frozenset({
    "m_ObjectHideFlags",
    "m_CorrespondingSourceObject",
    "m_PrefabInstance",
    "m_PrefabAsset",
    "m_GameObject",
    "m_Enabled",
    "m_EditorHideFlags",
    "m_Script",
    "m_Name",
    "m_EditorClassIdentifier",
})

_AUDIO_EXTENSIONS: frozenset[str] = frozenset({".ogg", ".wav", ".mp3"})


def _is_object_ref(value: Any) -> bool:
    """Return True when a YAML value looks like a Unity object reference."""
    if not isinstance(value, dict):
        return False
    guid = value.get("guid", "")
    return bool(guid) and guid != "0" * 32


def _process_mono_properties(
    props: dict[str, Any],
    guid_index: Any,
    result: dict[Path, dict[str, str]],
) -> None:
    """Collect prefab and audio references off a single MonoBehaviour's
    ``properties`` dict. Mutates ``result`` in place keyed by the owning
    script's ``.cs`` path.
    """
    script_ref = props.get("m_Script", {})
    if not isinstance(script_ref, dict):
        return
    script_guid = script_ref.get("guid", "")
    if not script_guid:
        return
    script_path = guid_index.resolve(script_guid) if guid_index else None
    if not script_path or script_path.suffix != ".cs":
        return

    for key, value in props.items():
        # Skip only known engine-internal keys. Unity projects commonly
        # serialize ``[SerializeField] private T m_foo`` fields that
        # appear in YAML verbatim as ``m_foo``; a blanket m_-prefix
        # filter would miss the common case this extractor exists for.
        if key in _MONO_INTERNAL_PROPS:
            continue
        if not _is_object_ref(value):
            continue

        ref_path = guid_index.resolve(value["guid"])
        if not ref_path:
            continue

        refs = result.setdefault(script_path, {})
        if key in refs:
            continue  # first-seen binding wins; deterministic
        if ref_path.suffix == ".prefab":
            refs[key] = ref_path.stem
        elif ref_path.suffix in _AUDIO_EXTENSIONS:
            # Prefix distinguishes audio refs from prefab refs downstream.
            refs[key] = f"audio:{ref_path}"


def extract_serialized_field_refs(
    parsed_scenes: list[ParsedScene],
    prefab_library: PrefabLibrary | None,
    guid_index: GuidIndex | None,
) -> dict[Path, dict[str, str]]:
    """Return ``{script_cs_path: {field_name: prefab_or_audio_ref}}``.

    Walks every scene node and every prefab node. Safe to call with empty
    scenes or a ``None`` prefab_library — missing pieces just contribute
    nothing. Returns an empty dict when nothing is discoverable (still a
    valid state that downstream consumers handle).
    """
    result: dict[Path, dict[str, str]] = {}
    if guid_index is None:
        return result

    for scene in parsed_scenes or ():
        for node in getattr(scene, "all_nodes", {}).values():
            for comp in getattr(node, "components", []):
                if comp.component_type == "MonoBehaviour":
                    _process_mono_properties(comp.properties, guid_index, result)

    if prefab_library is not None:
        def _walk_prefab(pnode: Any) -> None:
            if pnode is None:
                return
            for comp in getattr(pnode, "components", []):
                if comp.component_type == "MonoBehaviour":
                    _process_mono_properties(comp.properties, guid_index, result)
            for child in getattr(pnode, "children", []) or ():
                _walk_prefab(child)

        for template in getattr(prefab_library, "prefabs", []):
            _walk_prefab(getattr(template, "root", None))

    return result


def serialize_for_context(
    refs: dict[Path, dict[str, str]],
    project_root: Path | None = None,
) -> dict[str, dict[str, str]]:
    """Render ``extract_serialized_field_refs`` output as
    ``{relative_cs_path: {field: target}}`` — JSON-safe for
    persistence in ``conversion_context.json``.

    When ``project_root`` is provided, paths are recorded relative to
    it so the context file round-trips across machines. Absolute paths
    are used when no root fits.
    """
    out: dict[str, dict[str, str]] = {}
    for script_path, fields in refs.items():
        try:
            if project_root is not None:
                key = str(script_path.resolve().relative_to(project_root.resolve()))
            else:
                key = str(script_path)
        except ValueError:
            key = str(script_path)
        out[key] = dict(fields)
    return out
