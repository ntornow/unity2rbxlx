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
