"""Banned-claims lint (spec §35; kit Prompt H2).

Fails on any banned claim phrase appearing in README.md, docs/, dashboard/
and taskfence/ source and text. Negation-aware: "does not prevent all AI
attacks" is a disclaimer, not a claim, so negated occurrences are allowed.
"""

import re
from pathlib import Path

BANNED_PHRASES = [
    "prevents all ai attacks",
    "prevents all attacks",
    "eliminates prompt injection",
    "guarantees security",
    "guaranteed security",
    "world's first ai firewall",
    "first ai firewall",
    "no existing system can do this",
    "makes agents completely safe",
    "completely safe",
    "stops prompt injection",
    "100% secure",
]

NEGATION_PREFIXES = (
    "does not ", "doesn't ", "not ", "never ", "cannot ", "can't ",
    "won't ", "no claim", "rather than ", "instead of ",
)

SCAN_ROOTS = [Path("README.md"), Path("docs"), Path("dashboard"),
              Path("taskfence")]


def _iter_texts():
    for root in SCAN_ROOTS:
        if root.is_file():
            yield root
        elif root.is_dir():
            for pattern in ("*.py", "*.md", "*.txt"):
                for path in root.rglob(pattern):
                    yield path


def _negated(line: str, match_start: int) -> bool:
    """True when a negation word appears within the 40 chars before the hit
    on the same line (a disclaimer, e.g. 'does not prevent all AI attacks')."""
    window = line[max(0, match_start - 40):match_start].lower()
    return any(prefix in window for prefix in NEGATION_PREFIXES)


def test_no_banned_claim_phrases():
    offenders = []
    for path in _iter_texts():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for phrase in BANNED_PHRASES:
                for match in re.finditer(re.escape(phrase),
                                         line, flags=re.IGNORECASE):
                    if not _negated(line, match.start()):
                        offenders.append(
                            f"{path}:{lineno}: ...{line.strip()[:90]}")
    assert offenders == [], "\n" + "\n".join(offenders)


def test_required_honesty_lines_exist():
    """The kit requires the limitations and honesty statements to exist."""
    def plain(text: str) -> str:
        # Strip markdown emphasis/backticks so '**not**' matches 'not'.
        return text.lower().replace("*", "").replace("`", "")

    readme = plain(Path("README.md").read_text(encoding="utf-8"))
    for required in ("regex classification is coarse",
                     "conservative lineage can over-block",
                     "not hardened or isolated",
                     "limits the impact",
                     "does not eliminate prompt injection"):
        assert required in readme, required
    qa = plain(Path("docs/JUDGE_QA.md").read_text(encoding="utf-8"))
    assert ("does not claim it does" in qa
            or "don't claim it does" in qa
            or "does not stop prompt" in qa)
