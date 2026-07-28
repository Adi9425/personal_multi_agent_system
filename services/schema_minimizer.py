from typing import Any


def strip_titles(schema: Any) -> Any:
    """Pydantic's model_json_schema() adds a "title" key to every field and nested model
    (capitalized field/class names) — decorative metadata the model doesn't need to produce
    correct output; the property key and any "description" already carry that information.
    Zero-risk: this changes nothing semantic (type/enum/required/description all survive),
    it only removes the structured-output "tax" of redundant titles."""
    if isinstance(schema, dict):
        return {key: strip_titles(value) for key, value in schema.items() if key != "title"}
    if isinstance(schema, list):
        return [strip_titles(item) for item in schema]
    return schema
