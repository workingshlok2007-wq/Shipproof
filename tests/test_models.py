from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from shipproof.models import (
    Actor,
    ActorKind,
    AlignmentReport,
    ApprovalState,
    Charter,
    CharterAmendment,
    CheckStatus,
    CoverageLedger,
    EvidenceBasis,
    EvidenceItem,
    EvidenceMethod,
    ModelError,
    ProofCheck,
    Role,
    SourceKind,
    SourceRecord,
    SourceRef,
    SourceRefKind,
    SourceStatus,
    SubmissionPacket,
    WorkbenchCheck,
    addCharterClause,
    applyAmendment,
    confirmCoverage,
    promoteProofCheck,
    reconfirmProofCheck,
    transitionPacket,
    upsertSource,
)

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)
OWNER = Actor(actorId="owner", kind=ActorKind.HUMAN, role=Role.OWNER)
JUNIOR = Actor(actorId="junior", kind=ActorKind.HUMAN, role=Role.JUNIOR)
AGENT = Actor(actorId="agent", kind=ActorKind.AGENT)
REF = SourceRef(kind=SourceRefKind.URL, value="https://event.example/rules")
EMAIL_REF = SourceRef(kind=SourceRefKind.EMAIL_MESSAGE_ID, value="<abc123@mail.example>")


def makeCharter() -> Charter:
    charter = addCharterClause(Charter(), "c1", "Demo video must be public.", REF, OWNER)
    return addCharterClause(charter, "c2", "Repo must be open source.", REF, OWNER)


def makeAmendment(amendmentId: str = "a1", clauseId: str = "c1") -> CharterAmendment:
    return CharterAmendment(amendmentId=amendmentId, clauseId=clauseId, newText="Demo video must be public and under 3 minutes.",
                            sourceRef=EMAIL_REF, submittedBy=OWNER, submittedAt=NOW)


def makeCheck(checkId: str, clauseId: str, status: CheckStatus = CheckStatus.PASS) -> ProofCheck:
    return ProofCheck(checkId=checkId, clauseId=clauseId, clauseVersion=1, interpretation="YouTube unlisted is acceptable.",
                      status=status, authoredBy=JUNIOR)


def makeSource(sourceId: str = "s1") -> SourceRecord:
    return SourceRecord(sourceId=sourceId, kind=SourceKind.EVENT_PAGE, location="https://event.example/rules",
                        status=SourceStatus.CHECKED, retrievedAt=NOW, contentSha256="a" * 64)


def pendingPacket(**overrides) -> SubmissionPacket:
    packet = SubmissionPacket(packetId="p1", charter=makeCharter(), **overrides)
    packet = transitionPacket(packet, ApprovalState.AUDITING, AGENT)
    return transitionPacket(packet, ApprovalState.PENDING_HUMAN_APPROVAL, AGENT)


class TestActorsAndRefs:
    def test_humanRequiresRole(self):
        with pytest.raises(ValidationError):
            Actor(actorId="x", kind=ActorKind.HUMAN)

    def test_agentCannotHoldRole(self):
        with pytest.raises(ValidationError):
            Actor(actorId="x", kind=ActorKind.AGENT, role=Role.OWNER)

    @pytest.mark.parametrize("value", ["ftp://x.example", "javascript:alert(1)", "not a url", "https://"])
    def test_urlRefRejectsNonHttp(self, value):
        with pytest.raises(ValidationError):
            SourceRef(kind=SourceRefKind.URL, value=value)

    def test_controlCharsRejected(self):
        with pytest.raises(ValidationError):
            CharterAmendment(amendmentId="a", clauseId="c1", newText="bad\x00text", sourceRef=REF, submittedBy=OWNER, submittedAt=NOW)

    def test_naiveDatetimeRejected(self):
        with pytest.raises(ValidationError):
            CharterAmendment(amendmentId="a", clauseId="c1", newText="x", sourceRef=REF, submittedBy=OWNER, submittedAt=datetime(2026, 1, 1))


class TestCharter:
    def test_amendmentRequiresSourceRef(self):
        with pytest.raises(ValidationError):
            CharterAmendment(amendmentId="a", clauseId="c1", newText="x", submittedBy=OWNER, submittedAt=NOW)

    def test_amendmentRequiresOwner(self):
        with pytest.raises(ValidationError):
            CharterAmendment(amendmentId="a", clauseId="c1", newText="x", sourceRef=REF, submittedBy=JUNIOR, submittedAt=NOW)

    def test_clauseCreationRequiresOwner(self):
        with pytest.raises(ModelError):
            addCharterClause(Charter(), "c1", "x", REF, JUNIOR)

    def test_duplicateClauseRejected(self):
        with pytest.raises(ModelError):
            addCharterClause(makeCharter(), "c1", "x", REF, OWNER)

    def test_amendmentAppendsVersionAndKeepsHistory(self):
        charter, _ = applyAmendment(makeCharter(), (), makeAmendment())
        assert charter.currentClause("c1").version == 2
        assert [c.version for c in charter.clauses if c.clauseId == "c1"] == [1, 2]
        assert charter.currentClause("c1").sourceRef == EMAIL_REF

    def test_amendmentMarksOnlyDependentsStale(self):
        checks = (makeCheck("p1", "c1"), makeCheck("p2", "c2"))
        _, updated = applyAmendment(makeCharter(), checks, makeAmendment())
        assert updated[0].status is CheckStatus.STALE
        assert updated[1].status is CheckStatus.PASS

    def test_duplicateAmendmentRejected(self):
        charter, _ = applyAmendment(makeCharter(), (), makeAmendment())
        with pytest.raises(ModelError):
            applyAmendment(charter, (), makeAmendment())

    def test_amendmentUnknownClause(self):
        with pytest.raises(ModelError):
            applyAmendment(makeCharter(), (), makeAmendment(clauseId="zzz"))

    def test_outOfOrderVersionsRejected(self):
        charter = makeCharter()
        with pytest.raises(ValidationError):
            Charter(clauses=(charter.clauses[0].model_copy(update={"version": 2}),))

    def test_charterIsFrozen(self):
        with pytest.raises(ValidationError):
            makeCharter().clauses = ()


class TestProof:
    def test_staleReconfirmBecomesUnverifiedAtCurrentVersion(self):
        charter, (stale,) = applyAmendment(makeCharter(), (makeCheck("p1", "c1"),), makeAmendment())
        fresh = reconfirmProofCheck(stale, charter, JUNIOR, "Unlisted under 3 minutes is acceptable.")
        assert fresh.status is CheckStatus.UNVERIFIED and fresh.clauseVersion == 2 and not fresh.isPromoted

    def test_reconfirmRequiresHumanAndStale(self):
        with pytest.raises(ModelError):
            reconfirmProofCheck(makeCheck("p1", "c1", CheckStatus.STALE), makeCharter(), AGENT, "x")
        with pytest.raises(ModelError):
            reconfirmProofCheck(makeCheck("p1", "c1"), makeCharter(), JUNIOR, "x")

    def test_promotionRequiresHigherAccess(self):
        promoted = promoteProofCheck(makeCheck("p1", "c1"), makeCharter(), OWNER)
        assert promoted.isPromoted
        with pytest.raises(ValidationError):
            promoteProofCheck(makeCheck("p1", "c1"), makeCharter(), JUNIOR)
        with pytest.raises(ValidationError):
            promoteProofCheck(makeCheck("p1", "c1"), makeCharter(), AGENT)

    def test_promotionRequiresPassAndCurrentVersion(self):
        with pytest.raises(ModelError):
            promoteProofCheck(makeCheck("p1", "c1", CheckStatus.UNVERIFIED), makeCharter(), OWNER)
        charter, _ = applyAmendment(makeCharter(), (), makeAmendment())
        with pytest.raises(ModelError):
            promoteProofCheck(makeCheck("p1", "c1"), charter, OWNER)

    def test_promotionFieldsTogether(self):
        with pytest.raises(ValidationError):
            ProofCheck(checkId="p", clauseId="c1", clauseVersion=1, interpretation="x", authoredBy=JUNIOR, promotedBy=OWNER)


class TestWorkbench:
    @pytest.mark.parametrize("tier", [1, 2, 17])
    def test_tierBounds(self, tier):
        with pytest.raises(ValidationError):
            WorkbenchCheck(checkId="w", tier=tier, label="x", authoredBy=JUNIOR)


class TestCoverage:
    def test_agentCannotConfirm(self):
        with pytest.raises(ModelError):
            confirmCoverage(CoverageLedger(sources=(makeSource(),)), AGENT)

    def test_emptyLedgerCannotBeConfirmed(self):
        with pytest.raises(ValidationError):
            confirmCoverage(CoverageLedger(), OWNER)

    def test_sourceChangeInvalidatesConfirmation(self):
        ledger = confirmCoverage(CoverageLedger(sources=(makeSource(),)), OWNER)
        assert ledger.humanConfirmedComplete
        ledger = upsertSource(ledger, makeSource("s2"))
        assert not ledger.humanConfirmedComplete and len(ledger.sources) == 2

    def test_checkedSourceNeedsHash(self):
        with pytest.raises(ValidationError):
            SourceRecord(sourceId="s", kind=SourceKind.EMAIL, location="inbox", status=SourceStatus.CHECKED)

    def test_duplicateSourceIds(self):
        with pytest.raises(ValidationError):
            CoverageLedger(sources=(makeSource(), makeSource()))


class TestEvidence:
    def base(self, **overrides):
        data = dict(evidenceId="e", url="https://youtu.be/x", method=EvidenceMethod.HEADLESS_BROWSER, finalStatus=200,
                    authWallDetected=False, status=CheckStatus.PASS, checkedAt=NOW)
        data.update(overrides)
        return EvidenceItem(**data)

    def test_validPass(self):
        assert self.base().status is CheckStatus.PASS

    @pytest.mark.parametrize("overrides", [
        {"authWallDetected": True},
        {"finalStatus": 404},
        {"finalStatus": None},
        {"method": EvidenceMethod.HTTP_CLIENT},
        {"status": CheckStatus.STALE},
    ])
    def test_invalidPass(self, overrides):
        with pytest.raises(ValidationError):
            self.base(**overrides)


class TestAlignment:
    @pytest.mark.parametrize("score", [-1, 101])
    def test_scoreBounds(self, score):
        with pytest.raises(ValidationError):
            AlignmentReport(alignmentScore=score, evidenceBasis={EvidenceBasis.DOCUMENTATION}, caveat="x", generatedAt=NOW)

    def test_basisAndCaveatRequired(self):
        with pytest.raises(ValidationError):
            AlignmentReport(alignmentScore=50, evidenceBasis=set(), caveat="x", generatedAt=NOW)
        with pytest.raises(ValidationError):
            AlignmentReport(alignmentScore=50, evidenceBasis={EvidenceBasis.DOCUMENTATION}, caveat="  ", generatedAt=NOW)


class TestPacket:
    def test_happyPath(self):
        packet = pendingPacket(ledger=confirmCoverage(CoverageLedger(sources=(makeSource(),)), OWNER))
        approved = transitionPacket(packet, ApprovalState.APPROVED, OWNER)
        assert approved.approvalState is ApprovalState.APPROVED and approved.decidedBy == OWNER

    def test_agentCannotApproveOrReject(self):
        packet = pendingPacket(ledger=confirmCoverage(CoverageLedger(sources=(makeSource(),)), OWNER))
        for target in (ApprovalState.APPROVED, ApprovalState.REJECTED):
            with pytest.raises(ModelError):
                transitionPacket(packet, target, AGENT)

    def test_directConstructionCannotBypassApproval(self):
        with pytest.raises(ValidationError):
            SubmissionPacket(packetId="p", approvalState=ApprovalState.APPROVED, decidedBy=AGENT, decidedAt=NOW)

    def test_approvalRequiresConfirmedLedger(self):
        with pytest.raises(ValidationError):
            transitionPacket(pendingPacket(), ApprovalState.APPROVED, OWNER)

    def test_approvalBlockedByStale(self):
        ledger = confirmCoverage(CoverageLedger(sources=(makeSource(),)), OWNER)
        packet = pendingPacket(ledger=ledger, proofChecks=(makeCheck("p1", "c1", CheckStatus.STALE),))
        with pytest.raises(ValidationError):
            transitionPacket(packet, ApprovalState.APPROVED, OWNER)
        assert transitionPacket(packet, ApprovalState.REJECTED, OWNER).approvalState is ApprovalState.REJECTED

    @pytest.mark.parametrize("start,target", [
        (ApprovalState.DRAFT, ApprovalState.APPROVED),
        (ApprovalState.DRAFT, ApprovalState.PENDING_HUMAN_APPROVAL),
    ])
    def test_illegalTransitions(self, start, target):
        with pytest.raises(ModelError):
            transitionPacket(SubmissionPacket(packetId="p", approvalState=start), target, OWNER)

    def test_terminalStates(self):
        rejected = transitionPacket(pendingPacket(), ApprovalState.REJECTED, OWNER)
        with pytest.raises(ModelError):
            transitionPacket(rejected, ApprovalState.AUDITING, OWNER)

    def test_oldVersionCheckMustBeStale(self):
        charter, _ = applyAmendment(makeCharter(), (), makeAmendment())
        with pytest.raises(ValidationError):
            SubmissionPacket(packetId="p", charter=charter, proofChecks=(makeCheck("p1", "c1"),))

    def test_unknownClauseRejected(self):
        with pytest.raises(ValidationError):
            SubmissionPacket(packetId="p", charter=makeCharter(), proofChecks=(makeCheck("p1", "zz"),))

    def test_duplicateIdsRejected(self):
        with pytest.raises(ValidationError):
            SubmissionPacket(packetId="p", charter=makeCharter(), proofChecks=(makeCheck("p1", "c1"), makeCheck("p1", "c2")))

    def test_gatingChecksOnlyPromoted(self):
        promoted = promoteProofCheck(makeCheck("p1", "c1"), makeCharter(), OWNER)
        packet = SubmissionPacket(packetId="p", charter=makeCharter(), proofChecks=(promoted, makeCheck("p2", "c2")))
        assert packet.gatingChecks == (promoted,)

    def test_jsonRoundTrip(self):
        packet = pendingPacket(reports=(AlignmentReport(alignmentScore=70, evidenceBasis={EvidenceBasis.CODE_EXISTENCE},
                                                        caveat="Documentation plus code existence only.", generatedAt=NOW),))
        assert SubmissionPacket.model_validate_json(packet.model_dump_json()) == packet
