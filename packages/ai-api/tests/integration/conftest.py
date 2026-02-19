"""Integration test fixtures — disables rate limiter to prevent cross-test 429s."""

import pytest


@pytest.fixture(autouse=True)
def disable_rate_limiter():
    """Disable the SlowAPI rate limiter for integration tests."""
    from ai_api.deps import limiter

    original = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = original
