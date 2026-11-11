"""Audit agent: Ingest -> Extract -> Reconcile -> Gate -> Plan -> Audit -> Finalize, as a LangGraph state machine.

Uncertainty flags record what the agent cannot confirm. Definite results (README missing, license missing) are
FAIL evidence only, so they do not inflate the confusion index.

The agent never approves, submits or sends anything. It ends every run at PENDING_HUMAN_APPROVAL.
If the structural confusion index exceeds the threshold after reconciliation, it halts before auditing and asks the human.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional, TypedDict

from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph

from shipproof.agent.project import AuditSummary, PlannedTask, Project, computeConfusionIndex
from shipproof.agent.reconcile import FlagSpec, reconcileDeadlines
from shipproof.llm import LlmClient, LlmError
from shipproof.models import (
    AGENT_ACTOR,
    AlignmentReport,
    ApprovalState,
    CheckStatus,
    CoverageLedger,
    EvidenceItem,
    EvidenceMethod,
    ProofCheck,
    Severity,
    SourceKind,
    SourceRecord,
    SourceStatus,
    SubmissionPacket,
    UncertaintyFlag,
    UncertaintyKind,
    transitionPacket,
    utcNow,
)
from shipproof.prompts import FeatureAssessment, GroundedRule, assessAlignment, checkInterpretation, extractRules
from shipproof.remediation import (
    Remediation,
    extractReadmeLinks,
    featureGuidance,
    licenseRemediation,
    readmeLinksRemediation,
    readmeRemediation,
)
from shipproof.tools.agentTools import makeSourceId, unreachableSource
from shipproof.tools.githubAudit import RepoAudit

Emit = Callable[[str, dict[str, Any]], None]
REQUIRED_SOURCE_KINDS = (SourceKind.EVENT_PAGE, SourceKind.EMAIL, SourceKind.REPOSITORY, SourceKind.PROPOSAL)


@dataclass(frozen=True)
class AuditDependencies:
    tools: dict[str, BaseTool]
    llm: Optional[LlmClient]
    llmUnavailableReason: Optional[str]
    confusionThreshold: int
    emit: Emit


class AuditState(TypedDict, total=False):
    project: Project
    packetId: str
    sources: list[SourceRecord]
    texts: dict[str, str]
    rules: list[GroundedRule]
    flags: list[UncertaintyFlag]
    evidence: list[EvidenceItem]
    reports: list[AlignmentReport]
    proofChecks: list[ProofCheck]
    plan: list[PlannedTask]
    repoAudit: Optional[RepoAudit]
    effectiveDeadline: Optional[datetime]
    features: list[FeatureAssessment]
    alignmentAvailable: bool
    remediations: list[Remediation]
    confusionIndex: int
    halted: bool


def _addFlags(state: AuditState, specs: list[FlagSpec], emit: Emit) -> list[UncertaintyFlag]:
    flags = list(state.get("flags", []))
    for spec in specs:
        flag = UncertaintyFlag(flagId=f"f{len(flags) + 1}", kind=spec.kind, detail=spec.detail[:512] or spec.kind.value,
                               severity=spec.severity, sourceIds=spec.sourceIds, raisedAt=utcNow())
        flags.append(flag)
        emit("flag", flag.model_dump(mode="json"))
    return flags


def _llmUnavailable(deps: AuditDependencies, stage: str) -> FlagSpec:
    return FlagSpec(UncertaintyKind.ANALYSIS_FAILED, Severity.HIGH,
                    f"{stage} skipped: language model unavailable ({deps.llmUnavailableReason or 'not configured'}).", ())


def buildAuditGraph(deps: AuditDependencies) -> Any:
    emit = deps.emit

    def ingest(state: AuditState) -> AuditState:
        project = state["project"]
        emit("stage", {"stage": "ingest"})
        sources: list[SourceRecord] = []
        texts: dict[str, str] = {}
        specs: list[FlagSpec] = []

        proposalSource = SourceRecord(sourceId=makeSourceId("proposal", project.projectId), kind=SourceKind.PROPOSAL,
                                      location=f"project:{project.projectId}/proposal", status=SourceStatus.CHECKED,
                                      retrievedAt=utcNow(), contentSha256=hashlib.sha256(project.proposalText.encode()).hexdigest())
        sources.append(proposalSource)

        for url in project.rulePages:
            result = deps.tools["readRulesPage"].invoke({"url": url})
            record = SourceRecord.model_validate(result["source"])
            if record.sourceId in {s.sourceId for s in sources}:
                continue
            sources.append(record)
            if record.status is SourceStatus.CHECKED and result.get("text"):
                texts[record.sourceId] = result["text"]
            else:
                specs.append(FlagSpec(UncertaintyKind.UNREACHABLE_SOURCE, Severity.HIGH,
                                      f"Rules page unreachable as a logged-out visitor: {url[:200]} ({record.detail})", (record.sourceId,)))
            emit("source", record.model_dump(mode="json"))

        if project.emailQuery and "readEmails" in deps.tools:
            try:
                for item in deps.tools["readEmails"].invoke({"query": project.emailQuery, "maxResults": 25})["messages"]:
                    record = SourceRecord.model_validate(item["source"])
                    sources.append(record)
                    texts[record.sourceId] = f"Subject: {item['email']['subject']}\n\n{item['email']['bodyText']}"
                    emit("source", record.model_dump(mode="json"))
            except Exception as error:  # surfaced as a flag and an UNREACHABLE source, never swallowed
                record = unreachableSource("gmail", SourceKind.EMAIL, f"{type(error).__name__}: {error}")
                sources.append(record)
                specs.append(FlagSpec(UncertaintyKind.UNREACHABLE_SOURCE, Severity.HIGH, f"Inbox unreachable: {error}"[:500], (record.sourceId,)))
        else:
            reason = "no email query configured" if not project.emailQuery else "Gmail not authorised on this server"
            sources.append(SourceRecord(sourceId=makeSourceId("email", "inbox"), kind=SourceKind.EMAIL,
                                        location="gmail:inbox", status=SourceStatus.NOT_CHECKED, detail=reason))
        return {"sources": sources, "texts": texts, "flags": _addFlags(state, specs, emit)}

    def extract(state: AuditState) -> AuditState:
        emit("stage", {"stage": "extract"})
        rules: list[GroundedRule] = []
        specs: list[FlagSpec] = []
        if deps.llm is None:
            if state["texts"]:
                specs.append(_llmUnavailable(deps, "Rule extraction"))
            return {"rules": rules, "flags": _addFlags(state, specs, emit)}
        for sourceId, text in state["texts"].items():
            try:
                result = extractRules(deps.llm, sourceId, text)
            except (LlmError, ValueError) as error:
                specs.append(FlagSpec(UncertaintyKind.ANALYSIS_FAILED, Severity.HIGH, f"Rule extraction failed: {error}"[:500], (sourceId,)))
                continue
            rules.extend(result.rules)
            if result.droppedUngroundedCount:
                specs.append(FlagSpec(UncertaintyKind.UNGROUNDED_MODEL_OUTPUT, Severity.LOW,
                                      f"{result.droppedUngroundedCount} extracted rule(s) dropped: quote not found in source.", (sourceId,)))
            for injection in result.suspectedInjections:
                specs.append(FlagSpec(UncertaintyKind.PROMPT_INJECTION_SUSPECTED, Severity.HIGH,
                                      f"Instruction-like text in source was ignored: \"{injection[:300]}\"", (sourceId,)))
        emit("rules", {"count": len(rules)})
        return {"rules": rules, "flags": _addFlags(state, specs, emit)}

    def reconcile(state: AuditState) -> AuditState:
        emit("stage", {"stage": "reconcile"})
        sourceDates = {s.sourceId: s.sourceDate for s in state["sources"]}
        deadline, specs = reconcileDeadlines(tuple(state["rules"]), sourceDates)
        flags = _addFlags(state, specs if state["rules"] or state["texts"] else [], emit)
        index = computeConfusionIndex(flags)
        halted = index > deps.confusionThreshold
        if halted:
            flags = _addFlags({"flags": flags}, [FlagSpec(
                UncertaintyKind.CONFUSION_THRESHOLD_EXCEEDED, Severity.CRITICAL,
                f"Confusion index {index} exceeds threshold {deps.confusionThreshold}. Agent halted before auditing: "
                "resolve the flagged sources (e.g. amend the Charter) and re-run.", ())], emit)
        emit("confusion", {"index": index, "threshold": deps.confusionThreshold, "halted": halted})
        return {"effectiveDeadline": deadline, "flags": flags, "confusionIndex": index, "halted": halted}

    def plan(state: AuditState) -> AuditState:
        emit("stage", {"stage": "plan"})
        project = state["project"]
        tasks = [PlannedTask(tool="auditRepository", target=project.repo, reason="Repository must be public with README and license.")]
        tasks += [PlannedTask(tool="auditLink", target=link.url[:512], reason=f"{link.label} must be viewable logged-out.")
                  for link in project.links]
        tasks.append(PlannedTask(tool="assessAlignment", target=project.repo, reason="Compare proposal promises with code."))
        tasks += [PlannedTask(tool="checkInterpretation", target=check.checkId, reason=f"Re-check reading of clause {check.clauseId}.")
                  for check in project.proofChecks if check.status is not CheckStatus.STALE]
        emit("plan", {"tasks": [t.model_dump(mode="json") for t in tasks]})
        return {"plan": tasks}

    def audit(state: AuditState) -> AuditState:
        emit("stage", {"stage": "audit"})
        project = state["project"]
        evidence: list[EvidenceItem] = []
        sources = list(state["sources"])
        specs: list[FlagSpec] = []
        reports: list[AlignmentReport] = []
        proofChecks = list(project.proofChecks)
        repoAudit: Optional[RepoAudit] = None
        features: list[FeatureAssessment] = []
        alignmentAvailable = False

        try:
            repoAudit = RepoAudit.model_validate(deps.tools["auditRepository"].invoke({"repo": project.repo}))
            evidence.append(repoAudit.visibilityEvidence)
            repoUrl = repoAudit.visibilityEvidence.url
            for evidenceId, status, passText, failText in (
                ("repoReadme", repoAudit.readmeStatus, "README is populated.", "README missing or under 200 characters."),
                ("repoLicense", repoAudit.licenseStatus, f"License: {repoAudit.licenseSpdx}.", "No open-source license detected."),
            ):
                evidence.append(EvidenceItem(evidenceId=evidenceId, url=repoUrl, method=EvidenceMethod.GITHUB_API,
                                             finalStatus=200 if status is CheckStatus.PASS else None, authWallDetected=False,
                                             status=status, checkedAt=utcNow(),
                                             detail=passText if status is CheckStatus.PASS else failText if status is CheckStatus.FAIL
                                             else "Not checked: repository content unavailable."))
            sources.append(SourceRecord(sourceId=makeSourceId("repository", project.repo), kind=SourceKind.REPOSITORY,
                                        location=repoUrl, status=SourceStatus.CHECKED, retrievedAt=utcNow(),
                                        contentSha256=hashlib.sha256((repoAudit.readmeText or "").encode()).hexdigest()))
            if not repoAudit.isPubliclyVisible:
                specs.append(FlagSpec(UncertaintyKind.AUTH_REDIRECT, Severity.CRITICAL,
                                      f"Repository is not visible to logged-out judges. {' '.join(repoAudit.issues)}", ()))
        except Exception as error:  # GitHub errors become evidence of an unreachable source
            sources.append(unreachableSource(project.repo, SourceKind.REPOSITORY, f"{type(error).__name__}: {error}"))
            specs.append(FlagSpec(UncertaintyKind.UNREACHABLE_SOURCE, Severity.CRITICAL, f"Repository audit failed: {error}"[:500], ()))
        emit("evidence", {"items": [e.model_dump(mode="json") for e in evidence]})

        for position, link in enumerate(project.links, start=1):
            item = EvidenceItem.model_validate(deps.tools["auditLink"].invoke({"url": link.url, "evidenceId": f"link{position}"}))
            evidence.append(item)
            emit("evidence", {"items": [item.model_dump(mode="json")]})
            if item.authWallDetected:
                specs.append(FlagSpec(UncertaintyKind.AUTH_REDIRECT, Severity.CRITICAL, f"{link.label} is behind a login wall: {item.detail}", ()))
            elif item.status is not CheckStatus.PASS:
                specs.append(FlagSpec(UncertaintyKind.FAILED_RENDER, Severity.HIGH, f"{link.label} failed: {item.detail}", ()))

        if deps.llm is None:
            specs.append(_llmUnavailable(deps, "Alignment and interpretation checks"))
        else:
            if repoAudit is not None and repoAudit.codeIndex is not None:
                try:
                    alignment = assessAlignment(deps.llm, project.proposalText, repoAudit.readmeText or "",
                                                repoAudit.commitMessages, repoAudit.codeIndex)
                    features = list(alignment.features)
                    alignmentAvailable = True
                    if alignment.report is not None:
                        reports.append(alignment.report)
                        emit("alignment", alignment.report.model_dump(mode="json"))
                    else:
                        specs.append(FlagSpec(UncertaintyKind.ANALYSIS_FAILED, Severity.MEDIUM, "No grounded features found in the proposal.", ()))
                    for injection in alignment.suspectedInjections:
                        specs.append(FlagSpec(UncertaintyKind.PROMPT_INJECTION_SUSPECTED, Severity.HIGH,
                                              f"Instruction-like text in repository was ignored: \"{injection[:300]}\"", ()))
                except (LlmError, ValueError) as error:
                    specs.append(FlagSpec(UncertaintyKind.ANALYSIS_FAILED, Severity.HIGH, f"Alignment check failed: {error}"[:500], ()))
            for position, check in enumerate(proofChecks):
                if check.status is CheckStatus.STALE:
                    continue
                try:
                    result = checkInterpretation(deps.llm, project.charter.currentClause(check.clauseId).text, check.interpretation)
                except (LlmError, ValueError) as error:
                    specs.append(FlagSpec(UncertaintyKind.ANALYSIS_FAILED, Severity.MEDIUM, f"Proof check {check.checkId} failed: {error}"[:500], ()))
                    continue
                proofChecks[position] = check.model_copy(update={"status": result.status})
                emit("proofCheck", {"checkId": check.checkId, "status": result.status.value, "label": result.label})
        return {"evidence": evidence, "sources": sources, "reports": reports, "proofChecks": proofChecks,
                "repoAudit": repoAudit, "features": features, "alignmentAvailable": alignmentAvailable,
                "flags": _addFlags(state, specs, emit)}

    def remediate(state: AuditState) -> AuditState:
        """Drafts fixes for minor problems. Drafting only: nothing is written until a human applies a fix."""
        emit("stage", {"stage": "remediate"})
        repoAudit = state.get("repoAudit")
        evidence = list(state["evidence"])
        drafts: list[Remediation] = []
        if repoAudit is None:
            return {"remediations": drafts}
        features = tuple(state.get("features", []))
        if repoAudit.licenseStatus is CheckStatus.FAIL:
            drafts.append(licenseRemediation())
        if repoAudit.codeIndex is not None and repoAudit.readmeStatus is CheckStatus.FAIL:
            drafts.append(readmeRemediation(repoAudit.repo, repoAudit.codeIndex, features, state.get("alignmentAvailable", False)))
        elif repoAudit.readmeText:
            linkEvidence = [EvidenceItem.model_validate(deps.tools["auditLink"].invoke({"url": url, "evidenceId": f"readmeLink{n}"}))
                            for n, url in enumerate(extractReadmeLinks(repoAudit.readmeText), start=1)]
            evidence += linkEvidence
            if linkEvidence:
                emit("evidence", {"items": [e.model_dump(mode="json") for e in linkEvidence]})
            linkFix = readmeLinksRemediation(repoAudit.readmeText, linkEvidence)
            if linkFix is not None:
                drafts.append(linkFix)
        if repoAudit.codeIndex is not None:
            guidance = featureGuidance(features, repoAudit.codeIndex)
            if guidance is not None:
                drafts.append(guidance)
        for draft in drafts:
            emit("remediation", {"remediationId": draft.remediationId, "title": draft.title})
        return {"remediations": drafts, "evidence": evidence}

    def finalize(state: AuditState) -> AuditState:
        emit("stage", {"stage": "finalize"})
        present = {s.kind for s in state["sources"] if s.status is SourceStatus.CHECKED}
        missing = [k.value for k in REQUIRED_SOURCE_KINDS if k not in present]
        specs = [FlagSpec(UncertaintyKind.MISSING_SOURCE_CATEGORY, Severity.MEDIUM,
                          f"No checked source of type {kind}; coverage is incomplete.", ()) for kind in missing]
        flags = _addFlags(state, specs, emit)
        index = computeConfusionIndex(flags)
        if index > deps.confusionThreshold and not state["halted"]:
            flags = _addFlags({"flags": flags}, [FlagSpec(
                UncertaintyKind.CONFUSION_THRESHOLD_EXCEEDED, Severity.CRITICAL,
                f"Confusion index {index} exceeds threshold {deps.confusionThreshold} after auditing. "
                "Resolve the flagged items and re-run before approving.", ())], emit)
        emit("confusion", {"index": index, "threshold": deps.confusionThreshold, "halted": state["halted"]})
        return {"flags": flags, "confusionIndex": index}

    graph = StateGraph(AuditState)
    for name, node in (("ingest", ingest), ("extract", extract), ("reconcile", reconcile), ("plan", plan),
                       ("audit", audit), ("remediate", remediate), ("finalize", finalize)):
        graph.add_node(name, node)
    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "extract")
    graph.add_edge("extract", "reconcile")
    graph.add_conditional_edges("reconcile", lambda s: "finalize" if s["halted"] else "plan", {"finalize": "finalize", "plan": "plan"})
    graph.add_edge("plan", "audit")
    graph.add_edge("audit", "remediate")
    graph.add_edge("remediate", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()


def runAudit(project: Project, packetId: str, deps: AuditDependencies, startedAt: datetime) -> tuple[SubmissionPacket, AuditSummary, tuple[ProofCheck, ...]]:
    """Runs the graph and returns (packet at PENDING_HUMAN_APPROVAL, summary, proof checks with refreshed statuses)."""
    final: AuditState = buildAuditGraph(deps).invoke({"project": project, "packetId": packetId, "flags": [], "evidence": [],
                                                      "reports": [], "plan": [], "proofChecks": list(project.proofChecks),
                                                      "repoAudit": None, "halted": False, "confusionIndex": 0,
                                                      "features": [], "alignmentAvailable": False, "remediations": []})
    proofChecks = tuple(final.get("proofChecks", project.proofChecks))
    packet = SubmissionPacket(
        packetId=packetId, approvalState=ApprovalState.AUDITING, charter=project.charter, proofChecks=proofChecks,
        workbenchChecks=project.workbenchChecks, ledger=CoverageLedger(sources=tuple(final["sources"])),
        evidence=tuple(final["evidence"]), uncertaintyFlags=tuple(final["flags"]), reports=tuple(final["reports"]),
    )
    packet = transitionPacket(packet, ApprovalState.PENDING_HUMAN_APPROVAL, AGENT_ACTOR)
    summary = AuditSummary(packetId=packetId, projectId=project.projectId, startedAt=startedAt, finishedAt=utcNow(),
                           plan=tuple(final["plan"]), rules=tuple(final.get("rules", [])),
                           effectiveDeadline=final.get("effectiveDeadline"), confusionIndex=final["confusionIndex"],
                           confusionThreshold=deps.confusionThreshold, halted=final["halted"],
                           remediations=tuple(final.get("remediations", [])))
    return packet, summary, proofChecks
