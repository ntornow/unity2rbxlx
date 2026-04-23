"""
Roblox experience lifecycle manager.

Handles creation of experiences, publishing place files, and bulk uploading
of assets (textures, meshes, audio) to Roblox via the Open Cloud API.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from roblox.cloud_api import (
    create_experience,
    upload_image,
    upload_mesh,
    upload_audio,
    upload_place,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Asset type classification
# ---------------------------------------------------------------------------

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".webp"}
_MESH_EXTENSIONS = {".fbx", ".obj", ".gltf", ".glb"}
_AUDIO_EXTENSIONS = {".mp3", ".ogg", ".wav", ".flac"}


def _classify_asset(path: Path) -> str | None:
    """Return the asset category for a file, or ``None`` if unsupported."""
    ext = path.suffix.lower()
    if ext in _IMAGE_EXTENSIONS:
        return "image"
    if ext in _MESH_EXTENSIONS:
        return "mesh"
    if ext in _AUDIO_EXTENSIONS:
        return "audio"
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_or_create_experience(
    api_key: str,
    name: str,
    creator_id: str,  # noqa: ARG001 — kept for API compatibility
) -> tuple[int, int]:
    """Return ``(universe_id, place_id)``; universe creation is unsupported.

    Open Cloud does not expose universe creation via API-key auth, so this
    helper always raises ``RuntimeError`` with actionable guidance. Callers
    must obtain a ``(universe_id, place_id)`` pair externally (Creator Hub
    or ``convert --universe-id X --place-id Y``).

    Parameters
    ----------
    api_key:
        Roblox Open Cloud API key with appropriate scopes.
    name:
        Display name for the experience (unused; kept for API compatibility).
    creator_id:
        Roblox user or group ID that would own the experience (unused).
    """
    result = create_experience(api_key, name=name, description=f"Auto-created experience: {name}")
    if result is None:
        raise RuntimeError(
            f"Cannot auto-create Roblox experience '{name}': Open Cloud does "
            "not support universe creation with API-key auth. Create a place "
            "at https://create.roblox.com/dashboard/creations, then pass its "
            "universe_id and place_id explicitly."
        )
    universe_id, place_id = result
    logger.info(
        "Experience ready: universe_id=%d, place_id=%d", universe_id, place_id
    )
    return universe_id, place_id


def update_place_file(
    api_key: str,
    universe_id: int,
    place_id: int,
    rbxlx_path: str | Path,
) -> bool:
    """Publish an ``.rbxlx`` file to an existing Roblox place.

    Parameters
    ----------
    api_key:
        Roblox Open Cloud API key.
    universe_id:
        Target universe (experience) ID.
    place_id:
        Target place ID within the universe.
    rbxlx_path:
        Path to the ``.rbxlx`` file on disk.

    Returns
    -------
    bool
        ``True`` if the publish succeeded.
    """
    rbxlx_path = Path(rbxlx_path)
    if not rbxlx_path.exists():
        logger.error("Place file does not exist: %s", rbxlx_path)
        return False

    success = upload_place(rbxlx_path, api_key, universe_id, place_id)
    if success:
        logger.info("Place file published successfully.")
    else:
        logger.error("Place file publish failed.")
    return success


def upload_all_assets(
    api_key: str,
    creator_id: str,
    creator_type: str,
    asset_manifest: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, str]:
    """Upload all referenced assets and return a mapping of local paths to
    ``rbxassetid://`` URIs.

    Parameters
    ----------
    api_key:
        Roblox Open Cloud API key.
    creator_id:
        Roblox user or group ID.
    creator_type:
        ``"User"`` or ``"Group"``.
    asset_manifest:
        Dictionary mapping logical asset names/keys to their local file paths
        (relative to *output_dir* or absolute).
    output_dir:
        Base directory that relative paths in *asset_manifest* are resolved
        against.

    Returns
    -------
    dict[str, str]
        Mapping of local file path (as string) -> ``"rbxassetid://<id>"``
        for every successfully uploaded asset.
    """
    output_dir = Path(output_dir)
    mapping: dict[str, str] = {}
    failed: list[str] = []

    for asset_key, local_path_raw in asset_manifest.items():
        local_path = Path(local_path_raw)
        if not local_path.is_absolute():
            local_path = output_dir / local_path

        if not local_path.exists():
            logger.warning("Asset file missing, skipping: %s", local_path)
            failed.append(str(local_path))
            continue

        category = _classify_asset(local_path)
        if category is None:
            logger.warning("Unsupported asset type, skipping: %s", local_path)
            failed.append(str(local_path))
            continue

        asset_name = local_path.stem

        asset_id: str | None = None
        if category == "image":
            asset_id = upload_image(
                local_path, api_key, creator_id, creator_type, name=asset_name
            )
        elif category == "mesh":
            asset_id = upload_mesh(
                local_path, api_key, creator_id, creator_type, name=asset_name
            )
        elif category == "audio":
            asset_id = upload_audio(
                local_path, api_key, creator_id, creator_type, name=asset_name
            )

        if asset_id:
            rbx_uri = f"rbxassetid://{asset_id}"
            mapping[str(local_path)] = rbx_uri
            logger.info("Uploaded %s -> %s", local_path.name, rbx_uri)
        else:
            logger.error("Upload failed for %s", local_path)
            failed.append(str(local_path))

    if failed:
        logger.warning(
            "%d asset(s) failed to upload: %s",
            len(failed),
            ", ".join(failed[:10]),
        )

    logger.info(
        "Asset upload complete: %d succeeded, %d failed",
        len(mapping),
        len(failed),
    )
    return mapping
