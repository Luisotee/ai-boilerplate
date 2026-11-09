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
