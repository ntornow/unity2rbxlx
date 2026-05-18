"""Tests for utils.retry.exponential_backoff_retry."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.retry import exponential_backoff_retry


# base_delay=0 keeps the exponential sleeps at 0s so tests run instantly.

class TestExponentialBackoffRetry:
    def test_success_first_try(self):
        calls = []
        assert exponential_backoff_retry(
            lambda: calls.append(1) or "ok", base_delay=0
        ) == "ok"
        assert len(calls) == 1

    def test_retries_then_succeeds(self):
        calls = []

        def f():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("transient")
            return "ok"

        assert exponential_backoff_retry(f, max_retries=5, base_delay=0) == "ok"
        assert len(calls) == 3

    def test_exhausts_then_raises(self):
        calls = []

        def f():
            calls.append(1)
            raise RuntimeError("always fails")

        with pytest.raises(RuntimeError, match="always fails"):
            exponential_backoff_retry(f, max_retries=2, base_delay=0)
        assert len(calls) == 3  # 1 initial attempt + 2 retries

    def test_retry_on_predicate_triggers_retry(self):
        results = [429, 429, 200]
        calls = []

        def f():
            calls.append(1)
            return results[len(calls) - 1]

        out = exponential_backoff_retry(
            f, max_retries=5, base_delay=0, retry_on=lambda r: r == 429
        )
        assert out == 200
        assert len(calls) == 3

    def test_retry_on_exhausted_returns_last_result(self):
        calls = []

        def f():
            calls.append(1)
            return 429  # predicate always wants a retry

        # When retries are exhausted the last result is returned, not raised.
        out = exponential_backoff_retry(
            f, max_retries=2, base_delay=0, retry_on=lambda r: r == 429
        )
        assert out == 429
        assert len(calls) == 3  # 1 + 2 retries

    def test_no_predicate_returns_first_result(self):
        assert exponential_backoff_retry(lambda: 42, base_delay=0) == 42
