"""Unit tests for _match_shared_group — lenient for reads, strict for sends."""

from ai_api.agent.tools.group_context import _match_shared_group
from ai_api.whatsapp.client import SharedGroup

BOOK = SharedGroup(group_jid="1@g.us", subject="Book Club")
HIKE = SharedGroup(group_jid="2@g.us", subject="Hiking Crew")
HIKE2 = SharedGroup(group_jid="3@g.us", subject="Hiking Planning")
GROUPS = [BOOK, HIKE, HIKE2]


class TestLenient:
    def test_exact_match_is_case_insensitive(self):
        assert _match_shared_group(GROUPS, "BOOK CLUB") is BOOK

    def test_substring_match(self):
        assert _match_shared_group(GROUPS, "book") is BOOK

    def test_fuzzy_match_tolerates_typos(self):
        assert _match_shared_group(GROUPS, "Bok Clb") is BOOK

    def test_no_match_returns_none(self):
        assert _match_shared_group(GROUPS, "Secret Society") is None

    def test_empty_groups_returns_none(self):
        assert _match_shared_group([], "Book Club") is None

    def test_empty_name_never_matches(self):
        assert _match_shared_group([BOOK], "") is None
        assert _match_shared_group([BOOK], "   ") is None


class TestStrict:
    def test_rejects_ambiguous_substring(self):
        assert _match_shared_group(GROUPS, "hiking", strict=True) is None

    def test_accepts_unique_substring(self):
        assert _match_shared_group(GROUPS, "crew", strict=True) is HIKE

    def test_exact_wins_over_substring_siblings(self):
        assert _match_shared_group(GROUPS, "hiking crew", strict=True) is HIKE

    def test_drops_fuzzy(self):
        assert _match_shared_group(GROUPS, "Bok Clb", strict=True) is None

    def test_duplicate_exact_names_are_ambiguous(self):
        twin = SharedGroup(group_jid="9@g.us", subject="Book Club")
        assert _match_shared_group([BOOK, twin], "Book Club", strict=True) is None
        assert _match_shared_group([BOOK, twin], "Book Club") is BOOK
