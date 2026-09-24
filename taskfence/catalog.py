"""Controlled vocabulary and label constants for TaskFence.

Single source for every value a TaskContract may contain. Values come from
specs/DECISIONS.md §5 (vocabulary) and §2 (restricted labels); spec §9 defines
the classifier labels. Constants only — no logic.
"""

# --- Purposes (DECISIONS §5) -------------------------------------------------
PURPOSES = frozenset({
    "sales_reporting",
    "customer_support",
    "hr_analytics",
    "code_review",
    "market_research",
})

# --- Data groups / assets (DECISIONS §5) -------------------------------------
DATA_GROUPS = frozenset({
    "sales_reports",
    "customer_db",
    "employee_salary",
    "internal_strategy",
    "public_info",
    "source_code",
    "meeting_notes",
})

# --- Destinations (DECISIONS §5) ----------------------------------------------
INTERNAL_DESTINATIONS = frozenset({
    "sales_slack",
    "hr_portal",
    "internal_wiki",
    "repo",
})
EXTERNAL_DESTINATIONS = frozenset({
    "external_api",
    "personal_email",
    "public_paste",
})
DESTINATIONS = INTERNAL_DESTINATIONS | EXTERNAL_DESTINATIONS

# --- Actions (DECISIONS §5) ---------------------------------------------------
ACTIONS = frozenset({
    "read",
    "summarize",
    "query",
    "derive",
    "send_message",
    "post_external",
})
# Actions whose whole point is moving data out of the boundary (DECISIONS §1).
OUTBOUND_ACTIONS = frozenset({"post_external"})

# --- Classifier labels (spec §9) ----------------------------------------------
LABELS = frozenset({
    "PUBLIC",
    "INTERNAL",
    "CONFIDENTIAL",
    "PII",
    "FINANCIAL",
    "SOURCE_CODE",
    "EMPLOYEE_DATA",
    "CUSTOMER_DATA",
})

# "Never leave the allowed destinations" labels (DECISIONS §2). CONFIDENTIAL is
# contextual and PUBLIC is unrestricted; restricted labels propagate through
# lineage to derived data (spec §13, invariant I6).
RESTRICTED_LABELS = frozenset({
    "PII",
    "EMPLOYEE_DATA",
    "CUSTOMER_DATA",
    "FINANCIAL",
    "SOURCE_CODE",
})

# --- Tools (DECISIONS §5 tool→action mapping; spec §14) -----------------------
TOOLS = frozenset({
    "read_file",
    "query_customer_db",
    "send_slack",
    "post_external",
})
TOOL_ACTIONS = {
    "read_file": "read",
    "query_customer_db": "query",
    "send_slack": "send_message",
    "post_external": "post_external",
}
