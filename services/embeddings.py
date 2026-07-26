from voyageai.client_async import AsyncClient

from app.config import settings

EMBEDDING_MODEL = "voyage-3-lite"

_client = AsyncClient(api_key=settings.voyage_api_key)


async def embed(text: str) -> list[float]:
    result = await _client.embed(
        [text],
        model=EMBEDDING_MODEL,
        input_type="document",
        output_dimension=settings.embedding_dim,
    )
    return result.embeddings[0]
