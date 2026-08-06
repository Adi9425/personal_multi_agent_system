from voyageai.client_async import AsyncClient

from app.config import settings
from services.usage import record_usage

EMBEDDING_MODEL = "voyage-3-lite"

_client = AsyncClient(api_key=settings.voyage_api_key)


async def embed(text: str, *, user_id: int | None = None, action: str = "embed") -> list[float]:
    """user_id optional for the same reason as services/llm.py:call_structured — evals'
    direct calls shouldn't write synthetic usage_records rows. `action` lets call sites
    label which flow this embedding is for (e.g. "embed-capture", "embed-query")."""
    result = await _client.embed(
        [text],
        model=EMBEDDING_MODEL,
        input_type="document",
        output_dimension=settings.embedding_dim,
    )
    if user_id is not None:
        await record_usage(
            user_id=user_id,
            action=action,
            model=EMBEDDING_MODEL,
            input_tokens=result.total_tokens,
            output_tokens=0,
        )
    return result.embeddings[0]
