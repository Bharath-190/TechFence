"""Deterministic purpose-bound policy engine (spec §11, §12, §27; kit A2).

Invariants honored here: I2 (no LLM, no network, no file I/O, no clock —
imports are stdlib typing/enum + taskfence models/catalog only), I4 (default
deny: any unknown tool/source-group/destination/action BLOCKs).

Check order follows spec §27: source, action, destination, external-transfer,
lineage, purpose. ALL failed checks are collected, not just the first. The
hard/soft split from DECISIONS §1 lives in the constants below: a single hard
failure forces BLOCK; APPROVE requires zero hard failures and a destination
that is allowed and internal.

The request carries `source` (the display node id, e.g. an asset or derived
node) and `source_group` (the resolved controlled-vocabulary data group the
policy checks membership against). An empty/unknown source_group is a hard
violation: the gateway is the only layer allowed to resolve nodes to groups.
"""

from enum import Enum
from typing import List

from taskfence import catalog
from taskfence.models import Decision, FlowRequest, TaskContract

# DECISIONS §1 — hard (BLOCK) violation rules.
HARD_EXTERNAL_DESTINATION_WHEN_FORBIDDEN = "external_destination_forbidden"
HARD_RESTRICTED_LABEL_ESCAPES = "restricted_label_escape"
HARD_UNKNOWN_ENTITY = "unknown_entity"
HARD_OUTBOUND_ACTION_OUT_OF_SCOPE = "outbound_action_out_of_scope"

# DECISIONS §1 — soft (APPROVE-eligible) violation rules.
SOFT_SOURCE_OUTSIDE_CONTRACT = "source_outside_contract"
SOFT_ACTION_OUTSIDE_CONTRACT = "action_outside_contract"
SOFT_RESTRICTED_LABEL_REVIEW = "restricted_label_review"


class Check(str, Enum):
    SOURCE = "source"
    ACTION = "action"
    DESTINATION = "destination"
    EXTERNAL_TRANSFER = "external_transfer"
    LINEAGE = "lineage"
    PURPOSE = "purpose"


class PolicyEngine:
    def evaluate(self, contract: TaskContract, request: FlowRequest) -> Decision:
        hard: List[tuple] = []
        soft: List[tuple] = []

        # 1. Source check (spec §27 step 1).
        if request.source_group in catalog.DATA_GROUPS:
            if request.source_group not in contract.allowed_data:
                soft.append((Check.SOURCE, SOFT_SOURCE_OUTSIDE_CONTRACT,
                             f"Data group {request.source_group!r} is not in "
                             f"the task's allowed data."))
        else:
            hard.append((Check.SOURCE, HARD_UNKNOWN_ENTITY,
                         f"Source {request.source!r} is unknown; default deny."))

        # 2. Tool and action check (spec §27 step 2).
        if request.tool not in catalog.TOOLS:
            hard.append((Check.ACTION, HARD_UNKNOWN_ENTITY,
                         f"Tool {request.tool!r} is unknown; default deny."))
        elif request.action not in catalog.ACTIONS:
            hard.append((Check.ACTION, HARD_UNKNOWN_ENTITY,
                         f"Action {request.action!r} is unknown; default deny."))
        elif request.action not in contract.allowed_actions:
            if request.action in catalog.OUTBOUND_ACTIONS:
                hard.append((Check.ACTION, HARD_OUTBOUND_ACTION_OUT_OF_SCOPE,
                             f"Outbound action {request.action!r} is not "
                             f"authorized by the task."))
            else:
                soft.append((Check.ACTION, SOFT_ACTION_OUTSIDE_CONTRACT,
                             f"Action {request.action!r} is not in the task's "
                             f"allowed actions."))

        # 3. Destination check (spec §27 step 3), incl. external-transfer
        # policy (spec §27 step 4) when the destination itself is allowed.
        if request.destination not in catalog.DESTINATIONS:
            hard.append((Check.DESTINATION, HARD_UNKNOWN_ENTITY,
                         f"Destination {request.destination!r} is unknown; "
                         f"default deny."))
        elif request.destination not in contract.allowed_destinations:
            if request.destination in catalog.EXTERNAL_DESTINATIONS:
                hard.append(
                    (Check.DESTINATION, HARD_EXTERNAL_DESTINATION_WHEN_FORBIDDEN,
                     f"Destination {request.destination!r} is external and "
                     f"not allowed by the task."))
            else:
                hard.append(
                    (Check.DESTINATION, HARD_EXTERNAL_DESTINATION_WHEN_FORBIDDEN,
                     f"Destination {request.destination!r} is not in the "
                     f"task's allowed destinations."))
        elif (request.destination in catalog.EXTERNAL_DESTINATIONS
              and not contract.external_transfer):
            hard.append(
                (Check.EXTERNAL_TRANSFER, HARD_EXTERNAL_DESTINATION_WHEN_FORBIDDEN,
                 "External transfer is disabled for this task."))

        # 4. Lineage check (spec §27 step 5): labels already include
        # lineage-inherited labels. Restricted data needs human eyes: hard
        # BLOCK when the destination is not an allowed internal one, APPROVE
        # otherwise (DECISIONS §1).
        restricted = request.labels & catalog.RESTRICTED_LABELS
        if restricted:
            if (request.destination not in contract.allowed_destinations
                    or request.destination in catalog.EXTERNAL_DESTINATIONS):
                hard.append(
                    (Check.LINEAGE, HARD_RESTRICTED_LABEL_ESCAPES,
                     f"Restricted data ({', '.join(sorted(restricted))}) "
                     f"cannot go to {request.destination!r}."))
            else:
                soft.append(
                    (Check.LINEAGE, SOFT_RESTRICTED_LABEL_REVIEW,
                     f"Restricted data ({', '.join(sorted(restricted))}) "
                     f"requires human review before it moves to "
                     f"{request.destination!r}."))

        # 5. Purpose check (spec §27 step 6): an external destination is never
        # consistent with a purpose that did not authorize one.
        if (request.destination in catalog.EXTERNAL_DESTINATIONS
                and not contract.external_transfer):
            hard.append((Check.PURPOSE, HARD_EXTERNAL_DESTINATION_WHEN_FORBIDDEN,
                         "This movement is not justified by the task purpose."))

        reasons = [text for _, _, text in hard + soft]
        failed = [name for _, name, _ in hard + soft]

        if hard:
            outcome = "BLOCK"
        elif soft:
            outcome = "APPROVE"
        else:
            outcome = "ALLOW"

        return Decision(
            outcome=outcome,
            reasons=reasons,
            failed_checks=sorted(set(failed)),
            contract_version=contract.version,
        )
