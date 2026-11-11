"""Project: the persistent unit the human edits (inputs, Charter, Proof, Workbench). Each audit snapshots it into a packet."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Optional

from pydantic import Field

from shipproof.models import (
    MAX_COLLECTION,
    Charter,
    EntityId,
    FrozenModel,
    HttpUrlText,
    LongText,
    ProofCheck,
    ShortText,
    Severity,
    UncertaintyFlag,
    WorkbenchCheck,
)
from shipproof.prompts import GroundedRule
from shipproof.remediation import Remediation

MAX_LINKS = 20
MAX_RULE_PAGES = 10


class LinkTarget(FrozenModel):
    label: ShortText
    url: HttpUrlText


class Project(FrozenModel):
    projectId: EntityId
    repo: Annotated[str, Field(min_length=3, max_length=300)]
    proposalText: LongText
    links: Annotated[tuple[LinkTarget, ...], Field(max_length=MAX_LINKS)] = ()
    rulePages: Annotated[tuple[HttpUrlText, ...], Field(max_length=MAX_RULE_PAGES)] = ()
    emailQuery: Optional[Annotated[str, Field(min_length=1, max_length=500)]] = None
    charter: Charter = Charter()
    proofChecks: Annotated[tuple[ProofCheck, ...], Field(max_length=MAX_COLLECTION)] = ()
    workbenchChecks: Annotated[tuple[WorkbenchCheck, ...], Field(max_length=MAX_COLLECTION)] = ()
    packetIds: Annotated[tuple[EntityId, ...], Field(max_length=MAX_COLLECTION)] = ()


class PlannedTask(FrozenModel):
    tool: ShortText
    target: ShortText
    reason: ShortText


class AuditSummary(FrozenModel):
    """Agent working memory for one audit, kept beside the packet for the dashboard."""

    packetId: EntityId
    projectId: EntityId
    startedAt: datetime
    finishedAt: Optional[datetime] = None
    plan: tuple[PlannedTask, ...] = ()
    rules: tuple[GroundedRule, ...] = ()
    effectiveDeadline: Optional[datetime] = None
    confusionIndex: Annotated[int, Field(ge=0, le=100)] = 0
    confusionThreshold: Annotated[int, Field(ge=1, le=100)]
    halted: bool = False
    error: Optional[ShortText] = None
    remediations: tuple[Remediation, ...] = ()


SEVERITY_WEIGHT: dict[Severity, int] = {Severity.LOW: 1, Severity.MEDIUM: 3, Severity.HIGH: 6, Severity.CRITICAL: 10}


def computeConfusionIndex(flags: tuple[UncertaintyFlag, ...] | list[UncertaintyFlag]) -> int:
    """Structural signals only: weighted count of raised flags, capped at 100. Never uses model self-confidence."""
    return min(100, sum(SEVERITY_WEIGHT[flag.severity] for flag in flags))
