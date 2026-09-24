"""Rule-based data classification (spec §9; kit Prompt B2).

Deterministic regex/keyword rules only — no ML, no network. `classify()`
returns the union of registry base labels (via the asset hint) and content
scan hits, so derived payloads inherit their origin's labels plus whatever
the content itself reveals. Tradeoffs are stated per rule: precision over
recall for high-stakes labels, coarse recall for INTERNAL vs PUBLIC.
"""

import re

from taskfence import catalog
from taskfence.registry import base_labels

_LABEL_INTERNAL = "INTERNAL"

# Each rule: (label, compiled regex). Applied to every scanned text.
_CONTENT_RULES = [
    ("PII", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(?:invalid|example|test)")),
    ("PII", re.compile(r"\b[6-9]\d{9}\b")),                       # IN mobile
    ("PII", re.compile(r"\b[2-9]\d{3}\s?\d{4}\s?\d{4}\b")),       # Aadhaar-like
    ("PII", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),               # PAN-like
    ("FINANCIAL", re.compile(r"(?:₹|INR)\s?[\d,]+")),
    ("FINANCIAL", re.compile(r"(?i)\b(?:salary|ctc|compensation)\b")),
    ("SOURCE_CODE", re.compile(r"(?m)^\s*(?:def |class |import |from \w+ import)")),
    ("SOURCE_CODE", re.compile(r"\bsk-(?:live|test)-[A-Za-z0-9]{16,}\b")),  # API key
    ("CUSTOMER_DATA", re.compile(r"\b(?:CU-?\d{3,}|CUST-\d+)\b")),  # record id
]

# Substrings that demote otherwise-ambiguous content from PUBLIC.
_PRIVATE_HINTS = ("internal", "confidential", "do not distribute",
                  "employee", "salary", "customer")


def _contains_private_hint(text: str) -> bool:
    low = text.lower()
    return any(hint in low for hint in _PRIVATE_HINTS)


def classify(asset_id: str | None, text: str) -> set[str]:
    """Labels for `text`: union of the asset's registry base labels and any
    content-rule hits. When the origin asset is unknown, content rules still
    apply; anything substantive that is not provably public reads as
    INTERNAL (spec §9 deterministic default).
    """
    labels: set[str] = set(base_labels(asset_id)) if asset_id else set()

    for label, pattern in _CONTENT_RULES:
        if pattern.search(text):
            labels.add(label)

    if not labels:
        labels.add(_LABEL_INTERNAL)  # unclassifiable substantive content
    if labels == {"PUBLIC"} and _contains_private_hint(text):
        labels.discard("PUBLIC")
        labels.add(_LABEL_INTERNAL)

    return labels
