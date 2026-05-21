"""
asset_keys.py -- Shared helpers for asset-key string shapes.

The pipeline tracks uploaded assets in dicts keyed by string. Mesh
assets come in two shapes:

  * **External-file keys**: ``Assets/.../foo.fbx`` -- a path-like
    relative string ending in ``.fbx``/``.obj``. Produced by the
    standard upload loop.

  * **Synthetic embedded-mesh keys**: ``Assets/.../foo.prefab#<file_id>``
    -- a path-like prefix followed by ``#`` and a Unity file ID. The
    prefix ends in ``.prefab``/``.asset``; the suffix is the
    ``MeshFilter.m_Mesh.fileID`` value that disambiguates which
    ``!u!43 Mesh`` document inside the container we mean. Produced by
    ``unity.embedded_mesh_extractor`` + ``Pipeline._upload_embedded_meshes``.

The mesh-key predicate had been duplicated in three places (the
pipeline's resolve gate, the Studio resolver, the scene converter's
synthetic-key lookups). Hoisting it here gives one source of truth.
"""

from __future__ import annotations


_EXTERNAL_MESH_SUFFIXES = (".fbx", ".obj")
_EMBEDDED_MESH_PREFIX_SUFFIXES = (".prefab", ".asset")


def is_mesh_asset_key(key: str) -> bool:
    """True for any ``uploaded_assets`` key that points at a mesh upload.

    Accepts both external-file keys (``...fbx``/``...obj``) and synthetic
    embedded-mesh keys (``...prefab#<fileID>``/``...asset#<fileID>``).
    """
    if not key:
        return False
    kl = key.lower()
    if kl.endswith(_EXTERNAL_MESH_SUFFIXES):
        return True
    if "#" in key:
        prefix = kl.split("#", 1)[0]
        return prefix.endswith(_EMBEDDED_MESH_PREFIX_SUFFIXES)
    return False


def is_embedded_mesh_key(key: str) -> bool:
    """True for synthetic ``<path>#<file_id>`` keys only."""
    if "#" not in key:
        return False
    prefix = key.split("#", 1)[0].lower()
    return prefix.endswith(_EMBEDDED_MESH_PREFIX_SUFFIXES)


def make_embedded_mesh_key(relative_path: str, file_id: str) -> str:
    """Canonical formatter for the synthetic key shape."""
    return f"{relative_path}#{file_id}"
