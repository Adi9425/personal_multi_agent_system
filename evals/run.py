"""§10: not optional, not something to add at the end. Run this on every prompt change.

Only checks fields Phase 3 can actually produce: `action` for every case (intent
classification), plus category/kind/has_due for cases whose expected action is "capture"
(the only flow implemented so far). update/query cases still exercise the classifier —
their deeper fields (operation, status, etc.) wait for Phases 4/5.
"""

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.notes import classify_intent
from agents.notes.capture import extract

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
