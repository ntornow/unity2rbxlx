"""
test_place_publisher.py -- chunk-cache + size-guard semantics for the
shared headless publish helper.

Regression coverage for two real bugs caught on previous review passes:

1. ``u2r publish`` used to read ``place_builder.luau`` (the human-readable
   join of all chunks) and feed it to ``execute_luau`` as one ~4 MB+ blob,
   silently failing for big places. The fix is the chunk-cache JSON used
   here so re-publish runs through the size-guarded chunked path.

2. The first refactor regressed the ability to republish from an output
   directory whose Unity project is gone. ``publish_cached_chunks`` is the
   fast path that restores that workflow.
"""

from __future__ import annotations

import json
from pathlib import Path

from roblox.id_cache import read_ids, write_ids, CANONICAL, LEGACY
from roblox.place_publisher import (
    CHUNKS_FILENAME,
    MAX_EXECUTE_LUAU_BYTES,
    PublishResult,
    publish_cached_chunks,
    publish_place,
)


class _StubExecuteLuau:
    """Records each (uid, pid, script_len) call; returns a non-None token
    so the publish loop sees success."""

    def __init__(self):
        self.calls: list[tuple[int, int, int]] = []

    def __call__(self, api_key, uid, pid, script, *, timeout="300s"):
        self.calls.append((uid, pid, len(script)))
        return {"ok": True}


class TestPublishCachedChunks:
    def test_returns_none_when_cache_absent(self, tmp_path):
        result = publish_cached_chunks("k", 1, 2, tmp_path)
        assert result is None

    def test_replays_cached_chunks(self, tmp_path, monkeypatch):
        from roblox import place_publisher

        chunks = ["chunk-one", "chunk-two", "chunk-three"]
        (tmp_path / CHUNKS_FILENAME).write_text(json.dumps(chunks))

        stub = _StubExecuteLuau()
        monkeypatch.setattr(
            place_publisher, "_publish_chunks",
            lambda api_key, uid, pid, chs, script_path, *, timeout: PublishResult(
                success=True,
                chunks=len(chs),
                total_bytes=sum(len(c) for c in chs),
                script_path=script_path,
                chunk_results=[{"chunk": i + 1, "ok": True} for i in range(len(chs))],
            ),
        )

        result = publish_cached_chunks("k", 1, 2, tmp_path)
        assert result is not None
        assert result.success
        assert result.chunks == 3
        assert result.total_bytes == sum(len(c) for c in chunks)

    def test_corrupt_cache_returns_none(self, tmp_path):
        (tmp_path / CHUNKS_FILENAME).write_text("not json {{")
        assert publish_cached_chunks("k", 1, 2, tmp_path) is None

    def test_wrong_shape_cache_returns_none(self, tmp_path):
        (tmp_path / CHUNKS_FILENAME).write_text(json.dumps({"not": "a list"}))
        # No legacy script either, so this returns None (fall through to rebuild).
        assert publish_cached_chunks("k", 1, 2, tmp_path) is None

    def test_empty_cache_returns_none(self, tmp_path):
        """An empty chunk list would let ``_publish_chunks`` no-op and
        falsely report success=True. Treat it as missing so callers can
        fall back to a legacy script or rebuild path.
        """
        (tmp_path / CHUNKS_FILENAME).write_text(json.dumps([]))
        assert publish_cached_chunks("k", 1, 2, tmp_path) is None

    def test_empty_string_chunks_returns_none(self, tmp_path):
        """Same logic — a list of empty strings is still nothing to send."""
        (tmp_path / CHUNKS_FILENAME).write_text(json.dumps(["", ""]))
        assert publish_cached_chunks("k", 1, 2, tmp_path) is None

    def test_empty_legacy_script_returns_none(self, tmp_path):
        """An empty place_builder.luau (e.g. zero-byte file) likewise
        shouldn't false-positive a successful publish.
        """
        (tmp_path / "place_builder.luau").write_text("")
        assert publish_cached_chunks("k", 1, 2, tmp_path) is None

    def test_legacy_script_fallback_when_chunks_json_missing(self, tmp_path, monkeypatch):
        """Output dirs from before the chunk cache existed still have
        ``place_builder.luau``. Republishing those must work without a
        Pipeline rebuild — the regression flagged by Codex round 2.
        """
        (tmp_path / "place_builder.luau").write_text("legacy single-chunk script")

        called: list[tuple] = []
        monkeypatch.setattr(
            "roblox.cloud_api.execute_luau",
            lambda *a, **kw: called.append(a) or {"ok": True},
        )

        result = publish_cached_chunks("k", 1, 2, tmp_path)
        assert result is not None
        assert result.success
        assert result.chunks == 1
        assert len(called) == 1, "must execute legacy script through the chunked path"

    def test_legacy_oversized_script_fails_loudly(self, tmp_path, monkeypatch):
        """A legacy place_builder.luau >4MB used to silently fail at runtime
        because the old ``publish`` ran one ``execute_luau`` with no size
        guard. Now the size guard catches it and the user gets a clear
        error directing them to re-convert.
        """
        big = "x" * (MAX_EXECUTE_LUAU_BYTES + 1)
        (tmp_path / "place_builder.luau").write_text(big)

        called: list[tuple] = []
        monkeypatch.setattr(
            "roblox.cloud_api.execute_luau",
            lambda *a, **kw: called.append(a) or {"ok": True},
        )

        result = publish_cached_chunks("k", 1, 2, tmp_path)
        assert result is not None
        assert result.success is False
        assert result.exceeded_limit is True
        assert called == [], "must not call execute_luau on oversized legacy script"


class TestPublishPlaceWritesCache:
    """``publish_place`` MUST write ``place_builder_chunks.json`` so a later
    ``u2r publish`` can replay without rebuilding from the Unity project.
    """

    def test_chunks_cache_written_on_publish(self, tmp_path, monkeypatch):
        from roblox import place_publisher

        fake_chunks = ["a" * 100, "b" * 200]
        monkeypatch.setattr(
            "roblox.luau_place_builder.generate_place_luau_chunked",
            lambda rbx_place: fake_chunks,
        )
        monkeypatch.setattr(
            "roblox.cloud_api.execute_luau",
            lambda *a, **kw: {"ok": True},
        )

        result = publish_place("k", 1, 2, object(), tmp_path)

        assert result.success
        chunks_path = tmp_path / CHUNKS_FILENAME
        assert chunks_path.exists()
        assert json.loads(chunks_path.read_text()) == fake_chunks


class TestSizeGuard:
    """The 4MB execute_luau cap must be enforced. The unguarded
    single-script publish path used to ship oversized places that silently
    failed at runtime.
    """

    def test_oversized_chunks_short_circuit(self, tmp_path, monkeypatch):
        # One huge chunk that exceeds the cap. Stub generation so we don't
        # actually allocate 4MB+ of place data.
        big_chunk = "x" * (MAX_EXECUTE_LUAU_BYTES + 1)
        monkeypatch.setattr(
            "roblox.luau_place_builder.generate_place_luau_chunked",
            lambda rbx_place: [big_chunk],
        )

        called = []
        monkeypatch.setattr(
            "roblox.cloud_api.execute_luau",
            lambda *a, **kw: called.append(a) or {"ok": True},
        )

        result = publish_place("k", 1, 2, object(), tmp_path)

        assert result.success is False
        assert result.exceeded_limit is True
        assert called == [], "execute_luau must not be called when oversized"


class TestIdCacheMtimePriority:
    """When both .roblox_ids.json and resolve_ids.json exist (migration
    state), pick the most recently modified file. Returning the canonical
    file unconditionally would silently route publishes to the previous
    experience whenever the legacy file holds the freshest retarget.
    Regression flagged by Codex review on the v2 branch.
    """

    def test_legacy_newer_than_canonical_wins(self, tmp_path):
        import os
        # Older canonical
        (tmp_path / CANONICAL).write_text(
            json.dumps({"universe_id": 100, "place_id": 200})
        )
        os.utime(tmp_path / CANONICAL, (1000, 1000))
        # Newer legacy (later mtime → freshest retarget)
        (tmp_path / LEGACY).write_text(
            json.dumps({"universe_id": 999, "place_id": 888})
        )
        os.utime(tmp_path / LEGACY, (5000, 5000))

        uid, pid = read_ids(tmp_path)
        assert (uid, pid) == (999, 888), "must prefer freshest mtime"

    def test_non_dict_cache_returns_none_not_crash(self, tmp_path):
        """JSON list / scalar in the cache file used to crash callers via
        AttributeError on .get(). Codex round 16 caught this. The helper
        must treat any non-dict shape as missing.
        """
        (tmp_path / CANONICAL).write_text(json.dumps([1, 2, 3]))
        uid, pid = read_ids(tmp_path)
        assert (uid, pid) == (None, None)

    def test_non_numeric_cache_returns_none_not_crash(self, tmp_path):
        """Non-numeric uid/pid (e.g. {"universe_id": "abc"}) used to
        crash int() conversion. Treat as malformed → missing.
        """
        (tmp_path / CANONICAL).write_text(
            json.dumps({"universe_id": "abc", "place_id": "xyz"})
        )
        uid, pid = read_ids(tmp_path)
        assert (uid, pid) == (None, None)

    def test_canonical_newer_than_legacy_wins(self, tmp_path):
        import os
        (tmp_path / LEGACY).write_text(
            json.dumps({"universe_id": 100, "place_id": 200})
        )
        os.utime(tmp_path / LEGACY, (1000, 1000))
        (tmp_path / CANONICAL).write_text(
            json.dumps({"universe_id": 999, "place_id": 888})
        )
        os.utime(tmp_path / CANONICAL, (5000, 5000))

        uid, pid = read_ids(tmp_path)
        assert (uid, pid) == (999, 888)


class TestPublishCachedChunksRespectsSizeGuard:
    def test_oversized_cache_short_circuits(self, tmp_path):
        big = "x" * (MAX_EXECUTE_LUAU_BYTES + 1)
        (tmp_path / CHUNKS_FILENAME).write_text(json.dumps([big]))

        result = publish_cached_chunks("k", 1, 2, tmp_path)
        assert result is not None
        assert result.success is False
        assert result.exceeded_limit is True
