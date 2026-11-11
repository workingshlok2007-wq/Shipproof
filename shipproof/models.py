"""ShipProof Phase 1: domain models and invariants (Pydantic v2).

All models are frozen. State changes go through pure functions that return new
instances, so no shared mutable state exists between the agent loop and the API.
All text fields hold untrusted data and are never interpreted as instructions.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import Annotated, Optional
from urllib.parse import urlparse

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

MAX_TIER = 16
MAX_SHORT_TEXT = 512
MAX_LONG_TEXT = 20000
MAX_URL_LENGTH = 2048
MAX_COLLECTION = 1000

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _rejectControlChars(value: str) -> str:
    if _CONTROL_CHARS.search(value):
        raise ValueError("text contains control characters")
    return value


def _validateHttpUrl(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("url must be absolute http or https")
    if any(ch.isspace() for ch in value):
        raise ValueError("url must not contain whitespace")
    return value


EntityId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")]
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_SHORT_TEXT),
    AfterValidator(_rejectControlChars),
]
LongText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_LONG_TEXT),
    AfterValidator(_rejectControlChars),
]
HttpUrlText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_URL_LENGTH),
    AfterValidator(_validateHttpUrl),
]


def utcNow() -> datetime:
    return datetime.now(timezone.utc)


class ModelError(ValueError):
    """Raised when a domain invariant is violated by a state-change function."""


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)


# ---------------------------------------------------------------- enums


class TrustTier(IntEnum):
    CHARTER = 1
    PROOF = 2
    WORKBENCH = 3


class Role(str, Enum):
    OWNER = "OWNER"
    SENIOR = "SENIOR"
    MID = "MID"
    JUNIOR = "JUNIOR"
    INTERN = "INTERN"


ROLE_RANK: dict[Role, int] = {
    Role.OWNER: 5,
    Role.SENIOR: 4,
    Role.MID: 3,
    Role.JUNIOR: 2,
    Role.INTERN: 1,
}


class ActorKind(str, Enum):
    HUMAN = "HUMAN"
    AGENT = "AGENT"


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    STALE = "STALE"
    UNVERIFIED = "UNVERIFIED"


class ApprovalState(str, Enum):
    DRAFT = "DRAFT"
    AUDITING = "AUDITING"
    PENDING_HUMAN_APPROVAL = "PENDING_HUMAN_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class SourceStatus(str, Enum):
    CHECKED = "CHECKED"
    UNREACHABLE = "UNREACHABLE"
    NOT_CHECKED = "NOT_CHECKED"


class SourceKind(str, Enum):
    EVENT_PAGE = "EVENT_PAGE"
    EMAIL = "EMAIL"
    REPOSITORY = "REPOSITORY"
    PROPOSAL = "PROPOSAL"
    DOCUMENT = "DOCUMENT"
    MANUAL_ENTRY = "MANUAL_ENTRY"


class SourceRefKind(str, Enum):
    URL = "URL"
    EMAIL_MESSAGE_ID = "EMAIL_MESSAGE_ID"


class UncertaintyKind(str, Enum):
    CONFLICTING_SOURCES = "CONFLICTING_SOURCES"
    UNDATED_RULE = "UNDATED_RULE"
    FAILED_RENDER = "FAILED_RENDER"
    AUTH_REDIRECT = "AUTH_REDIRECT"
    UNREACHABLE_SOURCE = "UNREACHABLE_SOURCE"
    MISSING_SOURCE_CATEGORY = "MISSING_SOURCE_CATEGORY"
    PROMPT_INJECTION_SUSPECTED = "PROMPT_INJECTION_SUSPECTED"
    UNGROUNDED_MODEL_OUTPUT = "UNGROUNDED_MODEL_OUTPUT"
    ANALYSIS_FAILED = "ANALYSIS_FAILED"
    CONFUSION_THRESHOLD_EXCEEDED = "CONFUSION_THRESHOLD_EXCEEDED"


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class EvidenceMethod(str, Enum):
    HEADLESS_BROWSER = "HEADLESS_BROWSER"
    HTTP_CLIENT = "HTTP_CLIENT"
    GITHUB_API = "GITHUB_API"


class EvidenceBasis(str, Enum):
    DOCUMENTATION = "DOCUMENTATION"
    COMMIT_HISTORY = "COMMIT_HISTORY"
    CODE_EXISTENCE = "CODE_EXISTENCE"


# ---------------------------------------------------------------- actors and sources


class Actor(FrozenModel):
    actorId: EntityId
    kind: ActorKind
    role: Optional[Role] = None

    @model_validator(mode="after")
    def _roleMatchesKind(self) -> Actor:
        if self.kind is ActorKind.HUMAN and self.role is None:
            raise ValueError("human actor requires an assigned role")
        if self.kind is ActorKind.AGENT and self.role is not None:
            raise ValueError("agent actor cannot hold a role")
        return self

    @property
    def isHuman(self) -> bool:
        return self.kind is ActorKind.HUMAN


AGENT_ACTOR = Actor(actorId="shipproofAgent", kind=ActorKind.AGENT)


class SourceRef(FrozenModel):
    kind: SourceRefKind
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_URL_LENGTH)]

    @model_validator(mode="after")
    def _validateValue(self) -> SourceRef:
        _rejectControlChars(self.value)
        if self.kind is SourceRefKind.URL:
            _validateHttpUrl(self.value)
        elif any(ch.isspace() for ch in self.value):
            raise ValueError("email message id must not contain whitespace")
        return self


class SourceRecord(FrozenModel):
    sourceId: EntityId
    kind: SourceKind
    location: ShortText
    status: SourceStatus
    sourceDate: Optional[AwareDatetime] = None
    retrievedAt: Optional[AwareDatetime] = None
    contentSha256: Optional[Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]] = None
    detail: Optional[ShortText] = None

    @model_validator(mode="after")
    def _checkedHasRetrieval(self) -> SourceRecord:
        if self.status is SourceStatus.CHECKED and (self.retrievedAt is None or self.contentSha256 is None):
            raise ValueError("CHECKED source requires retrievedAt and contentSha256")
        if self.status is SourceStatus.NOT_CHECKED and self.retrievedAt is not None:
            raise ValueError("NOT_CHECKED source cannot have retrievedAt")
        return self


# ---------------------------------------------------------------- tier 1: charter


class CharterClause(FrozenModel):
    clauseId: EntityId
    version: Annotated[int, Field(ge=1)]
    text: LongText
    sourceRef: SourceRef
    effectiveAt: AwareDatetime


class CharterAmendment(FrozenModel):
    amendmentId: EntityId
    clauseId: EntityId
    newText: LongText
    sourceRef: SourceRef
    submittedBy: Actor
    submittedAt: AwareDatetime

    @model_validator(mode="after")
    def _submitterIsOwner(self) -> CharterAmendment:
        if not self.submittedBy.isHuman or self.submittedBy.role is not Role.OWNER:
            raise ValueError("amendments may only be submitted by a human OWNER")
        return self


class Charter(FrozenModel):
    """Append-only. clauses holds every version of every clause; nothing is edited in place."""

    clauses: Annotated[tuple[CharterClause, ...], Field(max_length=MAX_COLLECTION)] = ()
    amendments: Annotated[tuple[CharterAmendment, ...], Field(max_length=MAX_COLLECTION)] = ()

    @model_validator(mode="after")
    def _versionsContiguous(self) -> Charter:
        seen: dict[str, int] = {}
        for clause in self.clauses:
            expected = seen.get(clause.clauseId, 0) + 1
            if clause.version != expected:
                raise ValueError(f"clause {clause.clauseId} version {clause.version} out of order, expected {expected}")
            seen[clause.clauseId] = clause.version
        amendmentIds = [a.amendmentId for a in self.amendments]
        if len(amendmentIds) != len(set(amendmentIds)):
            raise ValueError("duplicate amendmentId")
        for amendment in self.amendments:
            if amendment.clauseId not in seen:
                raise ValueError(f"amendment {amendment.amendmentId} targets unknown clause {amendment.clauseId}")
        return self

    def currentClause(self, clauseId: str) -> CharterClause:
        latest: Optional[CharterClause] = None
        for clause in self.clauses:
            if clause.clauseId == clauseId:
                latest = clause
        if latest is None:
            raise ModelError(f"unknown clause {clauseId}")
        return latest

    def currentVersions(self) -> dict[str, int]:
        return {c.clauseId: c.version for c in self.clauses}


def addCharterClause(charter: Charter, clauseId: str, text: str, sourceRef: SourceRef, actor: Actor) -> Charter:
    """Initial clause creation (version 1). Existing clauses change only via applyAmendment."""
    if not actor.isHuman or actor.role is not Role.OWNER:
        raise ModelError("charter clauses may only be created by a human OWNER")
    if clauseId in charter.currentVersions():
        raise ModelError(f"clause {clauseId} exists; use an amendment")
    newClause = CharterClause(clauseId=clauseId, version=1, text=text, sourceRef=sourceRef, effectiveAt=utcNow())
    return Charter(clauses=charter.clauses + (newClause,), amendments=charter.amendments)


# ---------------------------------------------------------------- tier 2: proof


class ProofCheck(FrozenModel):
    """A user's interpretation of a Charter clause. Text-vs-text: FAIL means 'possible misreading', never 'verified'."""

    checkId: EntityId
    clauseId: EntityId
    clauseVersion: Annotated[int, Field(ge=1)]
    interpretation: LongText
    status: CheckStatus = CheckStatus.UNVERIFIED
    authoredBy: Actor
    promotedBy: Optional[Actor] = None
    promotedAt: Optional[AwareDatetime] = None

    @model_validator(mode="after")
    def _promotionConsistent(self) -> ProofCheck:
        if (self.promotedBy is None) != (self.promotedAt is None):
            raise ValueError("promotedBy and promotedAt must be set together")
        if self.promotedBy is not None:
            if not self.promotedBy.isHuman or not self.authoredBy.isHuman:
                raise ValueError("promotion requires human author and human promoter")
            ownerSelfPromotion = self.promotedBy.role is Role.OWNER and self.authoredBy.role is Role.OWNER
            if not ownerSelfPromotion and ROLE_RANK[self.promotedBy.role] <= ROLE_RANK[self.authoredBy.role]:  # type: ignore[index]
                raise ValueError("promoter must hold strictly higher access than author (OWNER may self-promote)")
        return self

    @property
    def isPromoted(self) -> bool:
        return self.promotedBy is not None


def applyAmendment(
    charter: Charter, proofChecks: tuple[ProofCheck, ...], amendment: CharterAmendment
) -> tuple[Charter, tuple[ProofCheck, ...]]:
    """Appends a new clause version and sets every dependent ProofCheck to STALE. Unrelated checks are untouched."""
    if any(a.amendmentId == amendment.amendmentId for a in charter.amendments):
        raise ModelError(f"amendment {amendment.amendmentId} already applied")
    previous = charter.currentClause(amendment.clauseId)
    newClause = CharterClause(
        clauseId=previous.clauseId,
        version=previous.version + 1,
        text=amendment.newText,
        sourceRef=amendment.sourceRef,
        effectiveAt=amendment.submittedAt,
    )
    newCharter = Charter(clauses=charter.clauses + (newClause,), amendments=charter.amendments + (amendment,))
    updatedChecks = tuple(
        check.model_copy(update={"status": CheckStatus.STALE}) if check.clauseId == amendment.clauseId else check
        for check in proofChecks
    )
    return newCharter, updatedChecks


def reconfirmProofCheck(check: ProofCheck, charter: Charter, actor: Actor, interpretation: str) -> ProofCheck:
    """Re-binds a STALE check to the current clause version. Result is UNVERIFIED and unpromoted; it must be re-audited."""
    if not actor.isHuman:
        raise ModelError("only a human may reconfirm a proof check")
    if check.status is not CheckStatus.STALE:
        raise ModelError("only STALE checks can be reconfirmed")
    current = charter.currentClause(check.clauseId)
    return ProofCheck(
        checkId=check.checkId,
        clauseId=check.clauseId,
        clauseVersion=current.version,
        interpretation=interpretation,
        status=CheckStatus.UNVERIFIED,
        authoredBy=actor,
    )


def promoteProofCheck(check: ProofCheck, charter: Charter, promoter: Actor) -> ProofCheck:
    if check.isPromoted:
        raise ModelError("check already promoted")
    if check.status is not CheckStatus.PASS:
        raise ModelError("only PASS checks can be promoted")
    if check.clauseVersion != charter.currentClause(check.clauseId).version:
        raise ModelError("check is bound to an outdated clause version")
    return ProofCheck(**{**check.model_dump(), "promotedBy": promoter, "promotedAt": utcNow()})


# ---------------------------------------------------------------- tier 3+: workbench


class WorkbenchCheck(FrozenModel):
    """Disposable and non-authoritative. Never gates approval."""

    checkId: EntityId
    tier: Annotated[int, Field(ge=int(TrustTier.WORKBENCH), le=MAX_TIER)]
    label: ShortText
    description: Optional[LongText] = None
    status: CheckStatus = CheckStatus.UNVERIFIED
    authoredBy: Actor


# ---------------------------------------------------------------- uncertainty and coverage


class UncertaintyFlag(FrozenModel):
    flagId: EntityId
    kind: UncertaintyKind
    detail: ShortText
    severity: Severity
    sourceIds: Annotated[tuple[EntityId, ...], Field(max_length=MAX_COLLECTION)] = ()
    raisedAt: AwareDatetime


class CoverageLedger(FrozenModel):
    """The agent never asserts completeness. Only a human confirms 'this is everything'."""

    sources: Annotated[tuple[SourceRecord, ...], Field(max_length=MAX_COLLECTION)] = ()
    humanConfirmedComplete: bool = False
    confirmedBy: Optional[Actor] = None
    confirmedAt: Optional[AwareDatetime] = None

    @model_validator(mode="after")
    def _confirmationConsistent(self) -> CoverageLedger:
        ids = [s.sourceId for s in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate sourceId")
        if self.humanConfirmedComplete:
            if self.confirmedBy is None or self.confirmedAt is None:
                raise ValueError("confirmation requires confirmedBy and confirmedAt")
            if not self.confirmedBy.isHuman:
                raise ValueError("only a human may confirm coverage")
            if not self.sources:
                raise ValueError("cannot confirm an empty ledger")
        elif self.confirmedBy is not None or self.confirmedAt is not None:
            raise ValueError("confirmedBy/confirmedAt set without confirmation")
        return self


def upsertSource(ledger: CoverageLedger, source: SourceRecord) -> CoverageLedger:
    """Any change to the source list invalidates a prior human confirmation."""
    remaining = tuple(s for s in ledger.sources if s.sourceId != source.sourceId)
    return CoverageLedger(sources=remaining + (source,))


def confirmCoverage(ledger: CoverageLedger, actor: Actor) -> CoverageLedger:
    if not actor.isHuman:
        raise ModelError("only a human may confirm coverage")
    return CoverageLedger(sources=ledger.sources, humanConfirmedComplete=True, confirmedBy=actor, confirmedAt=utcNow())


# ---------------------------------------------------------------- evidence and alignment


class EvidenceItem(FrozenModel):
    evidenceId: EntityId
    url: HttpUrlText
    method: EvidenceMethod
    finalStatus: Optional[Annotated[int, Field(ge=100, le=599)]] = None
    finalUrl: Optional[HttpUrlText] = None
    authWallDetected: bool
    status: CheckStatus
    checkedAt: AwareDatetime
    detail: Optional[ShortText] = None

    @model_validator(mode="after")
    def _passRequiresJudgeVantage(self) -> EvidenceItem:
        if self.status is CheckStatus.STALE:
            raise ValueError("evidence items cannot be STALE")
        if self.status is CheckStatus.PASS:
            if self.authWallDetected:
                raise ValueError("auth wall detected: cannot PASS")
            if self.finalStatus is None or not 200 <= self.finalStatus <= 299:
                raise ValueError("PASS requires a 2xx final status")
            if self.method is EvidenceMethod.HTTP_CLIENT:
                raise ValueError("raw HTTP client result cannot PASS; requires unauthenticated headless render")
        return self


class AlignmentReport(FrozenModel):
    alignmentScore: Annotated[int, Field(ge=0, le=100)]
    missingFeatures: Annotated[tuple[ShortText, ...], Field(max_length=MAX_COLLECTION)] = ()
    evidenceBasis: Annotated[frozenset[EvidenceBasis], Field(min_length=1)]
    caveat: ShortText
    generatedAt: AwareDatetime


# ---------------------------------------------------------------- submission packet and state machine

ALLOWED_TRANSITIONS: dict[ApprovalState, frozenset[ApprovalState]] = {
    ApprovalState.DRAFT: frozenset({ApprovalState.AUDITING}),
    ApprovalState.AUDITING: frozenset({ApprovalState.PENDING_HUMAN_APPROVAL}),
    ApprovalState.PENDING_HUMAN_APPROVAL: frozenset({ApprovalState.APPROVED, ApprovalState.REJECTED}),
    ApprovalState.APPROVED: frozenset(),
    ApprovalState.REJECTED: frozenset(),
}
HUMAN_ONLY_STATES = frozenset({ApprovalState.APPROVED, ApprovalState.REJECTED})


class SubmissionPacket(FrozenModel):
    packetId: EntityId
    approvalState: ApprovalState = ApprovalState.DRAFT
    charter: Charter = Charter()
    proofChecks: Annotated[tuple[ProofCheck, ...], Field(max_length=MAX_COLLECTION)] = ()
    workbenchChecks: Annotated[tuple[WorkbenchCheck, ...], Field(max_length=MAX_COLLECTION)] = ()
    ledger: CoverageLedger = CoverageLedger()
    evidence: Annotated[tuple[EvidenceItem, ...], Field(max_length=MAX_COLLECTION)] = ()
    uncertaintyFlags: Annotated[tuple[UncertaintyFlag, ...], Field(max_length=MAX_COLLECTION)] = ()
    reports: Annotated[tuple[AlignmentReport, ...], Field(max_length=MAX_COLLECTION)] = ()
    decidedBy: Optional[Actor] = None
    decidedAt: Optional[AwareDatetime] = None

    @model_validator(mode="after")
    def _invariants(self) -> SubmissionPacket:
        versions = self.charter.currentVersions()
        for check in self.proofChecks:
            current = versions.get(check.clauseId)
            if current is None:
                raise ValueError(f"proof check {check.checkId} references unknown clause {check.clauseId}")
            if check.clauseVersion > current:
                raise ValueError(f"proof check {check.checkId} references future clause version")
            if check.clauseVersion < current and check.status is not CheckStatus.STALE:
                raise ValueError(f"proof check {check.checkId} is bound to an old clause version and must be STALE")
        for idField, items in (("checkId", self.proofChecks), ("checkId", self.workbenchChecks),
                               ("evidenceId", self.evidence), ("flagId", self.uncertaintyFlags)):
            ids = [getattr(item, idField) for item in items]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {idField} in packet collection")

        decided = self.approvalState in HUMAN_ONLY_STATES
        if decided:
            if self.decidedBy is None or self.decidedAt is None or not self.decidedBy.isHuman:
                raise ValueError(f"{self.approvalState.value} requires a human decidedBy and decidedAt")
        elif self.decidedBy is not None or self.decidedAt is not None:
            raise ValueError("decidedBy/decidedAt set before a decision")
        if self.approvalState is ApprovalState.APPROVED:
            if not self.ledger.humanConfirmedComplete:
                raise ValueError("APPROVED requires human-confirmed coverage ledger")
            if any(c.status is CheckStatus.STALE for c in self.proofChecks):
                raise ValueError("APPROVED blocked: STALE proof checks require reconfirmation")
            if any(c.status is CheckStatus.FAIL for c in self.gatingChecks):
                raise ValueError("APPROVED blocked: promoted proof check has FAIL status")
            if any(f.severity is Severity.CRITICAL for f in self.uncertaintyFlags):
                raise ValueError("APPROVED blocked: open CRITICAL uncertainty flag")
        return self

    @property
    def gatingChecks(self) -> tuple[ProofCheck, ...]:
        """Only promoted Proof checks gate approval. Workbench checks never do."""
        return tuple(c for c in self.proofChecks if c.isPromoted)


def transitionPacket(packet: SubmissionPacket, target: ApprovalState, actor: Actor) -> SubmissionPacket:
    if target not in ALLOWED_TRANSITIONS[packet.approvalState]:
        raise ModelError(f"illegal transition {packet.approvalState.value} -> {target.value}")
    if target in HUMAN_ONLY_STATES:
        if not actor.isHuman:
            raise ModelError(f"only a human actor may set {target.value}")
        return packet.model_validate({**packet.model_dump(), "approvalState": target, "decidedBy": actor, "decidedAt": utcNow()})
    return packet.model_validate({**packet.model_dump(), "approvalState": target})
