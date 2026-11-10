"""Shared pgvector search helpers.

Both ``knowledge_base_chunks.embedding`` and ``conversation_messages.embedding``
are ``vector(3072)``. pgvector indexes ``vector`` only up to 2,000 dimensions,
so each table carries an HNSW expression index on ``embedding::halfvec(3072)``
(``halfvec`` indexes up to 4,000). A query uses that index only when it orders
by the same expression, so every search builds its distance with
:data:`HALFVEC_DISTANCE_SQL`.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..logger import logger

EMBEDDING_DIMENSIONS = 3072

# ``{col}`` is the embedding column (optionally table-qualified). The query
# vector is bound as ``:embedding``.
HALFVEC_DISTANCE_SQL = (
    f"({{col}}::halfvec({EMBEDDING_DIMENSIONS}) "
    f"<=> CAST(:embedding AS halfvec({EMBEDDING_DIMENSIONS})))"
)

# Candidates the HNSW scan considers. Must be >= the LIMIT of the inner ANN
# query, or the index returns fewer rows than asked for.
HNSW_EF_SEARCH = 100
# pgvector rejects a larger hnsw.ef_search (DataError: valid range 1 .. 1000).
HNSW_EF_SEARCH_MAX = 1000

_iterative_scan_supported: bool | None = None


def halfvec_distance(col: str) -> str:
    """SQL for the cosine distance between ``col`` and ``:embedding`` at half precision."""
    return HALFVEC_DISTANCE_SQL.format(col=col)


def _pgvector_supports_iterative_scan(db: Session) -> bool:
    """True when the installed pgvector is >= 0.8 (``hnsw.iterative_scan``). Cached."""
    global _iterative_scan_supported
    if _iterative_scan_supported is None:
        version = db.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar()
        supported = False
        if isinstance(version, str):
            try:
                major, minor = (int(p) for p in version.split(".")[:2])
                supported = (major, minor) >= (0, 8)
            except ValueError:
                supported = False
        _iterative_scan_supported = supported
        if supported:
            logger.info(f"pgvector {version}: HNSW iterative scan on")
        else:
            logger.warning(
                f"pgvector {version} has no HNSW iterative scan (needs >= 0.8): filtered "
                "vector searches can return fewer rows than asked for. Upgrade the image "
                "and run ALTER EXTENSION vector UPDATE (docs/media-and-kb.md)."
            )
    return _iterative_scan_supported


def prepare_hnsw_search(db: Session, ef_search: int = HNSW_EF_SEARCH) -> None:
    """Tune the HNSW scan for the next query, in the current transaction only.

    Call it right before the search query, in the same transaction: the values
    are ``SET LOCAL`` (``set_config(..., true)``), so they last until that
    transaction ends and nothing else should rely on them.

    - ``hnsw.ef_search`` is raised (at least :data:`HNSW_EF_SEARCH`) so the
      inner ``LIMIT`` can be filled; pass a larger ``ef_search`` for a larger
      LIMIT. It is capped at :data:`HNSW_EF_SEARCH_MAX`, pgvector's maximum.
    - On pgvector >= 0.8, ``hnsw.iterative_scan = relaxed_order`` keeps scanning
      when filters (user, status, scope) drop candidates, so a filtered search
      still returns its LIMIT. Older pgvector rejects that setting, hence the
      version check. Callers re-sort the rows, as ``relaxed_order`` requires.
    """
    db.execute(
        text("SELECT set_config('hnsw.ef_search', :ef, true)"),
        {"ef": str(min(max(HNSW_EF_SEARCH, ef_search), HNSW_EF_SEARCH_MAX))},
    )
    if _pgvector_supports_iterative_scan(db):
        db.execute(text("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)"))
