"""
Disk-based cache for Claude API (LLM) responses.

Responses are stored as JSON files named by the SHA-256 hash of the prompt
and model identifier, with a configurable time-to-live (TTL).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class LLMCache:
    """SHA-256-keyed, TTL-aware disk cache for LLM responses.

    Each cache entry is stored as a JSON file::

        {
            "key": "<original_key>",
            "value": "<cached_response>",
            "created_at": <unix_timestamp>,
            "model": "<model_id>"
        }

    Args:
        cache_dir: Directory in which cache files are stored.
        ttl_seconds: Time-to-live for cache entries in seconds.  Entries
            older than this are treated as expired and return ``None``.
            A value of ``0`` disables expiry.
    """

    def __init__(self, cache_dir: str | Path, ttl_seconds: int = 86400) -> None:
        self.cache_dir = Path(cache_dir)
        self.ttl_seconds = ttl_seconds
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        logger.debug("LLMCache initialised at %s (TTL=%ds)", self.cache_dir, ttl_seconds)

    # ------------------------------------------------------------------
    # Key derivation
    # ------------------------------------------------------------------

    @staticmethod
    def make_key(prompt: str, model: str) -> str:
        """Derive a deterministic cache key from a prompt and model identifier.

        Args:
            prompt: The full prompt text sent to the LLM.
            model: The model identifier (e.g. ``"claude-sonnet-4-20250514"``).

        Returns:
            A hex-encoded SHA-256 digest.
        """
        data = f"{model}::{prompt}"
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _path_for_key(self, key: str) -> Path:
        """Return the filesystem path for a cache key."""
        return self.cache_dir / f"{key}.json"

    def _is_expired(self, created_at: float) -> bool:
        """Check whether an entry created at *created_at* has expired."""
        if self.ttl_seconds <= 0:
            return False
        return (time.time() - created_at) > self.ttl_seconds

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[str]:
        """Retrieve a cached response by key.

        Args:
            key: The cache key (typically from :meth:`make_key`).

        Returns:
            The cached response string, or ``None`` if the key is not found
            or the entry has expired.
        """
        path = self._path_for_key(key)
        if not path.exists():
            logger.debug("Cache miss (not found): %s", key[:16])
            return None

        try:
            data: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Cache read error for %s: %s", key[:16], exc)
            return None

        created_at = data.get("created_at", 0.0)
        if self._is_expired(created_at):
            logger.debug("Cache miss (expired): %s", key[:16])
            return None

        logger.debug("Cache hit: %s", key[:16])
        return data.get("value")

    def put(self, key: str, value: str, model: str = "") -> None:
        """Store a response in the cache.

        Args:
            key: The cache key.
            value: The LLM response text to cache.
            model: Optional model identifier stored alongside the entry for
                debugging purposes.
        """
        path = self._path_for_key(key)
        data: Dict[str, Any] = {
            "key": key,
            "value": value,
            "created_at": time.time(),
            "model": model,
        }
        try:
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            logger.debug("Cache put: %s (%d chars)", key[:16], len(value))
        except OSError as exc:
            logger.warning("Cache write error for %s: %s", key[:16], exc)

    def clear(self) -> int:
        """Remove all cache entries.

        Returns:
            The number of entries removed.
        """
        count = 0
        for path in self.cache_dir.glob("*.json"):
            try:
                path.unlink()
                count += 1
            except OSError:
                pass
        logger.info("Cleared %d cache entries", count)
        return count

    def evict_expired(self) -> int:
        """Remove expired cache entries.

        Returns:
            The number of entries evicted.
        """
        count = 0
        for path in self.cache_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if self._is_expired(data.get("created_at", 0.0)):
                    path.unlink()
                    count += 1
            except (json.JSONDecodeError, OSError):
                pass
        logger.info("Evicted %d expired cache entries", count)
        return count
