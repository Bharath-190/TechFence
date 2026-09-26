"""Task -> TaskContract builder (spec §7, §15; DECISIONS §8; invariant I7).

Pipeline: the local LLM (Qwen3 via Ollama, temperature 0) PROPOSES a JSON
contract; deterministic code VALIDATES it — values outside the catalog are
dropped, `external_transfer` is True only when the user's task text itself
names an external catalog destination — and a keyword fallback builder takes
over when Ollama is down or the JSON is invalid. Which path was used is
recorded (last_builder()).

Invariant I7: build_contract accepts ONLY the task string — never documents,
tool output or agent messages.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone

import httpx

from taskfence import catalog
from taskfence.models import TaskContract

_LAST_BUILDER = "fallback"

_PROMPT_TEMPLATE = """You are a security-contract extractor. Convert the user's task into JSON ONLY (no prose, no markdown) with exactly these keys:
{{"purpose": one of {purposes}, "allowed_data": subset of {data}, "allowed_destinations": subset of {destinations}, "allowed_actions": subset of {actions}, "external_transfer": boolean}}

Task: {task_text}"""


def _ollama_chat(task_text: str) -> str | None:
    """Ask the local model for a JSON proposal; None when unreachable."""
    url = os.environ.get("TASKFENCE_OLLAMA_URL",
                         "http://localhost:11434").rstrip("/")
    model = os.environ.get("TASKFENCE_MODEL", "qwen3")
    try:
        response = httpx.post(
            f"{url}/api/chat",
            json={"model": model, "stream": False,
                  "options": {"temperature": 0},
                  "messages": [{"role": "user", "content":
                                _PROMPT_TEMPLATE.format(
                                    purposes=sorted(catalog.PURPOSES),
                                    data=sorted(catalog.DATA_GROUPS),
                                    destinations=sorted(catalog.DESTINATIONS),
                                    actions=sorted(catalog.ACTIONS),
                                    task_text=task_text)}]},
            timeout=20)
        response.raise_for_status()
        return response.json()["message"]["content"]
    except (httpx.HTTPError, KeyError, ValueError):
        return None


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, dict) else None
        except ValueError:
            return None


def _intersect(items, vocab) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for item in items or []:
        if item in vocab and item not in seen:
            seen.add(item)
            kept.append(item)
    return kept


def _kw_hit(low: str, keyword: str) -> bool:
    """Word-boundary keyword match (prevents e.g. 'repo' matching inside
    'report'); non-word-initial keywords like '#sales' stay substrings."""
    if not keyword[:1].isalnum():
        return keyword in low
    return re.search(rf"\b{re.escape(keyword)}\b", low) is not None


_PURPOSE_KEYWORDS = [
    ("hr_analytics", ("salary", "employee", "payroll", "compensation",
                      "ctc", "hr")),
    ("code_review", ("code", "repo", "deploy", "review")),
    ("customer_support", ("customer", "support", "ticket", "churn")),
    ("market_research", ("market", "competitor", "research")),
    ("sales_reporting", ("sales", "report", "q3", "q2", "revenue")),
]
_GROUP_KEYWORDS = [
    ("employee_salary", ("salary", "employee", "payroll",
                         "compensation", "ctc")),
    ("customer_db", ("customer", "crm", "churn")),
    ("internal_strategy", ("strategy",)),
    ("source_code", ("code", "repo", "deploy")),
    ("public_info", ("public", "press")),
    ("meeting_notes", ("meeting", "notes")),
    ("sales_reports", ("sales", "report", "q3", "q2", "revenue")),
]
_DEST_KEYWORDS = [
    ("sales_slack", ("slack", "#sales", "channel")),
    ("hr_portal", ("hr portal", "hr_portal")),
    ("internal_wiki", ("wiki",)),
    ("repo", ("repo", "github", "git")),
]
_PURPOSE_DEFAULTS = {
    "sales_reporting": ("sales_reports", "sales_slack"),
    "customer_support": ("customer_db", "internal_wiki"),
    "hr_analytics": ("employee_salary", "hr_portal"),
    "code_review": ("source_code", "repo"),
    "market_research": ("public_info", "internal_wiki"),
}


def _fallback_contract(task_text: str) -> dict:
    """Deterministic keyword builder (DECISIONS §8 path 2)."""
    low = f" {task_text.lower()} "
    purpose = "sales_reporting"
    for name, keywords in _PURPOSE_KEYWORDS:
        if any(_kw_hit(low, k) for k in keywords):
            purpose = name
            break
    groups = [name for name, keywords in _GROUP_KEYWORDS
              if any(_kw_hit(low, k) for k in keywords)]
    destinations = [name for name, keywords in _DEST_KEYWORDS
                    if any(_kw_hit(low, k) for k in keywords)]
    if not groups or not destinations:
        default_group, default_dest = _PURPOSE_DEFAULTS[purpose]
        groups = groups or [default_group]
        destinations = destinations or [default_dest]
    external_named = [d for d in catalog.EXTERNAL_DESTINATIONS if d in low]
    destinations += external_named
    actions = ["read"]
    if "customer" in low or "crm" in low:
        actions.append("query")
    if any(k in low for k in ("summar", "report", "average", "aggregate")):
        actions.append("summarize")
    if any(k in low for k in ("average", "aggregate", "compute")):
        actions.append("derive")
    if destinations or "slack" in low or "send" in low:
        actions.append("send_message")
    if external_named:
        actions.append("post_external")
    if actions == ["read"]:
        actions.append("summarize")
    return {"purpose": purpose, "allowed_data": groups,
            "allowed_destinations": destinations, "allowed_actions": actions}


def build_contract_with_path(task_text: str) -> tuple[TaskContract, str]:
    try:
        proposal = _ollama_chat(task_text)
    except Exception:  # any LLM-path failure -> deterministic fallback
        proposal = None
    fields = None
    path = "llm"
    if proposal:
        data = _extract_json(proposal)
        if data and data.get("purpose") in catalog.PURPOSES:
            fields = {
                "purpose": data["purpose"],
                "allowed_data": _intersect(data.get("allowed_data"),
                                           catalog.DATA_GROUPS),
                "allowed_destinations": _intersect(
                    data.get("allowed_destinations"), catalog.DESTINATIONS),
                "allowed_actions": _intersect(data.get("allowed_actions"),
                                              catalog.ACTIONS),
            }
    if fields is None:
        path = "fallback"
        fields = _fallback_contract(task_text)

    low = task_text.lower()
    external_transfer = any(d in low for d in catalog.EXTERNAL_DESTINATIONS)
    contract_id = "c-" + hashlib.sha256(
        f"{fields['purpose']}|{task_text}".encode()).hexdigest()[:12]
    contract = TaskContract(
        contract_id=contract_id, purpose=fields["purpose"],
        allowed_data=fields["allowed_data"],
        allowed_destinations=fields["allowed_destinations"],
        allowed_actions=fields["allowed_actions"],
        external_transfer=external_transfer,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return contract, path


def build_contract(task_text: str) -> TaskContract:
    """Build the TaskContract from the user's task text ONLY (I7)."""
    global _LAST_BUILDER
    contract, _LAST_BUILDER = build_contract_with_path(task_text)
    return contract


def last_builder() -> str:
    """Which path built the last contract: 'llm' or 'fallback'."""
    return _LAST_BUILDER
