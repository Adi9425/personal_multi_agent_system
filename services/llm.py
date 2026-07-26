import logging
import time
from typing import TypeVar

from anthropic import AsyncAnthropic
from langfuse import Langfuse
from pydantic import BaseModel, ValidationError

from app.config import settings

logger = logging.getLogger(__name__)

_anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)
_langfuse = Langfuse(
    public_key=settings.langfuse_public_key,
    secret_key=settings.langfuse_secret_key,
    host=settings.langfuse_host,
)

T = TypeVar("T", bound=BaseModel)

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_ATTEMPTS = 2  # NFR-8: one retry with the validation error appended, then give up


class LLMValidationError(Exception):
    """Raised when structured output still fails validation after the retry.
    NFR-8: the caller falls back to asking the user, not a crash."""


async def call_structured(
    *,
    model: str,
    system: str,
    user: str,
    response_model: type[T],
    trace_name: str,
) -> T:
    """D7: every LLM call returns a validated Pydantic model, never free text.
    All Anthropic calls go through here (§14) — no direct SDK calls in agent code."""
    trace = _langfuse.trace(name=trace_name, input={"system": system, "user": user})

    tool_name = response_model.__name__
    tool = {
        "name": tool_name,
        "description": f"Return a {tool_name}",
        "input_schema": response_model.model_json_schema(),
    }

    messages: list[dict] = [{"role": "user", "content": user}]
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        generation = trace.generation(
            name=f"{trace_name}-attempt{attempt}",
            model=model,
            input=messages,
        )
        start = time.monotonic()
        response = await _anthropic.messages.create(
            model=model,
            max_tokens=1024,
            system=system,
            messages=messages,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool_name},
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        latency_s = time.monotonic() - start

        tool_use = next((block for block in response.content if block.type == "tool_use"), None)
        raw_output = tool_use.input if tool_use else None

        generation.end(
            output=raw_output,
            usage_details={
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
            },
            metadata={"latency_s": latency_s},
        )

        if raw_output is None:
            last_error = ValueError("model did not return a tool_use block")
            logger.warning("%s attempt %d: no tool_use block in response", trace_name, attempt)
            continue

        try:
            result = response_model.model_validate(raw_output)
            trace.update(output=result.model_dump(mode="json"))
            return result
        except ValidationError as e:
            last_error = e
            logger.warning("%s attempt %d: validation failed: %s", trace_name, attempt, e)
            messages = messages + [
                {"role": "assistant", "content": response.content},
                {
                    "role": "user",
                    "content": f"That didn't validate: {e}. Call {tool_name} again with corrected values.",
                },
            ]

    trace.update(output={"error": str(last_error)})
    raise LLMValidationError(str(last_error))
