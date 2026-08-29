from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from core.schemas import QueryPlan

# D8: search reads searchable_items only, never `entries` directly. Later agents (calendar,
# job pipeline) join the view via UNION ALL; this filter/ranking logic never has to change.
_FILTERED_CTE = """
    filtered AS (
        SELECT * FROM searchable_items
        WHERE user_id = (:user_id)::bigint
          AND ((:category)::text IS NULL OR category = (:category)::text)
          AND ((:status)::text IS NULL OR status = (:status)::text)
          AND ((:kind)::text IS NULL OR kind = (:kind)::text)
          AND ((:due_before)::timestamptz IS NULL OR due_at <= (:due_before)::timestamptz)
          AND (
            (:completed_after)::timestamptz IS NULL
            OR (status = 'done' AND updated_at >= (:completed_after)::timestamptz)
          )
    )
"""


def _vector_literal(embedding: list[float]) -> str:
    # asyncpg has no `vector` codec registered on SQLAlchemy's pooled connections — bind the
    # text literal form ("[0.1,0.2,...]", which pgvector's input parser accepts) and CAST it
    # in SQL instead. Confirmed empirically against the real Postgres before writing this.
    return "[" + ",".join(str(x) for x in embedding) + "]"


async def hybrid_search(
    session: AsyncSession,
    *,
    user_id: int,
    plan: QueryPlan,
    query_embedding: list[float] | None,
    apply_similarity_floor: bool = True,
) -> list[dict]:
    params = {
        "user_id": user_id,
        "category": plan.category.value if plan.category else None,
        "status": plan.status.value if plan.status else None,
        "kind": plan.kind.value if plan.kind else None,
        "due_before": plan.due_before,
        "completed_after": plan.completed_after,
        "limit": plan.limit,
    }

    if plan.search_text is None:
        query = text(
            f"""
            WITH {_FILTERED_CTE}
            SELECT *, NULL::float AS score FROM filtered
            ORDER BY due_at ASC NULLS LAST, created_at DESC
            LIMIT :limit
            """
        )
        result = await session.execute(query, params)
        return [dict(row) for row in result.mappings().all()]

    params["search_query"] = plan.search_text
    params["recency_weight"] = settings.recency_weight
    params["recency_halflife_days"] = settings.recency_halflife_days

    if query_embedding is not None:
        params["query_embedding"] = _vector_literal(query_embedding)
        # §6.2's "else: closest 2" fallback for update-resolution needs candidates even when
        # nothing is a genuinely good match — Phase 4's floor is correct for search (showing
        # nothing beats showing junk) but wrong for that fallback, so resolve.py opts out.
        floor_clause = ""
        if apply_similarity_floor:
            params["min_similarity"] = settings.min_vector_similarity
            floor_clause = f"""
                  AND 1 - (embedding <=> CAST(:query_embedding AS vector({settings.embedding_dim})))
                      > :min_similarity
            """
        vec_cte = f"""
            vec AS (
                SELECT id, row_number() OVER (
                    ORDER BY embedding <=> CAST(:query_embedding AS vector({settings.embedding_dim}))
                ) AS rank_vec
                FROM filtered
                WHERE embedding IS NOT NULL
                {floor_clause}
            ),
        """
        vec_score = "COALESCE(1.0 / (60 + vec.rank_vec), 0)"
        vec_join = "LEFT JOIN vec ON vec.id = f.id"
        vec_match = "OR vec.id IS NOT NULL"
    else:
        vec_cte = ""
        vec_score = "0"
        vec_join = ""
        vec_match = ""

    query = text(
        f"""
        WITH {_FILTERED_CTE},
        {vec_cte}
        fts AS (
            SELECT id, row_number() OVER (
                ORDER BY ts_rank_cd(search_tsv, websearch_to_tsquery('english', :search_query)) DESC
            ) AS rank_fts
            FROM filtered
            WHERE search_tsv @@ websearch_to_tsquery('english', :search_query)
        )
        SELECT f.*,
            COALESCE(1.0 / (60 + fts.rank_fts), 0) + {vec_score}
              + :recency_weight * exp(
                  -EXTRACT(EPOCH FROM (now() - f.created_at)) / 86400.0 / :recency_halflife_days
                ) AS score
        FROM filtered f
        LEFT JOIN fts ON fts.id = f.id
        {vec_join}
        WHERE fts.id IS NOT NULL {vec_match}
        ORDER BY score DESC
        LIMIT :limit
        """
    )
    result = await session.execute(query, params)
    return [dict(row) for row in result.mappings().all()]
