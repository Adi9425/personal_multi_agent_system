"""§10: not optional, not something to add at the end. Run this on every prompt change.

Checks `action` for every case (intent classification), plus category/kind/has_due for
capture cases and category/status/has_due_filter for query cases — the two flows actually
implemented so far. update cases still exercise the classifier only; their deeper fields
(operation, etc.) wait for Phase 5.
"""

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.notes import classify_intent
from agents.notes.capture import extract
from agents.notes.query import extract_plan

CASES_PATH = Path(__file__).parent / "cases.jsonl"


def load_cases() -> list[dict]:
    cases = []
    with open(CASES_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


async def run_case(case: dict) -> dict:
    text = case["text"]
    expect = case["expect"]
    fields: dict[str, tuple] = {}

    intent = await classify_intent(text)
    fields["action"] = (expect.get("action"), intent.action, expect.get("action") == intent.action)

    if expect.get("action") == "capture" and intent.action == "capture":
        try:
            captured = await extract(text)
        except Exception as e:
            fields["extract_error"] = (None, str(e), False)
            return {"id": case["id"], "text": text, "fields": fields}

        if "category" in expect:
            actual = captured.category.value
            fields["category"] = (expect["category"], actual, expect["category"] == actual)
        if "kind" in expect:
            actual = captured.kind.value
            fields["kind"] = (expect["kind"], actual, expect["kind"] == actual)
        if "has_due" in expect:
            actual = captured.due_at is not None
            fields["has_due"] = (expect["has_due"], actual, expect["has_due"] == actual)

    if expect.get("action") == "query" and intent.action == "query":
        try:
            plan = await extract_plan(text)
        except Exception as e:
            fields["extract_plan_error"] = (None, str(e), False)
            return {"id": case["id"], "text": text, "fields": fields}

        if "category" in expect:
            actual = plan.category.value if plan.category else None
            fields["category"] = (expect["category"], actual, expect["category"] == actual)
        if "status" in expect:
            actual = plan.status.value if plan.status else None
            fields["status"] = (expect["status"], actual, expect["status"] == actual)
        if "has_due_filter" in expect:
            actual = plan.due_before is not None
            fields["has_due_filter"] = (expect["has_due_filter"], actual, expect["has_due_filter"] == actual)

    return {"id": case["id"], "text": text, "fields": fields}


async def main() -> None:
    cases = load_cases()
    results = [await run_case(case) for case in cases]

    for r in results:
        status = "PASS" if all(ok for _, _, ok in r["fields"].values()) else "FAIL"
        print(f"[{status}] {r['id']}: {r['text'][:60]!r}")
        for field, (exp, act, ok) in r["fields"].items():
            mark = "  ok" if ok else "  XX"
            print(f"{mark} {field}: expected={exp!r} actual={act!r}")

    field_totals: Counter = Counter()
    field_correct: Counter = Counter()
    for r in results:
        for field, (_, _, ok) in r["fields"].items():
            field_totals[field] += 1
            if ok:
                field_correct[field] += 1

    print("\n--- Per-field accuracy ---")
    for field in field_totals:
        total = field_totals[field]
        correct = field_correct[field]
        print(f"{field}: {correct}/{total} ({correct / total:.0%})")

    print("\n--- Category confusion matrix (capture cases only) ---")
    matrix: Counter = Counter()
    categories: set[str] = set()
    for r in results:
        if "category" in r["fields"]:
            exp, act, _ = r["fields"]["category"]
            matrix[(exp, act)] += 1
            categories.add(exp)
            categories.add(act)

    sorted_categories = sorted(categories)
    if sorted_categories:
        header = "expected \\ actual".ljust(18) + " ".join(c[:12].ljust(12) for c in sorted_categories)
        print(header)
        for exp in sorted_categories:
            row = exp.ljust(18) + " ".join(
                str(matrix.get((exp, act), 0)).ljust(12) for act in sorted_categories
            )
            print(row)
    else:
        print("(no capture cases were correctly routed to compare)")


if __name__ == "__main__":
    asyncio.run(main())
