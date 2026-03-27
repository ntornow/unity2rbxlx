"""
Exponential back-off retry utilities.

Provides both a direct-call helper and a decorator for wrapping functions with
automatic retry logic.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Any, Callable, Tuple, Type, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def retry(
    func: Callable[..., Any],
    *args: Any,
    max_attempts: int = 4,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    retryable_exceptions: Tuple[Type[BaseException], ...] = (Exception,),
    **kwargs: Any,
) -> Any:
    """Invoke *func* with exponential back-off retry on failure.

    Args:
        func: The callable to execute.
        *args: Positional arguments forwarded to *func*.
        max_attempts: Maximum number of attempts (including the first).
        base_delay: Initial delay in seconds before the first retry.
        max_delay: Upper bound on the delay between retries.
        backoff_factor: Multiplicative factor applied to the delay after each
            failed attempt.
        retryable_exceptions: Tuple of exception types that trigger a retry.
            Any exception *not* in this tuple will propagate immediately.
        **kwargs: Keyword arguments forwarded to *func*.

    Returns:
        The return value of *func* on a successful call.

    Raises:
        The last exception raised by *func* if all attempts are exhausted.
    """
    delay = base_delay
    last_exception: BaseException | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except retryable_exceptions as exc:
            last_exception = exc
            if attempt == max_attempts:
                logger.error(
                    "All %d attempts failed for %s: %s",
                    max_attempts,
                    func.__qualname__,
                    exc,
                )
                raise
            logger.warning(
                "Attempt %d/%d for %s failed (%s). Retrying in %.1fs...",
                attempt,
                max_attempts,
                func.__qualname__,
                exc,
                delay,
            )
            time.sleep(delay)
            delay = min(delay * backoff_factor, max_delay)

    # Should never reach here, but satisfy type checkers
    raise last_exception  # type: ignore[misc]


def with_retry(
    max_attempts: int = 4,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    retryable_exceptions: Tuple[Type[BaseException], ...] = (Exception,),
) -> Callable[[F], F]:
    """Decorator that wraps a function with exponential back-off retry.

    Usage::

        @with_retry(max_attempts=3, base_delay=1.0)
        def flaky_api_call(url: str) -> dict:
            ...

    Args:
        max_attempts: Maximum number of attempts.
        base_delay: Initial delay in seconds before the first retry.
        max_delay: Upper bound on the delay between retries.
        backoff_factor: Multiplicative factor for the delay.
        retryable_exceptions: Exception types that trigger a retry.

    Returns:
        A decorator that wraps the target function.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return retry(
                func,
                *args,
                max_attempts=max_attempts,
                base_delay=base_delay,
                max_delay=max_delay,
                backoff_factor=backoff_factor,
                retryable_exceptions=retryable_exceptions,
                **kwargs,
            )

        return wrapper  # type: ignore[return-value]

    return decorator


def exponential_backoff_retry(
    func: Callable[..., Any],
    max_retries: int = 5,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    retry_on: Callable[[Any], bool] | None = None,
) -> Any:
    """Retry a function with exponential backoff, optionally checking return value.

    Unlike :func:`retry`, this function also retries when *retry_on* returns
    ``True`` for the result (used by cloud_api to retry on HTTP 429/500).

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
