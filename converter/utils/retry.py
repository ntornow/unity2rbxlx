"""
Exponential back-off retry utility.

Provides ``exponential_backoff_retry`` — a direct-call helper that retries a
zero-arg callable on exception, and optionally when a predicate flags the
return value (used by ``cloud_api`` to retry on HTTP 429/500 responses).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


def exponential_backoff_retry(
    func: Callable[..., Any],
    max_retries: int = 5,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    retry_on: Callable[[Any], bool] | None = None,
) -> Any:
    """Retry a function with exponential backoff, optionally checking the result.

    Retries when *func* raises, and also when *retry_on* returns ``True`` for
    the return value (used by cloud_api to retry on HTTP 429/500).

    Args:
        func: Callable to invoke (no args).
        max_retries: Maximum number of retry attempts.
        base_delay: Initial delay in seconds.
        max_delay: Maximum delay cap.
        retry_on: Optional predicate on the return value. If it returns True,
            the call is retried.

    Returns:
        The return value of *func*.
    """
    delay = base_delay
    last_result = None

    for attempt in range(1, max_retries + 2):
        try:
            result = func()
        except Exception:
            if attempt > max_retries:
                raise
            logger.warning("Attempt %d failed, retrying in %.1fs...", attempt, delay)
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
            continue

        if retry_on and retry_on(result) and attempt <= max_retries:
            logger.warning("Attempt %d: retry_on triggered, retrying in %.1fs...", attempt, delay)
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
            last_result = result
            continue

        return result

    return last_result
