"""
Unit tests for the pgvector HNSW indexes and search helpers (ai_api.rag.vector).

The test suite has no real Postgres, so these pin the DDL create_all() emits and
the SQL shape the searches send: the index only helps when queries order by
the same halfvec expression it is built on.
"""

from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

import ai_api.kb_models  # noqa: F401 — registers the KB tables on Base.metadata
from ai_api.database import Base
from ai_api.rag import vector
from ai_api.rag.conversation import search_conversation_history
from ai_api.rag.knowledge_base import search_knowledge_base


def _index_ddl(table: str, name: str) -> str:
    index = next(i for i in Base.metadata.tables[table].indexes if i.name == name)
    return str(CreateIndex(index).compile(dialect=postgresql.dialect()))


@pytest.mark.parametrize(
    ("table", "name"),
    [
        ("knowledge_base_chunks", "idx_kb_chunks_embedding_hnsw"),
        ("conversation_messages", "idx_conversation_messages_embedding_hnsw"),
    ],
)
def test_hnsw_halfvec_index_ddl(table, name):
    ddl = _index_ddl(table, name)
    assert f"CREATE INDEX {name} ON {table} USING hnsw" in ddl
    assert "CAST(embedding AS HALFVEC(3072)) halfvec_cosine_ops" in ddl


def test_halfvec_distance_matches_the_index_expression():
    sql = vector.halfvec_distance("c.embedding")
    assert sql == "(c.embedding::halfvec(3072) <=> CAST(:embedding AS halfvec(3072)))"


@pytest.fixture(autouse=True)
def _reset_version_cache():
    vector._iterative_scan_supported = None
    yield
    vector._iterative_scan_supported = None


def _db_with_version(version):
    db = MagicMock()
    db.execute.return_value.scalar.return_value = version
    db.execute.return_value.fetchall.return_value = []
    return db


def _sql_calls(db) -> list[str]:
    return [str(call.args[0]) for call in db.execute.call_args_list]


class TestPrepareHnswSearch:
    def test_sets_ef_search_locally(self):
        db = _db_with_version("0.7.4")
        vector.prepare_hnsw_search(db)
        first = db.execute.call_args_list[0]
        assert "set_config('hnsw.ef_search', :ef, true)" in str(first.args[0])
        assert first.args[1] == {"ef": str(vector.HNSW_EF_SEARCH)}

    def test_iterative_scan_on_pgvector_0_8(self):
        db = _db_with_version("0.8.1")
        vector.prepare_hnsw_search(db)
        assert any("hnsw.iterative_scan" in s for s in _sql_calls(db))

    def test_no_iterative_scan_before_0_8(self):
        # pgvector < 0.8 rejects the setting, which would abort the transaction.
        db = _db_with_version("0.7.4")
        vector.prepare_hnsw_search(db)
        assert not any("hnsw.iterative_scan" in s for s in _sql_calls(db))

    def test_version_checked_once(self):
        db = _db_with_version("0.8.0")
        vector.prepare_hnsw_search(db)
        vector.prepare_hnsw_search(db)
        version_queries = [s for s in _sql_calls(db) if "pg_extension" in s]
        assert len(version_queries) == 1

    def test_unparseable_version_is_unsupported(self):
        db = _db_with_version(None)
        vector.prepare_hnsw_search(db)
        assert not any("hnsw.iterative_scan" in s for s in _sql_calls(db))


async def test_conversation_search_orders_by_halfvec_distance():
    db = _db_with_version("0.8.0")
    await search_conversation_history(
        db,
        [0.1] * 3072,
        "user-1",
        limit=5,
        similarity_threshold=0.5,
        include_context=False,
        context_window=0,
    )
    calls = _sql_calls(db)
    search_sql = calls[-1]
    assert "ORDER BY (embedding::halfvec(3072) <=> CAST(:embedding AS halfvec(3072)))" in search_sql
    # The HNSW settings precede the search, in the same session.
    assert any("hnsw.ef_search" in s for s in calls[:-1])


async def test_kb_vector_query_orders_by_halfvec_distance():
    db = _db_with_version("0.8.0")
    await search_knowledge_base(db, [0.1] * 3072, limit=5, similarity_threshold=0.5)
    search_sql = _sql_calls(db)[-1]
    assert "(c.embedding::halfvec(3072) <=> CAST(:embedding AS halfvec(3072)))" in search_sql
