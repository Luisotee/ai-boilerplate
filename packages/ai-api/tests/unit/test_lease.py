"""streams/lease.py: a Redis lease only its holder can extend or release."""

from unittest.mock import AsyncMock, MagicMock

from ai_api.streams.lease import acquire_lease, extend_lease, release_lease


def _redis(set_ok: bool, eval_result: int = 1) -> MagicMock:
    redis = MagicMock()
    redis.set = AsyncMock(return_value=set_ok)
    redis.eval = AsyncMock(return_value=eval_result)
    return redis


async def test_acquire_takes_a_free_lease():
    redis = _redis(set_ok=True)
    assert await acquire_lease(redis, "k", "tok", 60) is True
    redis.set.assert_awaited_once_with("k", "tok", nx=True, ex=60)
    redis.eval.assert_not_called()


async def test_acquire_renews_own_lease_and_refuses_anothers():
    assert await acquire_lease(_redis(set_ok=False, eval_result=1), "k", "tok", 60) is True
    assert await acquire_lease(_redis(set_ok=False, eval_result=0), "k", "tok", 60) is False


async def test_extend_checks_the_token():
    redis = _redis(set_ok=True, eval_result=0)
    assert await extend_lease(redis, "k", "tok", 60) is False
    script, nkeys, key, token, ttl = redis.eval.await_args.args
    assert "get" in script and "expire" in script
    assert (nkeys, key, token, ttl) == (1, "k", "tok", 60)


async def test_release_never_raises():
    redis = _redis(set_ok=True)
    redis.eval.side_effect = ConnectionError("redis down")
    await release_lease(redis, "k", "tok")
