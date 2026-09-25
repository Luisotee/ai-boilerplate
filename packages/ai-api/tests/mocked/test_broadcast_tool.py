"""The set_broadcast_subscription agent tool: private chats, and admin-only in groups."""

from unittest.mock import MagicMock

import pytest

from ai_api.agent.tools.broadcast import set_broadcast_subscription


def _ctx(conversation_type="private", is_group_admin=None, opt_out=False):
    ctx = MagicMock()
    ctx.deps.user_id = "user-123"
    ctx.deps.is_group_admin = is_group_admin
    user = MagicMock()
    user.conversation_type = conversation_type
    user.broadcast_opt_out = opt_out
    ctx.deps.db.get.return_value = user
    return ctx, user


class TestSetBroadcastSubscription:
    async def test_private_user_opts_out(self):
        ctx, user = _ctx()
        result = await set_broadcast_subscription(ctx, subscribed=False)
        assert user.broadcast_opt_out is True
        ctx.deps.db.commit.assert_called_once()
        assert "now off for you" in result

    async def test_private_user_opts_back_in(self):
        ctx, user = _ctx(opt_out=True)
        result = await set_broadcast_subscription(ctx, subscribed=True)
        assert user.broadcast_opt_out is False
        assert "on again" in result

    async def test_already_off_is_a_no_op(self):
        ctx, _ = _ctx(opt_out=True)
        result = await set_broadcast_subscription(ctx, subscribed=False)
        ctx.deps.db.commit.assert_not_called()
        assert "already off" in result

    async def test_group_admin_changes_the_group(self):
        ctx, user = _ctx("group", is_group_admin=True)
        result = await set_broadcast_subscription(ctx, subscribed=False)
        assert user.broadcast_opt_out is True
        assert "this group" in result

    @pytest.mark.parametrize("is_admin", [False, None])
    async def test_group_non_admin_refused(self, is_admin):
        ctx, user = _ctx("group", is_group_admin=is_admin)
        result = await set_broadcast_subscription(ctx, subscribed=False)
        assert user.broadcast_opt_out is False
        ctx.deps.db.commit.assert_not_called()
        assert "Only a group admin" in result

    async def test_db_error_rolls_back_and_stays_generic(self):
        ctx, _ = _ctx()
        ctx.deps.db.commit.side_effect = RuntimeError("psycopg2: host db-internal:5432")
        result = await set_broadcast_subscription(ctx, subscribed=False)
        ctx.deps.db.rollback.assert_called_once()
        assert "db-internal" not in result
        assert result.startswith("Failed")
