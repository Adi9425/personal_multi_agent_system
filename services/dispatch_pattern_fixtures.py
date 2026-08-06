# Hand-authored, code-reviewed test cases for services/dispatch_patterns.py's dry-run safety
# gate. Deliberately NOT DB-editable: the safety net for a pattern change shouldn't itself be
# a runtime-mutable thing an admin could accidentally weaken. Seeded from this session's real
# debugging history (the URL-colon bug, the bare-pronoun bug) plus the known-good phrasings.

from dataclasses import dataclass


@dataclass(frozen=True)
class ExtractFixture:
    text: str
    should_match: bool
    expected_key: str | None = None
    expected_value: str | None = None  # unused for "get" flow (no value group)


@dataclass(frozen=True)
class RejectFixture:
    key: str  # an already-extracted, normalized key — reject rules never see the raw message
    should_reject: bool


SAVE_EXTRACT_FIXTURES: list[ExtractFixture] = [
    ExtractFixture(
        "save my linkedin profile: https://linkedin.com/in/x", True,
        "my linkedin profile", "https://linkedin.com/in/x",
    ),
    ExtractFixture(
        "save my linkedin profile as https://linkedin.com/in/y", True,
        "my linkedin profile", "https://linkedin.com/in/y",
    ),
    ExtractFixture(
        "remember the recruiter's email is x@y.com", True,
        "the recruiter's email", "x@y.com",
    ),
    ExtractFixture(
        "save my website: https://example.com?x=1&y=2", True,
        "my website", "https://example.com?x=1&y=2",
    ),
    ExtractFixture("my timesheet is due friday", False),  # no trigger word — a real capture
]

GET_EXTRACT_FIXTURES: list[ExtractFixture] = [
    ExtractFixture("what is my linkedin profile", True, "my linkedin profile"),
    ExtractFixture("what's my linkedin profile", True, "my linkedin profile"),
    ExtractFixture("show me my collage dance video", False),  # "show me", not "what's"/"what is"
]

# Reject rules act on an already-extracted, normalized key — never the raw message.
SAVE_REJECT_FIXTURES: list[RejectFixture] = [
    RejectFixture("this", True),
    RejectFixture("that", True),
    RejectFixture("it", True),
    RejectFixture("my linkedin profile", False),
    RejectFixture("the recruiter's email", False),
]

EXTRACT_FIXTURES_BY_FLOW: dict[str, list[ExtractFixture]] = {
    "save": SAVE_EXTRACT_FIXTURES,
    "get": GET_EXTRACT_FIXTURES,
}

REJECT_FIXTURES_BY_FLOW: dict[str, list[RejectFixture]] = {
    "save": SAVE_REJECT_FIXTURES,
}
