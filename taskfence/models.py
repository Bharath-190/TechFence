"""Pydantic v2 models for TaskFence (spec §5, §8, §28; kit Prompt A1).

TaskContract is frozen (invariant I3): scope changes create a NEW contract
version, never a mutation. Its `version` is a deterministic content hash of
every field, so identical fields always yield an identical version and any
field change yields a different one. `created_at` is supplied by the caller —
models contain no clock (kit guardrail 9).

FlowRequest is deliberately NOT vocabulary-validated: it comes from the agent
side, and unknown tool/source/destination/action values must be representable
so the policy engine can default-deny them (invariant I4).
"""

import hashlib
import json
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, computed_field, field_validator

from taskfence import catalog


def _content_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TaskContract(BaseModel):
    model_config = ConfigDict(frozen=True)

    contract_id: str
    parent_contract_id: Optional[str] = None
    purpose: str
    allowed_data: list[str] = []
    allowed_destinations: list[str] = []
    allowed_actions: list[str] = []
    external_transfer: bool = False
    created_at: str

    @field_validator("purpose")
    @classmethod
    def _purpose_in_catalog(cls, v: str) -> str:
        if v not in catalog.PURPOSES:
            raise ValueError(f"unknown purpose: {v!r}")
        return v

    @field_validator("allowed_data")
    @classmethod
    def _data_groups_in_catalog(cls, v: list[str]) -> list[str]:
        unknown = [x for x in v if x not in catalog.DATA_GROUPS]
        if unknown:
            raise ValueError(f"unknown data groups: {unknown}")
        return v

    @field_validator("allowed_destinations")
    @classmethod
    def _destinations_in_catalog(cls, v: list[str]) -> list[str]:
        unknown = [x for x in v if x not in catalog.DESTINATIONS]
        if unknown:
            raise ValueError(f"unknown destinations: {unknown}")
        return v

    @field_validator("allowed_actions")
    @classmethod
    def _actions_in_catalog(cls, v: list[str]) -> list[str]:
        unknown = [x for x in v if x not in catalog.ACTIONS]
        if unknown:
            raise ValueError(f"unknown actions: {unknown}")
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def version(self) -> str:
        """Deterministic content hash over every contract field (included
        in model_dump so every serialized contract carries its version)."""
        payload = {
            "contract_id": self.contract_id,
            "parent_contract_id": self.parent_contract_id,
            "purpose": self.purpose,
            "allowed_data": sorted(self.allowed_data),
            "allowed_destinations": sorted(self.allowed_destinations),
            "allowed_actions": sorted(self.allowed_actions),
            "external_transfer": self.external_transfer,
            "created_at": self.created_at,
        }
        return _content_hash(payload)


class DataAsset(BaseModel):
    """A registered data asset (spec §28)."""

    model_config = ConfigDict(frozen=True)

    id: str
    source: str
    classification: str
    data_group: str = ""

    @field_validator("classification")
    @classmethod
    def _label_known(cls, v: str) -> str:
        if v not in catalog.LABELS:
            raise ValueError(f"unknown label: {v!r}")
        return v

    @field_validator("data_group")
    @classmethod
    def _group_known(cls, v: str) -> str:
        if v and v not in catalog.DATA_GROUPS:
            raise ValueError(f"unknown data group: {v!r}")
        return v


class FlowRequest(BaseModel):
    """A tool/data request intercepted at the gateway (spec §14, §27).

    Not vocabulary-validated on purpose: unknown values must reach the policy
    engine so it can BLOCK them (invariant I4, kit Prompt A2 tests).
    `source` is the display node id (asset or derived node); `source_group` is
    the gateway-resolved controlled-vocabulary data group the policy engine
    checks membership against — empty means unresolvable and is default-denied.
    `labels` is the effective label set for the payload, including
    lineage-inherited labels; the policy engine never computes lineage itself.
    """

    model_config = ConfigDict(frozen=True)

    tool: str
    action: str
    source: str
    source_group: str = ""
    # Outbound flows may combine several source groups (declared inputs UNION
    # session reads under conservative lineage, DECISIONS §4).
    source_groups: frozenset[str] = frozenset()
    destination: str
    labels: frozenset[str] = frozenset()
    transformation: str = ""
    payload: str = ""


class Decision(BaseModel):
    """Outcome of a policy evaluation (spec §11)."""

    model_config = ConfigDict(frozen=True)

    outcome: Literal["ALLOW", "APPROVE", "BLOCK"]
    reasons: list[str]
    failed_checks: list[str]
    contract_version: str


class FlowEvent(BaseModel):
    """Recorded data flow (spec §28; audit layer fills timestamp)."""

    model_config = ConfigDict(frozen=True)

    source: str
    transformation: str
    destination: str
    action: str
    decision: Literal["ALLOW", "APPROVE", "BLOCK"]
    reason: str
    task_id: str = ""
    labels: frozenset[str] = frozenset()
    contract_version: str = ""
    lineage_path: str = ""
    timestamp: str = ""
