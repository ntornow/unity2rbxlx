"""
place_publisher.py -- Headless place publish via Roblox Open Cloud execute_luau.

Single source of truth for the chunk + size-guard + execute_luau loop. Three
callers (u2r convert, u2r publish, convert_interactive upload) used to
duplicate this; this module collapses them.

Two entry points:

* :func:`publish_place` — derive chunks from an in-memory ``rbx_place`` and
  publish. Used by ``u2r convert`` and ``convert_interactive upload`` after a
  fresh Pipeline rebuild. Caches chunks to disk so a later ``u2r publish``
  can re-emit them.
* :func:`publish_cached_chunks` — read previously cached chunks from disk and
  publish without rebuilding. Used by ``u2r publish`` when the user wants to
  republish from an output directory whose Unity project source is gone or
  has moved.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from core.roblox_types import RbxPlace

log = logging.getLogger(__name__)


class ChunkResult(TypedDict):
    """Per-chunk publish outcome. Stays a plain dict at runtime (TypedDict)
    so the existing JSON-emit path serializes it without conversion."""
    chunk: int
    ok: bool

# Roblox Open Cloud execute_luau accepts scripts up to ~4MB. Place-builder
# scripts larger than this must fall back to the runtime MeshLoader path.
MAX_EXECUTE_LUAU_BYTES = 4_000_000

# JSON file that stores the raw chunk list for cache-only republish. Separate
# from ``place_builder.luau`` (which is the human-readable join used for
# inspection and is NOT safe to re-execute as one big call when chunked).
CHUNKS_FILENAME = "place_builder_chunks.json"


@dataclass
class PublishResult:
    success: bool
    chunks: int = 0
    total_bytes: int = 0
    exceeded_limit: bool = False
    script_path: Path | None = None
    chunk_results: list[ChunkResult] = field(default_factory=list)
    error: str | None = None


def _save_artifacts(output_dir: Path, chunks: list[str]) -> Path:
    """Write the human-readable place_builder.luau and the chunk cache."""
    output_dir.mkdir(parents=True, exist_ok=True)
    script_path = output_dir / "place_builder.luau"
    script_path.write_text(
        chunks[0] if len(chunks) == 1 else "\n\n".join(chunks),
        encoding="utf-8",
    )
    chunks_path = output_dir / CHUNKS_FILENAME
    chunks_path.write_text(json.dumps(chunks), encoding="utf-8")
    return script_path


def _publish_chunks(
    api_key: str,
    universe_id: int,
    place_id: int,
    chunks: list[str],
    script_path: Path,
    *,
    timeout: str,
) -> PublishResult:
    """Execute already-generated chunks through ``execute_luau``."""
    from roblox.cloud_api import execute_luau

    total_bytes = sum(len(c) for c in chunks)

    if total_bytes > MAX_EXECUTE_LUAU_BYTES:
        return PublishResult(
            success=False,
            chunks=len(chunks),
            total_bytes=total_bytes,
            exceeded_limit=True,
            script_path=script_path,
            error=(
                f"Place builder script exceeds "
                f"{MAX_EXECUTE_LUAU_BYTES // 1_000_000}MB limit "
                f"({total_bytes / 1024 / 1024:.1f} MB). "
                "Use the local rbxlx with the runtime MeshLoader instead."
            ),
        )

    chunk_results: list[ChunkResult] = []
    for i, chunk in enumerate(chunks):
        log.info("place_publisher: executing chunk %d/%d", i + 1, len(chunks))
        result = execute_luau(api_key, universe_id, place_id, chunk, timeout=timeout)
        ok = result is not None
        chunk_results.append(ChunkResult(chunk=i + 1, ok=ok))
        if not ok:
            return PublishResult(
                success=False,
                chunks=len(chunks),
                total_bytes=total_bytes,
                script_path=script_path,
                chunk_results=chunk_results,
                error=f"Chunk {i + 1}/{len(chunks)} failed.",
            )

    return PublishResult(
        success=True,
        chunks=len(chunks),
        total_bytes=total_bytes,
        script_path=script_path,
        chunk_results=chunk_results,
    )


def _build_collision_fidelity_fixup_script(targets: list[dict]) -> str:
    """Build a Luau script that re-cooks specific MeshParts with the right
    CollisionFidelity by recreating them via ``CreateMeshPartAsync`` with
    the option dict (the only way Roblox actually decomposes concave
    geometry — property assignment from a serialized place file silently
    snaps back to Box).

    Targets are a list of ``{path, mesh_id, fidelity}`` dicts; the
    script resolves each by descending from ``workspace`` so it doesn't
    have to walk every part in the place (an unbounded scan times out
    server-side on places with thousands of descendants).
    """
    import json as _json
    payload = _json.dumps(targets)
    return f"""
local AssetService = game:GetService("AssetService")
local TARGETS = game:GetService("HttpService"):JSONDecode([==[{payload}]==])
local fixed, failed = 0, 0
local function resolve(path)
    local node = workspace
    for segment in string.gmatch(path, "[^/]+") do
        if not node then return nil end
        node = node:FindFirstChild(segment)
    end
    return node
end
for _, target in ipairs(TARGETS) do
    local old = resolve(target.path)
    if old and old:IsA("MeshPart") and old.MeshId == target.mesh_id then
        local ok, new = pcall(function()
            return AssetService:CreateMeshPartAsync(
                old.MeshId,
                {{ CollisionFidelity = Enum.CollisionFidelity[target.fidelity] }}
            )
        end)
        if ok and new then
            new.Name = old.Name
            new.CFrame = old.CFrame
            new.Anchored = old.Anchored
            new.Size = old.Size
            new.Color = old.Color
            new.Material = old.Material
            new.Transparency = old.Transparency
            new.CanCollide = old.CanCollide
            new.CanQuery = old.CanQuery
            new.CanTouch = old.CanTouch
            new.CastShadow = old.CastShadow
            new.TextureID = old.TextureID
            for k, v in pairs(old:GetAttributes()) do
                new:SetAttribute(k, v)
            end
            for _, c in ipairs(old:GetChildren()) do
                c.Parent = new
            end
            new.Parent = old.Parent
            old:Destroy()
            fixed = fixed + 1
        else
            failed = failed + 1
        end
    else
        failed = failed + 1
    end
end
AssetService:SavePlaceAsync()
return string.format("CollisionFidelity fixup: %d cooked, %d failed (of %d targets)", fixed, failed, #TARGETS)
"""


def _collect_collision_fidelity_targets(rbx_place) -> list[dict]:
    """Walk an RbxPlace and pick out every MeshPart whose
    ``collision_fidelity`` is non-Default and non-Box. Each target carries
    its workspace path (so the runtime fixup can resolve it without an
    O(N) scan), the mesh URL it should be cooked from, and the fidelity
    enum *name* (the runtime resolves via ``Enum.CollisionFidelity[name]``).
    """
    _names = {1: "Hull", 3: "PreciseConvexDecomposition"}
    targets: list[dict] = []

    def _walk(parts, prefix: str):
        for p in parts or []:
            cls = getattr(p, "class_name", None) or "Part"
            name = getattr(p, "name", None) or "Part"
            child_prefix = f"{prefix}/{name}" if prefix else name
            if cls == "MeshPart":
                fid = getattr(p, "collision_fidelity", None)
                mesh_id = getattr(p, "mesh_id", None) or ""
                if fid in _names and mesh_id:
                    targets.append({
                        "path": child_prefix,
                        "mesh_id": mesh_id,
                        "fidelity": _names[fid],
                    })
            _walk(getattr(p, "children", None) or [], child_prefix)

    _walk(getattr(rbx_place, "workspace_parts", None) or [], "")
    return targets


def publish_place_file(
    api_key: str,
    universe_id: int,
    place_id: int,
    rbxlx_path: Path,
    *,
    rbx_place=None,
    fixup_targets: list[dict] | None = None,
    fixup_collision_fidelity: bool = True,
) -> PublishResult:
    """Publish a place by uploading the generated ``.rbxlx`` file directly.

    The rbxlx carries the complete final state (resolved ``MeshId`` URLs,
    attributes, RemoteEvents, scripts, surface appearances). Open Cloud's
    place-version endpoint ingests every property byte-for-byte —
    including ``CollisionFidelity``.

    Caveat: ``CollisionFidelity`` is a property whose runtime effect
    depends on a *cooked* collision mesh, and Roblox builds that mesh at
    MeshPart creation time, not on property assignment from a serialized
    file. So a freshly-loaded place with ``<token name="CollisionFidelity">3</token>``
    in the file shows the property as Box (whatever the asset was ingested
    with) until something explicitly recreates the part via
    ``AssetService:CreateMeshPartAsync(id, {CollisionFidelity = ...})``.

    The fixup pass below does exactly that, scoped to MeshParts whose
    fidelity is non-Default and non-Box. Bounded payload (~few dozen
    parts in SimpleFPS-class projects), runs in seconds, and reuses the
    Open Cloud Luau Execution session that's already authorized against
    this universe.

    Set ``fixup_collision_fidelity=False`` to skip the fixup (e.g. when
    the place has no concave collision proxies that need preservation).
    """
    from roblox.cloud_api import upload_place, execute_luau

    rbxlx_path = Path(rbxlx_path)
    if not rbxlx_path.exists():
        return PublishResult(
            success=False,
            error=f"rbxlx not found at {rbxlx_path}",
        )

    total_bytes = rbxlx_path.stat().st_size
    log.info(
        "place_publisher: uploading %s (%d bytes) to universe=%s place=%s",
        rbxlx_path.name, total_bytes, universe_id, place_id,
    )
    ok = upload_place(rbxlx_path, api_key, universe_id, place_id)
    if not ok:
        return PublishResult(
            success=False,
            total_bytes=total_bytes,
            script_path=rbxlx_path,
            error="rbxlx upload failed (see cloud_api log).",
        )

    if fixup_collision_fidelity:
        if fixup_targets is not None:
            targets = fixup_targets
        elif rbx_place is not None:
            targets = _collect_collision_fidelity_targets(rbx_place)
        else:
            targets = []
        if targets:
            log.info(
                "place_publisher: cooking CollisionFidelity for %d MeshPart(s) ...",
                len(targets),
            )
            # Batch targets — each ``CreateMeshPartAsync`` round-trips an
            # asset fetch + decomposition (~30-60s on complex meshes),
            # plus a closing ``SavePlaceAsync``. The Roblox Open Cloud
            # Luau Execution API caps task timeout at 300s, so a single
            # 6-mesh call can blow the cap on large places. Splitting
            # into smaller chunks keeps each task well under budget,
            # at the cost of ``len(targets) / batch`` extra place saves.
            BATCH_SIZE = 2
            failures = 0
            for batch_idx in range(0, len(targets), BATCH_SIZE):
                batch = targets[batch_idx : batch_idx + BATCH_SIZE]
                script = _build_collision_fidelity_fixup_script(batch)
                log.info(
                    "place_publisher: fixup batch %d/%d (%d target(s))",
                    batch_idx // BATCH_SIZE + 1,
                    (len(targets) + BATCH_SIZE - 1) // BATCH_SIZE,
                    len(batch),
                )
                result = execute_luau(
                    api_key, universe_id, place_id, script, timeout="300s",
                )
                if result is None:
                    failures += len(batch)
                    log.warning(
                        "place_publisher: fixup batch %d timed out — %d target(s) "
                        "in this batch may stay at Box collision",
                        batch_idx // BATCH_SIZE + 1,
                        len(batch),
                    )
                    continue
                outputs = (result.get("output", {}) or {}).get("results") or []
                if outputs:
                    log.info("place_publisher: fixup result: %s", outputs[0])
            if failures:
                log.warning(
                    "place_publisher: CollisionFidelity fixup partial — "
                    "%d/%d target(s) failed. Re-run u2r.py publish to retry "
                    "the missed targets.",
                    failures, len(targets),
                )
        else:
            log.debug(
                "place_publisher: no MeshParts need CollisionFidelity fixup"
            )

    return PublishResult(
        success=True,
        chunks=1,
        total_bytes=total_bytes,
        script_path=rbxlx_path,
    )


def publish_place(
    api_key: str,
    universe_id: int,
    place_id: int,
    rbx_place: RbxPlace,
    output_dir: Path,
    *,
    timeout: str = "300s",
) -> PublishResult:
    """Publish a place to Roblox by executing chunked Luau via Open Cloud.

    Generates the place-builder Luau script(s) for ``rbx_place``, writes the
    full script to ``<output_dir>/place_builder.luau`` for human inspection
    AND a machine-readable chunk cache to
    ``<output_dir>/place_builder_chunks.json``, then publishes via
    :func:`_publish_chunks`.
    """
    from roblox.luau_place_builder import generate_place_luau_chunked

    chunks = generate_place_luau_chunked(rbx_place)
    script_path = _save_artifacts(output_dir, chunks)
    return _publish_chunks(
        api_key, universe_id, place_id, chunks, script_path, timeout=timeout,
    )


def publish_cached_chunks(
    api_key: str,
    universe_id: int,
    place_id: int,
    output_dir: Path,
    *,
    timeout: str = "300s",
) -> PublishResult | None:
    """Publish from cached chunks, skipping the Pipeline rebuild.

    Two cache shapes are supported, in priority order:

    1. ``<output_dir>/place_builder_chunks.json`` — JSON list written by
       :func:`publish_place`. Preserves the original chunking, so the size
       guard sees the same picture the convert run did.
    2. ``<output_dir>/place_builder.luau`` — single Luau file. Older
       conversions (and the previous ``u2r publish`` flow) wrote only this.
       It is treated as a single chunk so the size guard catches the >4MB
       case rather than silently truncating like the prior implementation.

    Returns ``None`` only when neither file is present — callers should
    fall back to a Pipeline rebuild in that case.
    """
    script_path = output_dir / "place_builder.luau"
    chunks_path = output_dir / CHUNKS_FILENAME

    chunks: list[str] | None = None
    if chunks_path.exists():
        try:
            data = json.loads(chunks_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("publish_cached_chunks: %s unreadable: %s", chunks_path, exc)
            data = None
        # Reject empty lists and non-empty entries-of-empty-strings: an
        # empty cache would make _publish_chunks no-op and falsely report
        # success=True. Treat it as missing so callers can fall back.
        if (
            isinstance(data, list)
            and data
            and all(isinstance(c, str) and c for c in data)
        ):
            chunks = data
        elif data is not None:
            log.warning("publish_cached_chunks: %s empty or unexpected shape", chunks_path)

    if chunks is None and script_path.exists():
        try:
            text = script_path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("publish_cached_chunks: %s unreadable: %s", script_path, exc)
            return None
        if text:
            chunks = [text]

    if chunks is None:
        return None

    return _publish_chunks(
        api_key, universe_id, place_id, chunks, script_path, timeout=timeout,
    )
