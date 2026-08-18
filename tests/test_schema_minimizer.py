import json

from core.schemas import AnswerSynthesis, CapturedEntry, IntentResult, QueryPlan, UpdateRequest
from services.schema_minimizer import strip_titles

ALL_RESPONSE_MODELS = [IntentResult, CapturedEntry, QueryPlan, AnswerSynthesis, UpdateRequest]


def _has_no_titles(obj) -> bool:
    if isinstance(obj, dict):
        return "title" not in obj and all(_has_no_titles(v) for v in obj.values())
    if isinstance(obj, list):
        return all(_has_no_titles(item) for item in obj)
    return True


def test_titles_stripped():
    for model in ALL_RESPONSE_MODELS:
        minimized = strip_titles(model.model_json_schema())
        assert _has_no_titles(minimized), f"{model.__name__} still has a title key"


def test_semantic_keys_preserved():
    # None of core/schemas.py's fields currently set an explicit Pydantic `description=`
    # (field semantics live in the system prompts instead) — so `description` isn't a key
    # to check for here. What every field-constrained schema does carry: enum values,
    # numeric bounds, string length limits, and the required-fields list.
    minimized = strip_titles(IntentResult.model_json_schema())
    assert minimized["properties"]["action"]["enum"] == ["capture", "update", "query", "unknown"]
    assert minimized["properties"]["confidence"]["minimum"] == 0.0
    assert minimized["properties"]["confidence"]["maximum"] == 1.0
    assert minimized["properties"]["reasoning"]["maxLength"] == 200
    assert set(minimized["required"]) == {"action", "confidence", "reasoning"}


def test_schema_actually_shrinks():
    total_raw, total_min = 0, 0
    for model in ALL_RESPONSE_MODELS:
        raw = model.model_json_schema()
        minimized = strip_titles(raw)
        raw_len, min_len = len(json.dumps(raw)), len(json.dumps(minimized))
        assert min_len < raw_len, f"{model.__name__} did not shrink"
        total_raw += raw_len
        total_min += min_len

    print(f"\nOverall schema size reduction: {(total_raw - total_min) / total_raw:.1%}")
