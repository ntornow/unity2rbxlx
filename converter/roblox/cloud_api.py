"""
Roblox Open Cloud API integration.

Provides helpers for uploading assets (images, meshes, audio) and publishing
place files to Roblox via the Open Cloud REST API.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import requests

from utils.retry import exponential_backoff_retry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ASSETS_URL = "https://apis.roblox.com/assets/v1/assets"
_PLACE_VERSION_URL = (
    "https://apis.roblox.com/universes/v1/{universe_id}"
    "/places/{place_id}/versions"
)
_LUAU_EXECUTION_URL = (
    "https://apis.roblox.com/cloud/v2/universes/{universe_id}"
    "/places/{place_id}/luau-execution-session-tasks"
)
_LUAU_TASK_URL = (
    "https://apis.roblox.com/cloud/v2/universes/{universe_id}"
    "/places/{place_id}/versions/{version_id}"
    "/luau-execution-sessions/{session_id}/tasks/{task_id}"
)

_DEFAULT_TIMEOUT = 60  # seconds


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _auth_headers(api_key: str) -> dict[str, str]:
    return {"x-api-key": api_key}


def _handle_rate_limit(response: requests.Response) -> None:
    """If the response indicates rate-limiting, sleep until the limit resets."""
    remaining = response.headers.get("x-ratelimit-remaining")
    if remaining is not None and int(remaining) <= 0:
        reset_after = response.headers.get("x-ratelimit-reset")
        wait = float(reset_after) if reset_after else 5.0
        logger.warning("Rate-limited by Roblox API; sleeping %.1fs", wait)
        time.sleep(wait)


def _poll_operation(
    operation_id: str,
    api_key: str,
    max_polls: int = 60,
    poll_interval: float = 2.0,
) -> str | None:
    """Poll an async operation until done, return the asset ID.

    Only returns numeric asset IDs (not UUIDs or operation paths). On a
    Roblox-side processing failure, surfaces ``error.code`` and
    ``error.message`` so callers know whether the upload was rejected by
    moderation, malformed, or hit a transient server bug — the previous
    "no numeric asset ID" message dropped the only diagnostic Roblox returned.
    """
    url = f"https://apis.roblox.com/assets/v1/operations/{operation_id}"
    for i in range(max_polls):
        time.sleep(poll_interval)
        try:
            resp = requests.get(url, headers=_auth_headers(api_key), timeout=30)
            if resp.status_code != 200:
                continue
            data = resp.json()
            if data.get("done"):
                # Asset ID is in response.assetId or response path
                response_data = data.get("response", {})
                asset_id = response_data.get("assetId")
                if asset_id and str(asset_id).isdigit():
                    return str(asset_id)
                # Try extracting from path like "assets/123456"
                path = data.get("path", "") or response_data.get("path", "")
                if "/" in path:
                    candidate = path.split("/")[-1]
                    if candidate.isdigit():
                        return candidate
                # Operation completed without an asset ID. The real cause
                # is on ``data.error`` — surface it so users know whether
                # to retry, fix the source asset, or skip it permanently.
                error = data.get("error") or {}
                err_code = error.get("code", "MissingAssetId")
                err_msg = error.get("message", "")
                logger.warning(
                    "Upload op %s failed: code=%s message=%r response=%s",
                    operation_id, err_code, err_msg, response_data,
                )
                return None
        except Exception as exc:
            logger.warning("Poll attempt %d failed: %s", i + 1, exc)
    logger.warning("Operation %s did not complete after %d polls", operation_id, max_polls)
    return None


def _upload_asset(
    file_path: str | Path,
    api_key: str,
    creator_id: str,
    creator_type: str,
    asset_type: str,
    name: str,
    description: str = "",
) -> str | None:
    """Upload a generic asset via the Open Cloud assets endpoint.

    Returns the asset ID string on success, or ``None`` on failure.
    """
    file_path = Path(file_path)
    if not api_key:
        logger.warning("No API key provided; skipping upload of %s", file_path.name)
        return None
    if not file_path.exists():
        logger.error("Asset file not found: %s", file_path)
        return None

    metadata = {
        "assetType": asset_type,
        "displayName": name,
        "description": description,
        "creationContext": {
            "creator": {
                "userId" if creator_type.lower() == "user" else "groupId": creator_id,
            },
        },
    }

    # Determine MIME type from extension
    _MIME_MAP = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".bmp": "image/bmp", ".tga": "image/x-tga", ".gif": "image/gif",
        ".psd": "image/vnd.adobe.photoshop", ".tif": "image/tiff", ".tiff": "image/tiff",
        ".fbx": "model/fbx", ".obj": "model/obj",
        ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".wav": "audio/wav",
        ".flac": "audio/flac",
    }
    mime_type = _MIME_MAP.get(file_path.suffix.lower(), "application/octet-stream")

    def _do_upload() -> requests.Response:
        with open(file_path, "rb") as f:
            files = {
                "request": (None, json.dumps(metadata), "application/json"),
                "fileContent": (file_path.name, f, mime_type),
            }
            resp = requests.post(
                _ASSETS_URL,
                headers=_auth_headers(api_key),
                files=files,
                timeout=_DEFAULT_TIMEOUT,
            )
        return resp

    try:
        response = _do_upload()
        # Only retry on server errors, NOT on 429 (rate limit) - retrying makes it worse
        if response.status_code in (500, 502, 503):
            logger.warning("Server error %d, retrying once...", response.status_code)
            time.sleep(5)
            response = _do_upload()
    except Exception:
        logger.exception("Failed to upload asset %s", file_path.name)
        return None

    _handle_rate_limit(response)

    if response.status_code in (200, 201):
        data = response.json()
        asset_id = data.get("assetId") or data.get("id")
        if asset_id and str(asset_id).isdigit():
            logger.info("Uploaded %s -> asset %s", file_path.name, asset_id)
            return str(asset_id)

        # Async operation - poll for completion
        op_id = data.get("operationId") or data.get("path", "").split("/")[-1]
        if op_id and not data.get("done", True):
            asset_id = _poll_operation(op_id, api_key)
            if asset_id:
                logger.info("Uploaded %s -> asset %s (async)", file_path.name, asset_id)
                return asset_id

        logger.warning("Upload succeeded but no asset ID in response: %s", data)
        return None

    logger.error(
        "Asset upload failed (%d): %s", response.status_code, response.text[:500]
    )
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def upload_image(
    file_path: str | Path,
    api_key: str,
    creator_id: str,
    creator_type: str = "User",
    name: str = "Image",
    description: str = "",
) -> str | None:
    """Upload an image to Roblox using the 'Image' asset type.

    Returns the Image asset ID string on success, or ``None`` on failure.
    The returned ID is directly usable in SurfaceAppearance (ColorMap, NormalMap,
    etc.) without needing InsertService resolution — unlike 'Decal' uploads which
    return Decal IDs that must be resolved to Image IDs.
    """
    return _upload_asset(
        file_path, api_key, creator_id, creator_type,
        asset_type="Image",
        name=name,
        description=description,
    )


def upload_mesh(
    file_path: str | Path,
    api_key: str,
    creator_id: str,
    creator_type: str = "User",
    name: str = "Mesh",
) -> str | None:
    """Upload a mesh (Model) asset to Roblox.

    Returns the asset ID string on success, or ``None`` on failure.
    """
    return _upload_asset(
        file_path, api_key, creator_id, creator_type,
        asset_type="Model",
        name=name,
    )


def upload_audio(
    file_path: str | Path,
    api_key: str,
    creator_id: str,
    creator_type: str = "User",
    name: str = "Audio",
) -> str | None:
    """Upload an audio asset to Roblox.

    Returns the asset ID string on success, or ``None`` on failure.
    """
    return _upload_asset(
        file_path, api_key, creator_id, creator_type,
        asset_type="Audio",
        name=name,
    )


def probe_asset_availability(
    asset_id: str,
    api_key: str,
) -> str:
    """Check whether an uploaded asset is reachable/approved.

    Returns "approved", "rejected", or "unknown" (inconclusive).
    Defaults to "unknown" on errors to avoid false negatives.
    """
    clean = str(asset_id).strip()
    if clean.startswith("rbxassetid://"):
        clean = clean[len("rbxassetid://"):]
    if not clean.isdigit():
        return "unknown"
    url = f"{_ASSETS_URL}/{clean}"

    # Retry up to 3 times on 429 so a transient rate-limit doesn't cause a
    # false "unknown" — the audit sweep calls this in a tight loop.
    resp = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=_auth_headers(api_key), timeout=30)
        except requests.RequestException as exc:
            logger.debug("probe_asset_availability: request failed for %s: %s", clean, exc)
            return "unknown"
        if resp.status_code != 429:
            break
        wait = 2.0 * (attempt + 1)
        logger.debug("probe_asset_availability: 429 on %s, waiting %.1fs", clean, wait)
        time.sleep(wait)

    if resp is None:
        return "unknown"
    if resp.status_code == 403:
        # Distinguish asset-level rejection from account-level moderation:
        # when the uploader's account is moderated, every probe returns
        # 403 with "User is moderated" — those assets aren't actually
        # rejected, they're just inaccessible to this API key. Treating
        # those as rejected would strip otherwise-valid uploads from
        # the rbxlx.
        body = resp.text or ""
        if "user is moderated" in body.lower() or "user moderated" in body.lower():
            return "unknown"
        return "rejected"
    if resp.status_code == 404:
        return "rejected"
    if resp.status_code != 200:
        return "unknown"

    try:
        data = resp.json()
    except ValueError:
        return "unknown"

    # The assets/v1/assets/{id} endpoint includes a `moderationResult` with a
    # `moderationState` enum (Approved / Pending / Rejected). Some responses
    # wrap it under `response`. Be defensive about both shapes.
    moderation = (
        data.get("moderationResult")
        or (data.get("response") or {}).get("moderationResult")
        or {}
    )
    state = (
        moderation.get("moderationState")
        if isinstance(moderation, dict)
        else None
    )
    if state == "Rejected":
        return "rejected"
    if state in ("Approved", None):
        # Approved, or no moderation block reported — trust it.
        return "approved"
    # Pending / any other explicit state — treat as unknown, caller decides.
    return "unknown"


def upload_place(
    rbxlx_path: str | Path,
    api_key: str,
    universe_id: int | str,
    place_id: int | str,
) -> bool:
    """Upload (publish) a ``.rbxlx`` file to an existing Roblox place.

    Returns ``True`` on success, ``False`` on failure.
    """
    rbxlx_path = Path(rbxlx_path)
    if not rbxlx_path.exists():
        logger.error("Place file not found: %s", rbxlx_path)
        return False

    # .rbxl is binary, .rbxlx is XML. suffix.lower() to handle .RBXL too.
    content_type = (
        "application/octet-stream"
        if rbxlx_path.suffix.lower() == ".rbxl"
        else "application/xml"
    )

    url = _PLACE_VERSION_URL.format(universe_id=universe_id, place_id=place_id)
    params = {"versionType": "Published"}

    def _do_publish() -> requests.Response:
        with open(rbxlx_path, "rb") as f:
            body = f.read()
        resp = requests.post(
            url,
            params=params,
            headers={
                **_auth_headers(api_key),
                "Content-Type": content_type,
            },
            data=body,
            timeout=_DEFAULT_TIMEOUT,
        )
        return resp

    try:
        response = exponential_backoff_retry(
            _do_publish,
            max_retries=5,
            retry_on=lambda r: r.status_code in (429, 500, 502, 503),
        )
    except Exception:
        logger.exception("Failed to publish place after retries")
        return False

    _handle_rate_limit(response)

    if response.status_code in (200, 201):
        logger.info(
            "Published place %s (universe %s), version: %s",
            place_id,
            universe_id,
            response.json().get("versionNumber", "?"),
        )
        return True

    logger.error(
        "Place publish failed (%d): %s", response.status_code, response.text[:500]
    )
    return False


# ---------------------------------------------------------------------------
# Luau Execution API (headless script execution on published places)
# ---------------------------------------------------------------------------

def execute_luau(
    api_key: str,
    universe_id: int | str,
    place_id: int | str,
    script: str,
    timeout: str = "300s",
) -> dict | None:
    """Execute a Luau script on a published Roblox place (headless).

    Uses the Open Cloud Luau Execution API to run code server-side
    without Studio. The script has access to the full DataModel including
    AssetService, InsertService, etc.

    Returns the task result dict on success, or None on failure.
    """
    url = _LUAU_EXECUTION_URL.format(
        universe_id=universe_id,
        place_id=place_id,
    )

    payload = {
        "script": script,
        "timeout": timeout,
    }

    try:
        resp = requests.post(
            url,
            headers={
                **_auth_headers(api_key),
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=_DEFAULT_TIMEOUT,
        )
    except Exception:
        logger.exception("Failed to submit Luau execution task")
        return None

    if resp.status_code not in (200, 201):
        logger.error(
            "Luau execution submit failed (%d): %s",
            resp.status_code, resp.text[:500],
        )
        return None

    task_data = resp.json()
    task_path = task_data.get("path", "")
    state = task_data.get("state", "")
    logger.info("Luau task submitted: %s (state=%s)", task_path, state)

    # Poll for completion
    if state == "COMPLETE":
        return task_data

    # Extract IDs from path for polling
    # Path format: universes/{uid}/places/{pid}/versions/{vid}/luau-execution-sessions/{sid}/tasks/{tid}
    parts = task_path.split("/")
    if len(parts) < 10:
        logger.error("Unexpected task path format: %s", task_path)
        return None

    poll_url = f"https://apis.roblox.com/cloud/v2/{task_path}"

    start_time = time.time()
    for attempt in range(120):  # 5 minutes max (2.5s intervals)
        time.sleep(2.5)
        try:
            poll_resp = requests.get(
                poll_url,
                headers=_auth_headers(api_key),
                timeout=30,
            )
            if poll_resp.status_code != 200:
                logger.debug("Poll attempt %d: HTTP %d", attempt, poll_resp.status_code)
                continue

            result = poll_resp.json()
            state = result.get("state", "")
            if state == "COMPLETE":
                elapsed = time.time() - start_time
                logger.info("Luau task completed in %.1fs", elapsed)
                return result
            if state == "FAILED":
                error = result.get("error", {})
                logger.error("Luau task failed: %s", error.get("message", str(error)))
                # Try to fetch execution logs for debugging
                try:
                    log_url = f"{poll_url}/logs"
                    log_resp = requests.get(log_url, headers=_auth_headers(api_key), timeout=15)
                    if log_resp.status_code == 200:
                        log_data = log_resp.json()
                        entries = log_data.get("luauExecutionSessionTaskLogs", [])
                        for entry in entries[:5]:
                            for msg in entry.get("messages", []):
                                logger.error("  Luau log: %s", msg[:200])
                except Exception:
                    pass
                return None
            # PROCESSING — keep polling
        except Exception as exc:
            logger.debug("Poll error: %s", exc)

    logger.error("Luau task timed out after polling")
    return None


def execute_luau_with_binary(
    api_key: str,
    universe_id: int | str,
    place_id: int | str,
    script: str,
    timeout: str = "300s",
) -> bytes | None:
    """Execute Luau with binary output enabled, return the binary data.

    The script must return a LuauExecutionTaskOutput table:
        return { BinaryOutput = buffer, ReturnValues = {...} }

    Returns the binary data on success, or None on failure.
    """
    url = _LUAU_EXECUTION_URL.format(
        universe_id=universe_id,
        place_id=place_id,
    )

    payload = {
        "script": script,
        "timeout": timeout,
        "enableBinaryOutput": True,
    }

    try:
        resp = requests.post(
            url,
            headers={
                **_auth_headers(api_key),
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=_DEFAULT_TIMEOUT,
        )
    except Exception:
        logger.exception("Failed to submit Luau binary task")
        return None

    if resp.status_code not in (200, 201):
        logger.error("Luau binary task submit failed (%d): %s",
                     resp.status_code, resp.text[:500])
        return None

    task_data = resp.json()
    task_path = task_data.get("path", "")
    poll_url = f"https://apis.roblox.com/cloud/v2/{task_path}"

    # Poll for completion
    for attempt in range(120):
        time.sleep(2.5)
        try:
            poll_resp = requests.get(poll_url, headers=_auth_headers(api_key), timeout=30)
            if poll_resp.status_code != 200:
                continue
            result = poll_resp.json()
            state = result.get("state", "")
            if state == "COMPLETE":
                binary_uri = result.get("output", {}).get("binaryOutputUri")
                if binary_uri:
                    logger.info("Downloading binary output...")
                    dl = requests.get(binary_uri, timeout=120)
                    if dl.status_code == 200:
                        return dl.content
                    logger.error("Binary download failed: %d", dl.status_code)
                return None
            if state == "FAILED":
                error = result.get("error", {})
                logger.error("Luau binary task failed: %s", error.get("message", str(error)))
                return None
        except Exception as exc:
            logger.debug("Poll error: %s", exc)

    logger.error("Luau binary task timed out")
    return None


def resolve_meshes_headless(
    api_key: str,
    universe_id: int | str,
    place_id: int | str,
    rbxlx_path: str | Path,
    output_path: str | Path,
) -> bool:
    """Resolve all MeshParts in a place headlessly via Luau Execution API.

    1. Uploads the rbxlx to the Roblox place
    2. Executes Luau to replace MeshParts via CreateMeshPartAsync
    3. Saves the place (persisting PhysicalConfigData)
    4. Downloads the result via SerializationService binary output

    The final file at output_path will have proper mesh geometry embedded.
    """
    rbxlx_path = Path(rbxlx_path)
    output_path = Path(output_path)

    # Step 1: Upload rbxlx
    logger.info("Step 1: Uploading rbxlx to place %s...", place_id)
    if not upload_place(rbxlx_path, api_key, universe_id, place_id):
        return False

    # Step 2: Execute mesh resolution + save
    logger.info("Step 2: Resolving meshes via CreateMeshPartAsync...")
    resolve_script = '''
local AssetService = game:GetService("AssetService")
local loaded = 0
local failed = 0
local meshCache = {}

local parts = {}
for _, d in game.Workspace:GetDescendants() do
    if d:IsA("MeshPart") and d:GetAttribute("_MeshId") then
        table.insert(parts, d)
    end
end

for _, part in ipairs(parts) do
    local meshUrl = part:GetAttribute("_MeshId")

    if not meshCache[meshUrl] then
        local ok, mp = pcall(function() return AssetService:CreateMeshPartAsync(meshUrl) end)
        if ok then
            meshCache[meshUrl] = { meshId = meshUrl, initialSize = mp.Size, template = mp }
        else
            local numId = tonumber(meshUrl:match("(%d+)"))
            if numId then
                local ok2, model = pcall(function()
                    return game:GetService("InsertService"):LoadAsset(numId)
                end)
                if ok2 and model then
                    for _, desc in model:GetDescendants() do
                        if desc:IsA("MeshPart") and desc.MeshId ~= "" then
                            local ok3, mp2 = pcall(function()
                                return AssetService:CreateMeshPartAsync(desc.MeshId)
                            end)
                            if ok3 then
                                meshCache[meshUrl] = {
                                    meshId = desc.MeshId,
                                    initialSize = mp2.Size,
                                    template = mp2,
                                }
                            end
                            break
                        end
                    end
                    model:Destroy()
                end
            end
        end
        if not meshCache[meshUrl] then
            meshCache[meshUrl] = false
        end
    end

    local cached = meshCache[meshUrl]
    if not cached then failed = failed + 1; continue end

    local newPart
    if cached.template then
        newPart = cached.template
        cached.template = nil
    else
        local ok, mp = pcall(function()
            return AssetService:CreateMeshPartAsync(cached.meshId)
        end)
        if not ok then failed = failed + 1; continue end
        newPart = mp
    end

    newPart.Name = part.Name
    newPart.CFrame = part.CFrame
    newPart.Anchored = part.Anchored
    newPart.CanCollide = part.CanCollide
    newPart.Color = part.Color
    newPart.Material = part.Material
    newPart.Transparency = part.Transparency
    newPart.CastShadow = part.CastShadow

    local sx = part:GetAttribute("_ScaleX")
    local sy = part:GetAttribute("_ScaleY")
    local sz = part:GetAttribute("_ScaleZ")
    if sx and sy and sz then
        newPart.Size = Vector3.new(
            cached.initialSize.X * sx,
            cached.initialSize.Y * sy,
            cached.initialSize.Z * sz
        )
    else
        newPart.Size = part.Size
    end

    for name, value in part:GetAttributes() do
        if string.sub(name, 1, 1) ~= "_" then
            newPart:SetAttribute(name, value)
        end
    end

    for _, child in part:GetChildren() do
        pcall(function() child.Parent = newPart end)
    end

    newPart.Parent = part.Parent
    part:Destroy()
    loaded = loaded + 1
end

-- Save the place with resolved meshes
game:GetService("AssetService"):SavePlaceAsync()

return string.format("Resolved %d meshes, %d failed", loaded, failed)
'''
    result = execute_luau(api_key, universe_id, place_id, resolve_script)
    if result is None:
        logger.error("Mesh resolution failed")
        return False

    output = result.get("output", {})
    results = output.get("results", [])
    logger.info("Mesh resolution result: %s", results)

    # Step 3: Download the saved place with embedded mesh data
    logger.info("Step 3: Downloading resolved place...")
    download_script = '''
local SerializationService = game:GetService("SerializationService")
local buf = SerializationService:SerializePlaceToRBXL()
return { BinaryOutput = buf, ReturnValues = { "ok" } }
'''
    place_data = execute_luau_with_binary(
        api_key, universe_id, place_id, download_script
    )
    if place_data is None:
        logger.error("Failed to download resolved place")
        return False

    output_path.write_bytes(place_data)
    logger.info("Saved resolved place to %s (%d bytes)", output_path, len(place_data))
    return True
