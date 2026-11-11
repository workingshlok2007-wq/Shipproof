"""Agent memory across audits: human decisions, accepted risks, questions, agent steps, and audit-to-audit diffs."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Optional

from pydantic import Field

from shipproof.models import (
    Actor,
    CheckStatus,
    EntityId,
    EvidenceItem,
    FrozenModel,
    LongText,
    ShortText,
    SubmissionPacket,
    UncertaintyFlag,
    UncertaintyKind,
)

MAX_MEMORY_ITEMS = 200
MAX_OPTIONS = 6
HexKey = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _fingerprint(*parts: str) -> str:
    normalised = "\x1f".join(re.sub(r"\s+", " ", p).strip().casefold() for p in parts)
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def flagKey(kind: UncertaintyKind, detail: str) -> str:
    """Same problem in a later audit produces the same key, so accepted risks and diffs survive re-runs."""
    return _fingerprint(kind.value, re.sub(r"\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:\d{2})?", "", detail))


class StepOrigin(str, Enum):
    BASELINE = "BASELINE"
    AGENT = "AGENT"
    FOLLOW_UP = "FOLLOW_UP"
    MEMORY = "MEMORY"


class AgentStep(FrozenModel):
    stepId: Annotated[int, Field(ge=1)]
    origin: StepOrigin
    tool: ShortText
    target: ShortText
    reason: LongText
    outcome: Optional[LongText] = None


class QuestionKind(str, Enum):
    DEADLINE_CHOICE = "DEADLINE_CHOICE"


UNSURE_VALUE = "unsure"


class QuestionOption(FrozenModel):
    value: Annotated[str, Field(min_length=1, max_length=64)]
    label: LongText


class AgentQuestion(FrozenModel):
    questionId: EntityId
    questionKey: HexKey
    kind: QuestionKind
    prompt: LongText
    options: Annotated[tuple[QuestionOption, ...], Field(min_length=2, max_length=MAX_OPTIONS + 1)]
    askedAt: datetime
    answer: Optional[QuestionOption] = None
    answeredBy: Optional[Actor] = None
    answeredFromMemory: bool = False

    def option(self, value: str) -> QuestionOption:
        for option in self.options:
            if option.value == value:
                return option
        raise ValueError(f"answer must be one of: {', '.join(o.value for o in self.options)}")


class HumanDecision(FrozenModel):
    questionKey: HexKey
    question: LongText
    answer: QuestionOption
    decidedBy: Actor
    decidedAt: datetime


class AcceptedRisk(FrozenModel):
    flagKey: HexKey
    kind: UncertaintyKind
    detail: LongText
    reason: ShortText
    acceptedBy: Actor
    acceptedAt: datetime


class ProjectMemory(FrozenModel):
    decisions: Annotated[tuple[HumanDecision, ...], Field(max_length=MAX_MEMORY_ITEMS)] = ()
    acceptedRisks: Annotated[tuple[AcceptedRisk, ...], Field(max_length=MAX_MEMORY_ITEMS)] = ()

    def decisionFor(self, questionKey: str) -> Optional[HumanDecision]:
        for decision in reversed(self.decisions):
            if decision.questionKey == questionKey:
                return decision
        return None

    def acceptedRiskFor(self, flag: UncertaintyFlag) -> Optional[AcceptedRisk]:
        key = flagKey(flag.kind, flag.detail)
        for risk in reversed(self.acceptedRisks):
            if risk.flagKey == key:
                return risk
        return None

    def withDecision(self, decision: HumanDecision) -> ProjectMemory:
        kept = tuple(d for d in self.decisions if d.questionKey != decision.questionKey)
        return self.model_copy(update={"decisions": (kept + (decision,))[-MAX_MEMORY_ITEMS:]})

    def withAcceptedRisk(self, risk: AcceptedRisk) -> ProjectMemory:
        kept = tuple(r for r in self.acceptedRisks if r.flagKey != risk.flagKey)
        return self.model_copy(update={"acceptedRisks": (kept + (risk,))[-MAX_MEMORY_ITEMS:]})


class AuditDiff(FrozenModel):
    previousPacketId: EntityId
    newFlags: tuple[LongText, ...] = ()
    resolvedFlags: tuple[LongText, ...] = ()
    fixedEvidence: tuple[ShortText, ...] = ()
    regressedEvidence: tuple[ShortText, ...] = ()
    stillFailing: tuple[ShortText, ...] = ()


def _evidenceKey(item: EvidenceItem) -> str:
    return f"{item.evidenceId}|{item.url}"


def computeDiff(previous: Optional[SubmissionPacket], flags: list[UncertaintyFlag], evidence: list[EvidenceItem]) -> Optional[AuditDiff]:
    if previous is None:
        return None
    before = {flagKey(f.kind, f.detail): f.detail for f in previous.uncertaintyFlags}
    now = {flagKey(f.kind, f.detail): f.detail for f in flags}
    previousEvidence = {_evidenceKey(e): e for e in previous.evidence}
    fixed, regressed, still = [], [], []
    for item in evidence:
        old = previousEvidence.get(_evidenceKey(item))
        label = f"{item.evidenceId} ({item.url})"[:500]
        if old is None:
            continue
        if old.status is not CheckStatus.PASS and item.status is CheckStatus.PASS:
            fixed.append(label)
        elif old.status is CheckStatus.PASS and item.status is not CheckStatus.PASS:
            regressed.append(label)
        elif item.status is not CheckStatus.PASS:
            still.append(label)
    return AuditDiff(
        previousPacketId=previous.packetId,
        newFlags=tuple(detail for key, detail in now.items() if key not in before)[:50],
        resolvedFlags=tuple(detail for key, detail in before.items() if key not in now)[:50],
        fixedEvidence=tuple(fixed)[:50], regressedEvidence=tuple(regressed)[:50], stillFailing=tuple(still)[:50],
    )
