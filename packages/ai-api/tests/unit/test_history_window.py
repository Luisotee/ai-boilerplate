"""The stepped history window (database.stepped_window_start).

The provider's prompt cache only reuses an exact prefix of the request. With a
plain last-N window the history's first message changes on every turn, so
nothing after the instructions is ever reused; with the stepped window the
start stays put for `step` messages, so each request repeats the previous
one's history and only adds to its end.
"""

import pytest

from ai_api.database import stepped_window_start


@pytest.mark.parametrize(
    "total,limit,step,expected",
    [
        (0, 20, 10, 0),
        (20, 20, 10, 0),  # fits: everything
        (21, 20, 10, 0),  # 21 messages, still from the first
        (29, 20, 10, 0),  # 29: the window grows up to limit + step - 1
        (30, 20, 10, 10),  # jumps by a whole step
        (39, 20, 10, 10),
        (40, 20, 10, 20),
        (25, 20, 1, 5),  # step 1 = the plain last-N window
        (25, 20, 0, 5),  # 0 is treated as 1
    ],
)
def test_stepped_window_start(total, limit, step, expected):
    assert stepped_window_start(total, limit, step) == expected


@pytest.mark.parametrize("total", range(0, 200))
@pytest.mark.parametrize("limit,step", [(20, 10), (30, 10), (20, 1), (5, 7)])
def test_window_never_shorter_than_the_limit_nor_longer_than_limit_plus_step(total, limit, step):
    size = total - stepped_window_start(total, limit, step)
    assert size == total if total <= limit else limit <= size <= limit + max(step, 1) - 1


def _reused_share(limit: int, step: int, turns: int = 60) -> float:
    """Average share of each request's history that repeats the previous request's.

    A private chat: every turn saves the user's message and the bot's reply.
    """
    shares = []
    previous = None
    for turn in range(turns):
        total = 2 * turn  # messages saved before this turn's prompt
        window = list(range(stepped_window_start(total, limit, step), total))
        if previous and window:
            common = 0
            for a, b in zip(previous, window, strict=False):
                if a != b:
                    break
                common += 1
            shares.append(common / len(window))
        previous = window
    return sum(shares) / len(shares)


def test_sliding_window_reuses_nothing_once_full():
    # Before the window fills, the history only grows; after, every turn
    # shifts it by two messages and the cached prefix stops at the instructions.
    assert _reused_share(20, 1, turns=200) < 0.15


def test_stepped_window_reuses_most_of_the_history():
    # A step of 10 messages is 5 private turns: 4 of them repeat the whole
    # previous history, the 5th (the jump) repeats none of it. ~74% on average.
    assert _reused_share(20, 10, turns=200) > 0.7


class TestGetConversationHistoryQuery:
    """The query itself: newest first with a LIMIT (a bounded sort), reversed."""

    def _run(self, step: int, total: int, limit: int = 20):
        from unittest.mock import MagicMock, patch

        from ai_api import database

        query = MagicMock()
        query.filter.return_value = query
        query.order_by.return_value = query
        query.limit.return_value = query
        query.count.return_value = total
        query.all.return_value = ["m3", "m2", "m1"]
        db = MagicMock()
        db.query.return_value = query
        with (
            patch.object(database, "get_or_create_user", return_value=MagicMock(id="u1")),
            patch.object(database.settings, "history_window_step", step),
        ):
            result = database.get_conversation_history(db, "jid", "private", limit=limit)
        return query, result

    def test_step_one_skips_count_and_limits_to_the_limit(self):
        query, result = self._run(step=1, total=500)
        query.count.assert_not_called()
        query.limit.assert_called_once_with(20)
        assert result == ["m1", "m2", "m3"]  # oldest first

    def test_stepped_window_limits_to_the_window_size(self):
        query, _ = self._run(step=10, total=35)
        query.count.assert_called_once()
        query.limit.assert_called_once_with(35 - stepped_window_start(35, 20, 10))
