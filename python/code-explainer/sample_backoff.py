"""A small retry helper, used as sample input for the code explainer."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class RetryableError(Exception):
    """Raised by an operation when trying again later may succeed."""


def with_backoff(
    operation: Callable[[], T],
    attempts: int = 4,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
) -> T:
    """Call `operation`, retrying RetryableError with exponential backoff and full jitter."""
    for attempt in range(attempts):
        try:
            return operation()
        except RetryableError:
            if attempt == attempts - 1:
                raise
            delay = min(max_delay, base_delay * 2**attempt)
            time.sleep(random.uniform(0, delay))
    raise AssertionError("unreachable")
