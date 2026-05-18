"""
test_llm_cache.py -- Tests for the disk-based LLM response cache.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.llm_cache import LLMCache


class TestMakeKey:
    def test_deterministic(self):
        k1 = LLMCache.make_key("hello", "claude-x")
        k2 = LLMCache.make_key("hello", "claude-x")
        assert k1 == k2

    def test_prompt_affects_key(self):
        assert LLMCache.make_key("a", "m") != LLMCache.make_key("b", "m")

    def test_model_affects_key(self):
        assert LLMCache.make_key("p", "m1") != LLMCache.make_key("p", "m2")

    def test_is_sha256_hex(self):
        k = LLMCache.make_key("prompt", "model")
        assert len(k) == 64
        assert all(c in "0123456789abcdef" for c in k)


class TestInit:
    def test_creates_cache_dir(self, tmp_path):
        target = tmp_path / "nested" / "cache"
        assert not target.exists()
        LLMCache(target)
        assert target.is_dir()

    def test_accepts_str_path(self, tmp_path):
        cache = LLMCache(str(tmp_path / "c"))
        assert isinstance(cache.cache_dir, Path)


class TestGetPutRoundTrip:
    def test_put_then_get(self, tmp_path):
        cache = LLMCache(tmp_path)
        key = LLMCache.make_key("prompt", "model")
        cache.put(key, "the response", model="model")
        assert cache.get(key) == "the response"

    def test_get_missing_returns_none(self, tmp_path):
        cache = LLMCache(tmp_path)
        assert cache.get("nonexistent-key") is None

    def test_put_overwrites(self, tmp_path):
        cache = LLMCache(tmp_path)
        cache.put("k", "first")
        cache.put("k", "second")
        assert cache.get("k") == "second"

    def test_unicode_value(self, tmp_path):
        cache = LLMCache(tmp_path)
        cache.put("k", "résumé — naïve 日本語")
        assert cache.get("k") == "résumé — naïve 日本語"

    def test_put_writes_metadata(self, tmp_path):
        cache = LLMCache(tmp_path)
        cache.put("k", "v", model="claude-test")
        data = json.loads((tmp_path / "k.json").read_text(encoding="utf-8"))
        assert data["key"] == "k"
        assert data["value"] == "v"
        assert data["model"] == "claude-test"
        assert "created_at" in data


class TestCorruptCacheFile:
    def test_invalid_json_returns_none(self, tmp_path):
        cache = LLMCache(tmp_path)
        (tmp_path / "bad.json").write_text("{not valid json", encoding="utf-8")
        assert cache.get("bad") is None

    def test_empty_file_returns_none(self, tmp_path):
        cache = LLMCache(tmp_path)
        (tmp_path / "empty.json").write_text("", encoding="utf-8")
        assert cache.get("empty") is None

    def test_valid_json_missing_value_key(self, tmp_path):
        cache = LLMCache(tmp_path)
        (tmp_path / "k.json").write_text(
            json.dumps({"created_at": time.time()}), encoding="utf-8"
        )
        # No "value" key -> .get() returns None.
        assert cache.get("k") is None


class TestExpiry:
    def test_expired_entry_returns_none(self, tmp_path):
        cache = LLMCache(tmp_path, ttl_seconds=100)
        # Write an entry with an old created_at timestamp.
        (tmp_path / "old.json").write_text(
            json.dumps(
                {"key": "old", "value": "stale", "created_at": time.time() - 500}
            ),
            encoding="utf-8",
        )
        assert cache.get("old") is None

    def test_fresh_entry_within_ttl_returns_value(self, tmp_path):
        cache = LLMCache(tmp_path, ttl_seconds=100)
        cache.put("k", "fresh")
        assert cache.get("k") == "fresh"

    def test_ttl_zero_disables_expiry(self, tmp_path):
        cache = LLMCache(tmp_path, ttl_seconds=0)
        (tmp_path / "ancient.json").write_text(
            json.dumps(
                {"key": "ancient", "value": "v", "created_at": time.time() - 10**9}
            ),
            encoding="utf-8",
        )
        assert cache.get("ancient") == "v"

    def test_missing_created_at_treated_as_epoch_and_expires(self, tmp_path):
        cache = LLMCache(tmp_path, ttl_seconds=100)
        (tmp_path / "k.json").write_text(
            json.dumps({"key": "k", "value": "v"}), encoding="utf-8"
        )
        # created_at defaults to 0.0 -> ancient -> expired.
        assert cache.get("k") is None


class TestClear:
    def test_clear_removes_all_and_returns_count(self, tmp_path):
        cache = LLMCache(tmp_path)
        cache.put("a", "1")
        cache.put("b", "2")
        cache.put("c", "3")
        assert cache.clear() == 3
        assert cache.get("a") is None
        assert list(tmp_path.glob("*.json")) == []

    def test_clear_empty_cache_returns_zero(self, tmp_path):
        cache = LLMCache(tmp_path)
        assert cache.clear() == 0


class TestEvictExpired:
    def test_evicts_only_expired_entries(self, tmp_path):
        cache = LLMCache(tmp_path, ttl_seconds=100)
        cache.put("fresh", "still good")
        (tmp_path / "stale.json").write_text(
            json.dumps(
                {"key": "stale", "value": "v", "created_at": time.time() - 999}
            ),
            encoding="utf-8",
        )
        evicted = cache.evict_expired()
        assert evicted == 1
        assert cache.get("fresh") == "still good"
        assert not (tmp_path / "stale.json").exists()

    def test_evict_skips_corrupt_files(self, tmp_path):
        cache = LLMCache(tmp_path, ttl_seconds=100)
        (tmp_path / "corrupt.json").write_text("garbage", encoding="utf-8")
        # Corrupt files are skipped silently, not counted as evicted.
        assert cache.evict_expired() == 0
        assert (tmp_path / "corrupt.json").exists()

    def test_evict_with_ttl_zero_evicts_nothing(self, tmp_path):
        cache = LLMCache(tmp_path, ttl_seconds=0)
        (tmp_path / "old.json").write_text(
            json.dumps({"key": "old", "value": "v", "created_at": 0.0}),
            encoding="utf-8",
        )
        assert cache.evict_expired() == 0
