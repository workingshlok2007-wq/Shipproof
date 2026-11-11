"""Audit agent as a LangGraph state machine.

  ingest -> extract -> reconcile -> [halt? finalize] -> plan -> audit -> investigate -> reflect -> [ask] -> remediate -> finalize

- plan/audit: fixed baseline checks that always run.
- investigate: the model chooses extra read-only steps from URLs it has actually seen (bounded rounds and steps).
- reflect: deterministic second pass on its own flags: re-check failures, look for alternatives, search the inbox.
- ask: when a deadline conflict cannot be resolved, the graph pauses (LangGraph interrupt) and resumes on the human's
  answer. Earlier answers are reused from project memory instead of asking again.
- finalize: applies risks the human accepted before, computes the confusion index and the diff with the last audit.

Uncertainty flags record what the agent cannot confirm; definite results are FAIL evidence. The agent never approves,
submits, sends or merges anything. Every run ends at PENDING_HUMAN_APPROVAL, or pauses at AWAITING_INPUT.
"""

from __future__ import annotations

import enum
import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional, TypedDict
from urllib.parse import urlsplit

from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from shipproof.agent.investigate import MAX_ROUNDS, InvestigationAction, extractUrls, planInvestigation
from shipproof.agent.memory import (
    UNSURE_VALUE,
    AgentQuestion,
    AgentStep,
    HumanDecision,
    QuestionKind,
    QuestionOption,
    StepOrigin,
    _fingerprint,
    computeDiff,
)
from shipproof.agent.project import AuditSummary, PlannedTask, Project, computeConfusionIndex
from shipproof.agent.reconcile import FlagSpec, effectiveDate, reconcileDeadlines
from shipproof.llm import LlmClient, LlmError
from shipproof.models import (
    AGENT_ACTOR,
    Actor,
    AlignmentReport,
    ApprovalState,
    CheckStatus,
    CoverageLedger,
    EvidenceItem,
    EvidenceMethod,
    FrozenModel,
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
from shipproof.prompts import FeatureAssessment, GroundedRule, RuleCategory, assessAlignment, checkInterpretation, extractRules
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
MAX_ALTERNATIVE_CHECKS = 2
MAX_STEPS = 60
STATE_KEYS_FROM_REFLECT = ("steps", "flags", "evidence", "sources", "texts", "rules", "candidateLinks", "visited",
                           "effectiveDeadline", "deadlineFlagIds", "pendingQuestion", "questions", "decisions")


@dataclass(frozen=True)
class AuditDependencies:
    tools: dict[str, BaseTool]
    llm: Optional[LlmClient]
    llmUnavailableReason: Optional[str]
    confusionThreshold: int
    emit: Emit
    budgetSeconds: int = 90
    deadline: float = field(default_factory=lambda: time.monotonic() + 90)
    previousPacket: Optional[SubmissionPacket] = None
    checkpointer: Optional[Any] = None
    llmModel: Optional[str] = None

    def budgetLeft(self) -> bool:
        return time.monotonic() < self.deadline


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
    steps: list[AgentStep]
    visited: list[str]
    candidateLinks: list[str]
    deadlineFlagIds: list[str]
    pendingQuestion: Optional[AgentQuestion]
    questions: list[AgentQuestion]
    decisions: list[HumanDecision]
    budgetExhausted: bool


def checkpointSerializer() -> JsonPlusSerializer:
    """Explicit allowlist of every ShipProof model and enum the paused graph may store; nothing else is deserialised."""
    import shipproof.agent.memory as memoryModule
    import shipproof.models as modelsModule
    import shipproof.prompts as promptsModule
    import shipproof.remediation as remediationModule

    seen: set[type] = set()
    pending: list[type] = [FrozenModel]
    while pending:
        cls = pending.pop()
        for sub in cls.__subclasses__():
            if sub not in seen:
                seen.add(sub)
                pending.append(sub)
    enums = {obj for module in (modelsModule, memoryModule, promptsModule, remediationModule) for obj in vars(module).values()
             if isinstance(obj, type) and issubclass(obj, enum.Enum) and obj.__module__.startswith("shipproof")}
    return JsonPlusSerializer(allowed_msgpack_modules=[(c.__module__, c.__name__) for c in seen | enums])


def newCheckpointer() -> InMemorySaver:
    return InMemorySaver(serde=checkpointSerializer())


# ---------------------------------------------------------------- helpers


def _addFlags(flags: list[UncertaintyFlag], specs: list[FlagSpec], emit: Emit) -> list[UncertaintyFlag]:
    flags = list(flags)
    nextId = max((int(f.flagId[1:]) for f in flags if f.flagId[1:].isdigit()), default=0) + 1
    for spec in specs:
        flag = UncertaintyFlag(flagId=f"f{nextId}", kind=spec.kind, detail=spec.detail[:512] or spec.kind.value,
                               severity=spec.severity, sourceIds=spec.sourceIds, raisedAt=utcNow())
        nextId += 1
        flags.append(flag)
        emit("flag", flag.model_dump(mode="json"))
    return flags


def _addStep(steps: list[AgentStep], emit: Emit, origin: StepOrigin, tool: str, target: str, reason: str,
             outcome: Optional[str] = None) -> list[AgentStep]:
    if len(steps) >= MAX_STEPS:
        return list(steps)
    step = AgentStep(stepId=len(steps) + 1, origin=origin, tool=tool[:512], target=(target or "-")[:512],
                     reason=(reason or "-")[:2000], outcome=outcome[:2000] if outcome else None)
    emit("agentStep", step.model_dump(mode="json"))
    return [*steps, step]


def _llmUnavailable(deps: AuditDependencies, stage: str) -> FlagSpec:
    return FlagSpec(UncertaintyKind.ANALYSIS_FAILED, Severity.HIGH,
                    f"{stage} skipped: language model unavailable ({deps.llmUnavailableReason or 'not configured'}).", ())


def _extract(deps: AuditDependencies, sourceId: str, text: str) -> tuple[list[GroundedRule], list[FlagSpec]]:
    if deps.llm is None:
        return [], [_llmUnavailable(deps, "Rule extraction")]
    try:
        result = extractRules(deps.llm, sourceId, text)
    except (LlmError, ValueError) as error:
        return [], [FlagSpec(UncertaintyKind.ANALYSIS_FAILED, Severity.HIGH, f"Rule extraction failed: {error}"[:500], (sourceId,))]
    specs: list[FlagSpec] = []
    if result.droppedUngroundedCount:
        specs.append(FlagSpec(UncertaintyKind.UNGROUNDED_MODEL_OUTPUT, Severity.LOW,
                              f"{result.droppedUngroundedCount} extracted rule(s) dropped: quote not found in source.", (sourceId,)))
    for injection in result.suspectedInjections:
        specs.append(FlagSpec(UncertaintyKind.PROMPT_INJECTION_SUSPECTED, Severity.HIGH,
                              f"Instruction-like text in source was ignored: \"{injection[:300]}\"", (sourceId,)))
    return list(result.rules), specs


def _reconcile(state: AuditState, emit: Emit) -> AuditState:
    """(Re)computes the deadline, replacing the previous reconciliation's flags so re-runs never duplicate them."""
    stale = set(state.get("deadlineFlagIds", []))
    flags = [f for f in state.get("flags", []) if f.flagId not in stale]
    sourceDates = {s.sourceId: s.sourceDate for s in state["sources"]}
    deadline, specs = reconcileDeadlines(tuple(state.get("rules", [])), sourceDates)
    if not (state.get("rules") or state.get("texts")):
        specs = []
    before = {f.flagId for f in flags}
    flags = _addFlags(flags, specs, emit)
    return {"effectiveDeadline": deadline, "flags": flags, "deadlineFlagIds": [f.flagId for f in flags if f.flagId not in before]}


def _deadlineCandidates(state: AuditState) -> list[tuple[datetime, GroundedRule, Optional[datetime]]]:
    sourceDates = {s.sourceId: s.sourceDate for s in state["sources"]}
    best: dict[datetime, tuple[datetime, GroundedRule, Optional[datetime]]] = {}
    for rule in state.get("rules", []):
        if rule.category is RuleCategory.DEADLINE and rule.deadlineAt is not None and rule.deadlineAt not in best:
            best[rule.deadlineAt] = (rule.deadlineAt, rule, effectiveDate(rule, sourceDates))
    return sorted(best.values(), key=lambda item: item[0])


VIDEO_SITES = frozenset({"youtube.com", "youtu.be", "vimeo.com", "loom.com", "google.com", "dropbox.com", "streamable.com"})
_PATH_WORD = re.compile(r"[a-z]{3,}")
_GENERIC_WORDS = frozenset({"html", "htm", "php", "index", "www", "http", "https", "com"})


def _site(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def isLikelyAlternative(original: str, candidate: str) -> bool:
    """Same site, and either a video host or a shared meaningful path word (video-private vs video-public)."""
    if candidate == original or _site(candidate) != _site(original):
        return False
    if _site(original) in VIDEO_SITES:
        return True
    words = lambda url: set(_PATH_WORD.findall(urlsplit(url).path.lower())) - _GENERIC_WORDS  # noqa: E731
    return bool(words(original) & words(candidate))


def _sourceLocation(state: AuditState, sourceId: str) -> str:
    for source in state["sources"]:
        if source.sourceId == sourceId:
            return source.location
    return sourceId


def _applyAnswer(state: AuditState, question: AgentQuestion, emit: Emit) -> AuditState:
    questions = [q for q in state.get("questions", []) if q.questionId != question.questionId] + [question]
    emit("questionAnswered", question.model_dump(mode="json"))
    deadlineIds = set(state.get("deadlineFlagIds", []))
    conflictIds = {f.flagId for f in state["flags"] if f.flagId in deadlineIds and f.kind is UncertaintyKind.CONFLICTING_SOURCES}
    if question.answer is None or question.answer.value == UNSURE_VALUE:
        flags = [f.model_copy(update={"followUp": "Owner was unsure; the deadline stays unresolved."}) if f.flagId in conflictIds else f
                 for f in state["flags"]]
        return {"questions": questions, "flags": flags}
    return {"questions": questions, "flags": [f for f in state["flags"] if f.flagId not in conflictIds],
            "effectiveDeadline": datetime.fromisoformat(question.answer.value),
            "deadlineFlagIds": [i for i in state.get("deadlineFlagIds", []) if i not in conflictIds]}


# ---------------------------------------------------------------- graph


def buildAuditGraph(deps: AuditDependencies) -> Any:
    emit = deps.emit

    def readPage(url: str, sources: list[SourceRecord], texts: dict[str, str],
                 candidates: list[str]) -> tuple[SourceRecord, Optional[str]]:
        result = deps.tools["readRulesPage"].invoke({"url": url})
        record = SourceRecord.model_validate(result["source"])
        if record.sourceId in {s.sourceId for s in sources}:
            sources[:] = [record if s.sourceId == record.sourceId else s for s in sources]
        else:
            sources.append(record)
        emit("source", record.model_dump(mode="json"))
        text = result.get("text") if record.status is SourceStatus.CHECKED else None
        if text:
            texts[record.sourceId] = text
            candidates.extend(link for link in result.get("links", []) or [] if isinstance(link, str))
        return record, text

    def readInbox(query: str, maxResults: int, sources: list[SourceRecord], texts: dict[str, str],
                  candidates: list[str]) -> list[str]:
        """Returns the sourceIds of messages not seen before. Raises on tool failure; callers record it."""
        added: list[str] = []
        for item in deps.tools["readEmails"].invoke({"query": query, "maxResults": maxResults})["messages"]:
            record = SourceRecord.model_validate(item["source"])
            if record.sourceId in {s.sourceId for s in sources}:
                continue
            sources.append(record)
            body = f"Subject: {item['email']['subject']}\n\n{item['email']['bodyText']}"
            texts[record.sourceId] = body
            candidates.extend(extractUrls(body))
            emit("source", record.model_dump(mode="json"))
            added.append(record.sourceId)
        return added

    def ingest(state: AuditState) -> AuditState:
        project = state["project"]
        emit("stage", {"stage": "ingest"})
        sources: list[SourceRecord] = []
        texts: dict[str, str] = {}
        candidates: list[str] = []
        specs: list[FlagSpec] = []
        steps = list(state.get("steps", []))
        visited = list(state.get("visited", []))

        sources.append(SourceRecord(sourceId=makeSourceId("proposal", project.projectId), kind=SourceKind.PROPOSAL,
                                    location=f"project:{project.projectId}/proposal", status=SourceStatus.CHECKED,
                                    retrievedAt=utcNow(), contentSha256=hashlib.sha256(project.proposalText.encode()).hexdigest()))
        for url in project.rulePages:
            visited.append(url)
            record, text = readPage(url, sources, texts, candidates)
            if not text:
                specs.append(FlagSpec(UncertaintyKind.UNREACHABLE_SOURCE, Severity.HIGH,
                                      f"Rules page unreachable as a logged-out visitor: {url[:200]} ({record.detail})", (record.sourceId,)))
            steps = _addStep(steps, emit, StepOrigin.BASELINE, "readRulesPage", url, "Configured rules page.",
                             "read" if text else f"unreachable: {record.detail}")

        if project.emailQuery and "readEmails" in deps.tools:
            try:
                added = readInbox(project.emailQuery, 25, sources, texts, candidates)
                visited.append(f"inbox:{project.emailQuery}")
                steps = _addStep(steps, emit, StepOrigin.BASELINE, "readEmails", project.emailQuery, "Configured inbox query.",
                                 f"{len(added)} message(s)")
            except Exception as error:  # surfaced as a flag and an UNREACHABLE source, never swallowed
                record = unreachableSource("gmail", SourceKind.EMAIL, f"{type(error).__name__}: {error}")
                sources.append(record)
                specs.append(FlagSpec(UncertaintyKind.UNREACHABLE_SOURCE, Severity.HIGH, f"Inbox unreachable: {error}"[:500], (record.sourceId,)))
        else:
            reason = "no email query configured" if not project.emailQuery else "Gmail not authorised on this server"
            sources.append(SourceRecord(sourceId=makeSourceId("email", "inbox"), kind=SourceKind.EMAIL,
                                        location="gmail:inbox", status=SourceStatus.NOT_CHECKED, detail=reason))
        return {"sources": sources, "texts": texts, "candidateLinks": candidates, "steps": steps, "visited": visited,
                "flags": _addFlags(state.get("flags", []), specs, emit)}

    def extract(state: AuditState) -> AuditState:
        emit("stage", {"stage": "extract"})
        rules: list[GroundedRule] = []
        specs: list[FlagSpec] = []
        if deps.llm is None:
            if state["texts"]:
                specs.append(_llmUnavailable(deps, "Rule extraction"))
        else:
            for sourceId, text in state["texts"].items():
                found, problems = _extract(deps, sourceId, text)
                rules.extend(found)
                specs.extend(problems)
        emit("rules", {"count": len(rules)})
        return {"rules": rules, "flags": _addFlags(state["flags"], specs, emit)}

    def reconcile(state: AuditState) -> AuditState:
        emit("stage", {"stage": "reconcile"})
        update = _reconcile(state, emit)
        flags = update["flags"]
        index = computeConfusionIndex(flags)
        halted = index > deps.confusionThreshold
        if halted:
            flags = _addFlags(flags, [FlagSpec(
                UncertaintyKind.CONFUSION_THRESHOLD_EXCEEDED, Severity.CRITICAL,
                f"Confusion index {index} exceeds threshold {deps.confusionThreshold}. Agent halted before auditing: "
                "resolve the flagged sources (e.g. amend the Charter) and re-run.", ())], emit)
        emit("confusion", {"index": index, "threshold": deps.confusionThreshold, "halted": halted})
        return {**update, "flags": flags, "confusionIndex": index, "halted": halted}

    def plan(state: AuditState) -> AuditState:
        emit("stage", {"stage": "plan"})
        project = state["project"]
        tasks = [PlannedTask(tool="auditRepository", target=project.repo, reason="Repository must be public with README and license.")]
        tasks += [PlannedTask(tool="auditLink", target=link.url[:512], reason=f"{link.label} must be viewable logged-out.")
                  for link in project.links]
        tasks.append(PlannedTask(tool="assessAlignment", target=project.repo, reason="Compare proposal promises with code."))
        tasks += [PlannedTask(tool="checkInterpretation", target=check.checkId, reason=f"Re-check reading of clause {check.clauseId}.")
                  for check in project.proofChecks if check.status is not CheckStatus.STALE]
        tasks.append(PlannedTask(tool="investigate", target="agent", reason="Model chooses extra read-only steps from what it found."))
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
        candidates = list(state.get("candidateLinks", []))
        visited = list(state.get("visited", []))
        steps = list(state.get("steps", []))
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
                                      f"Repository is not visible to logged-out judges. {' '.join(repoAudit.issues)}", ("repoVisibility",)))
            if repoAudit.readmeText:
                candidates.extend(extractReadmeLinks(repoAudit.readmeText))
            steps = _addStep(steps, emit, StepOrigin.BASELINE, "auditRepository", project.repo, "Baseline check.",
                             "public" if repoAudit.isPubliclyVisible else "not public")
        except Exception as error:  # GitHub errors become evidence of an unreachable source
            sources.append(unreachableSource(project.repo, SourceKind.REPOSITORY, f"{type(error).__name__}: {error}"))
            specs.append(FlagSpec(UncertaintyKind.UNREACHABLE_SOURCE, Severity.CRITICAL, f"Repository audit failed: {error}"[:500], ()))
            steps = _addStep(steps, emit, StepOrigin.BASELINE, "auditRepository", project.repo, "Baseline check.", f"failed: {error}")
        emit("evidence", {"items": [e.model_dump(mode="json") for e in evidence]})

        for position, link in enumerate(project.links, start=1):
            evidenceId = f"link{position}"
            visited.append(link.url)
            item = EvidenceItem.model_validate(deps.tools["auditLink"].invoke({"url": link.url, "evidenceId": evidenceId}))
            evidence.append(item)
            emit("evidence", {"items": [item.model_dump(mode="json")]})
            if item.authWallDetected:
                specs.append(FlagSpec(UncertaintyKind.AUTH_REDIRECT, Severity.CRITICAL, f"{link.label} is behind a login wall: {item.detail}", (evidenceId,)))
            elif item.status is not CheckStatus.PASS:
                specs.append(FlagSpec(UncertaintyKind.FAILED_RENDER, Severity.HIGH, f"{link.label} failed: {item.detail}", (evidenceId,)))
            steps = _addStep(steps, emit, StepOrigin.BASELINE, "auditLink", link.url, f"{link.label} must be viewable logged-out.", item.status.value)

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
        return {"evidence": evidence, "sources": sources, "reports": reports, "proofChecks": proofChecks, "repoAudit": repoAudit,
                "features": features, "alignmentAvailable": alignmentAvailable, "candidateLinks": candidates, "visited": visited,
                "steps": steps, "flags": _addFlags(state["flags"], specs, emit)}

    def findingsSummary(state: AuditState) -> str:
        deadline = state.get("effectiveDeadline")
        lines = [f"Effective deadline: {deadline.isoformat() if deadline else 'UNRESOLVED'}"]
        lines += [f"Rule: [{r.category.value}] {r.ruleText}" for r in state.get("rules", [])[:30]]
        lines += [f"Flag {f.severity.value} {f.kind.value}: {f.detail}" for f in state["flags"][:30]]
        lines += [f"Evidence {e.evidenceId} {e.status.value}: {e.url} {e.detail or ''}" for e in state["evidence"][:30]]
        return "\n".join(lines)

    def investigate(state: AuditState) -> AuditState:
        emit("stage", {"stage": "investigate"})
        steps = list(state.get("steps", []))
        if deps.llm is None:
            return {"steps": _addStep(steps, emit, StepOrigin.AGENT, "investigate", "-", "Choose extra steps.", "skipped: no language model")}
        sources, texts = list(state["sources"]), dict(state["texts"])
        candidates, visited = list(state.get("candidateLinks", [])), set(state.get("visited", []))
        evidence, rules, flags = list(state["evidence"]), list(state.get("rules", [])), list(state["flags"])
        budgetExhausted = state.get("budgetExhausted", False)
        inboxAvailable = "readEmails" in deps.tools and bool(state["project"].emailQuery)
        newRules = False
        agentLinks = 0
        for _ in range(MAX_ROUNDS):
            if not deps.budgetLeft():
                budgetExhausted = True
                steps = _addStep(steps, emit, StepOrigin.AGENT, "investigate", "-", "Choose extra steps.", "stopped: time budget exhausted")
                break
            try:
                planned, rejected, injections = planInvestigation(
                    deps.llm, findingsSummary({**state, "flags": flags, "evidence": evidence, "rules": rules}),
                    candidates, inboxAvailable, visited)
            except (LlmError, ValueError) as error:
                steps = _addStep(steps, emit, StepOrigin.AGENT, "investigate", "-", "Choose extra steps.", f"planner failed: {error}")
                break
            flags = _addFlags(flags, [FlagSpec(UncertaintyKind.PROMPT_INJECTION_SUSPECTED, Severity.HIGH,
                                               f"Instruction-like text was ignored while planning: \"{i[:300]}\"", ()) for i in injections], emit)
            if rejected:
                steps = _addStep(steps, emit, StepOrigin.AGENT, "investigate", "-", "Validate the model's proposals.",
                                 f"{rejected} proposed step(s) refused: target not seen in any source, or already visited")
            if not planned:
                steps = _addStep(steps, emit, StepOrigin.AGENT, "investigate", "-", "Choose extra steps.", "agent chose to stop")
                break
            for step in planned:
                if step.action is InvestigationAction.READ_PAGE:
                    record, text = readPage(step.target, sources, texts, candidates)
                    outcome = f"unreachable: {record.detail}"
                    if text:
                        found, problems = _extract(deps, record.sourceId, text)
                        rules.extend(found)
                        newRules = newRules or bool(found)
                        flags = _addFlags(flags, problems, emit)
                        outcome = f"read; {len(found)} rule(s) extracted"
                    steps = _addStep(steps, emit, StepOrigin.AGENT, "readRulesPage", step.target, step.reason, outcome)
                elif step.action is InvestigationAction.CHECK_LINK:
                    agentLinks += 1
                    item = EvidenceItem.model_validate(deps.tools["auditLink"].invoke({"url": step.target, "evidenceId": f"agentLink{agentLinks}"}))
                    evidence.append(item)
                    emit("evidence", {"items": [item.model_dump(mode="json")]})
                    if item.status is not CheckStatus.PASS:
                        flags = _addFlags(flags, [FlagSpec(
                            UncertaintyKind.AUTH_REDIRECT if item.authWallDetected else UncertaintyKind.FAILED_RENDER, Severity.MEDIUM,
                            f"Linked artifact not viewable logged-out: {step.target[:200]} ({item.detail})", (item.evidenceId,))], emit)
                    steps = _addStep(steps, emit, StepOrigin.AGENT, "auditLink", step.target, step.reason, item.status.value)
                else:
                    try:
                        added = readInbox(step.target, 10, sources, texts, candidates)
                    except Exception as error:  # recorded as the step outcome
                        steps = _addStep(steps, emit, StepOrigin.AGENT, "readEmails", step.target, step.reason, f"failed: {error}")
                        continue
                    for sourceId in added:
                        found, problems = _extract(deps, sourceId, texts[sourceId])
                        rules.extend(found)
                        newRules = newRules or bool(found)
                        flags = _addFlags(flags, problems, emit)
                    steps = _addStep(steps, emit, StepOrigin.AGENT, "readEmails", step.target, step.reason, f"{len(added)} new message(s)")
        update: AuditState = {"sources": sources, "texts": texts, "candidateLinks": candidates, "visited": sorted(visited),
                              "evidence": evidence, "rules": rules, "flags": flags, "steps": steps, "budgetExhausted": budgetExhausted}
        if newRules:
            update.update(_reconcile({**state, **update}, emit))
        return update

    def reflect(state: AuditState) -> AuditState:
        """Second pass over the agent's own findings before reporting."""
        emit("stage", {"stage": "reflect"})
        project = state["project"]
        merged: AuditState = {**state, "steps": list(state.get("steps", [])), "flags": list(state["flags"]),
                              "evidence": list(state["evidence"]), "sources": list(state["sources"]), "texts": dict(state["texts"]),
                              "rules": list(state.get("rules", [])), "candidateLinks": list(state.get("candidateLinks", [])),
                              "visited": list(state.get("visited", []))}
        visited = set(merged["visited"])
        rulesChanged = False

        # 1. Submission links that failed: re-check once, then look for a public alternative on the same site.
        for position, link in enumerate(project.links, start=1):
            evidenceId = f"link{position}"
            current = next((e for e in merged["evidence"] if e.evidenceId == evidenceId), None)
            if current is None or current.status is CheckStatus.PASS:
                continue
            retry = EvidenceItem.model_validate(deps.tools["auditLink"].invoke({"url": link.url, "evidenceId": evidenceId}))
            merged["evidence"] = [retry if e.evidenceId == evidenceId else e for e in merged["evidence"]]
            if retry.status is CheckStatus.PASS:
                merged["flags"] = [f for f in merged["flags"] if evidenceId not in f.sourceIds]
                merged["steps"] = _addStep(merged["steps"], emit, StepOrigin.FOLLOW_UP, "auditLink", link.url,
                                           "Re-check a failed submission link.", "passed on re-check; flag cleared (earlier failure was transient)")
                continue
            note = f"Re-checked: still failing ({retry.detail})."
            alternatives = [c for c in dict.fromkeys(merged["candidateLinks"]) if c not in visited and isLikelyAlternative(link.url, c)]
            found = False
            for alternative in alternatives[:MAX_ALTERNATIVE_CHECKS]:
                visited.add(alternative)
                altItem = EvidenceItem.model_validate(deps.tools["auditLink"].invoke({"url": alternative, "evidenceId": f"{evidenceId}alt"}))
                if altItem.status is CheckStatus.PASS:
                    note += f" Public alternative found: {alternative}. Replace the submitted link with it."
                    found = True
                    break
            if alternatives and not found:
                note += " No public alternative found among links in the sources."
            merged["flags"] = [f.model_copy(update={"followUp": note[:2000]}) if evidenceId in f.sourceIds else f for f in merged["flags"]]
            merged["steps"] = _addStep(merged["steps"], emit, StepOrigin.FOLLOW_UP, "auditLink", link.url, "Re-check a failed submission link.", note)

        # 2. Rules pages that were unreachable: retry once.
        for flag in [f for f in merged["flags"] if f.kind is UncertaintyKind.UNREACHABLE_SOURCE and f.sourceIds]:
            location = _sourceLocation(merged, flag.sourceIds[0])
            if not location.startswith(("http://", "https://")):
                continue
            record, text = readPage(location, merged["sources"], merged["texts"], merged["candidateLinks"])
            if text:
                found, problems = _extract(deps, record.sourceId, text)
                merged["rules"] = [*merged["rules"], *found]
                rulesChanged = True
                merged["flags"] = _addFlags([f for f in merged["flags"] if f.flagId != flag.flagId], problems, emit)
                outcome = f"reachable on retry; {len(found)} rule(s)"
            else:
                outcome = "still unreachable"
            merged["steps"] = _addStep(merged["steps"], emit, StepOrigin.FOLLOW_UP, "readRulesPage", location, "Retry an unreachable rules page.", outcome)
        if rulesChanged:
            merged.update(_reconcile(merged, emit))

        # 3. Deadline still unresolved: search the inbox, then ask the human (or reuse an earlier answer).
        if merged.get("effectiveDeadline") is None and len(_deadlineCandidates(merged)) >= 2:
            query = f"{project.emailQuery} deadline"[:120] if project.emailQuery else ""
            if query and "readEmails" in deps.tools and f"inbox:{query}" not in visited:
                visited.add(f"inbox:{query}")
                try:
                    added = readInbox(query, 10, merged["sources"], merged["texts"], merged["candidateLinks"])
                    newRules = 0
                    for sourceId in added:
                        found, problems = _extract(deps, sourceId, merged["texts"][sourceId])
                        merged["rules"] = [*merged["rules"], *found]
                        merged["flags"] = _addFlags(merged["flags"], problems, emit)
                        newRules += len(found)
                    merged.update(_reconcile(merged, emit))
                    outcome = f"{len(added)} new message(s), {newRules} new rule(s)"
                except Exception as error:  # recorded, then fall through to asking
                    outcome = f"failed: {error}"
                merged["steps"] = _addStep(merged["steps"], emit, StepOrigin.FOLLOW_UP, "readEmails", query,
                                           "Look for the newest deadline email before asking you.", outcome)
            candidates = _deadlineCandidates(merged)
            if merged.get("effectiveDeadline") is None and len(candidates) >= 2:
                options = tuple(QuestionOption(
                    value=deadline.isoformat()[:64],
                    label=(f"{deadline.isoformat()} from {_sourceLocation(merged, rule.sourceId)[:200]} "
                           f"({'issued ' + issued.isoformat() if issued else 'undated'}): \"{rule.verbatimQuote[:200]}\""))
                    for deadline, rule, issued in candidates[:6])
                key = _fingerprint("DEADLINE_CHOICE", *[o.value for o in options])
                question = AgentQuestion(
                    questionId=f"q{len(merged.get('questions', [])) + 1}", questionKey=key, kind=QuestionKind.DEADLINE_CHOICE,
                    prompt="Sources disagree on the submission deadline and recency cannot settle it. Which deadline is authoritative?",
                    options=(*options, QuestionOption(value=UNSURE_VALUE, label="I am not sure; keep it flagged.")), askedAt=utcNow())
                remembered = project.memory.decisionFor(key)
                if remembered is not None:
                    merged.update(_applyAnswer(merged, question.model_copy(update={
                        "answer": remembered.answer, "answeredBy": remembered.decidedBy, "answeredFromMemory": True}), emit))
                    merged["steps"] = _addStep(merged["steps"], emit, StepOrigin.MEMORY, "memory", question.questionId,
                                               "The same question was answered in an earlier audit.", f"reused answer: {remembered.answer.label[:300]}")
                    merged["pendingQuestion"] = None
                else:
                    merged["pendingQuestion"] = question
        merged["visited"] = sorted(visited)
        return {k: merged[k] for k in STATE_KEYS_FROM_REFLECT if k in merged}

    def ask(state: AuditState) -> AuditState:
        """Pauses the graph. Resumes with {"value": optionValue, "actor": actorJson} supplied by an authenticated human."""
        question = state.get("pendingQuestion")
        if question is None:
            raise RuntimeError("ask node reached without a pending question")
        emit("question", question.model_dump(mode="json"))
        reply = interrupt(question.model_dump(mode="json"))
        actor = Actor.model_validate(reply["actor"])
        if not actor.isHuman:
            raise ValueError("only a human can answer the agent's question")
        answered = question.model_copy(update={"answer": question.option(str(reply["value"])), "answeredBy": actor})
        update = _applyAnswer(state, answered, emit)
        decisions = [*state.get("decisions", []), HumanDecision(questionKey=question.questionKey, question=question.prompt,
                                                                answer=answered.answer, decidedBy=actor, decidedAt=utcNow())]
        steps = _addStep(state.get("steps", []), emit, StepOrigin.MEMORY, "askHuman", question.questionId, question.prompt,
                         f"answered: {answered.answer.label[:300]}")
        return {**update, "decisions": decisions, "steps": steps, "pendingQuestion": None}

    def remediate(state: AuditState) -> AuditState:
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
        memory = state["project"].memory
        present = {s.kind for s in state["sources"] if s.status is SourceStatus.CHECKED}
        specs = [FlagSpec(UncertaintyKind.MISSING_SOURCE_CATEGORY, Severity.MEDIUM,
                          f"No checked source of type {kind.value}; coverage is incomplete.", ())
                 for kind in REQUIRED_SOURCE_KINDS if kind not in present]
        flags: list[UncertaintyFlag] = []
        for flag in _addFlags(state["flags"], specs, emit):
            risk = memory.acceptedRiskFor(flag)
            flags.append(flag.model_copy(update={"acceptedReason": f"Accepted earlier: {risk.reason}"[:512], "acceptedBy": risk.acceptedBy,
                                                 "acceptedAt": risk.acceptedAt}) if risk and not flag.isAccepted else flag)
        index = computeConfusionIndex([f for f in flags if not f.isAccepted])
        if index > deps.confusionThreshold and not state["halted"]:
            flags = _addFlags(flags, [FlagSpec(
                UncertaintyKind.CONFUSION_THRESHOLD_EXCEEDED, Severity.CRITICAL,
                f"Confusion index {index} exceeds threshold {deps.confusionThreshold} after auditing. "
                "Resolve the flagged items and re-run before approving.", ())], emit)
        emit("confusion", {"index": index, "threshold": deps.confusionThreshold, "halted": state["halted"]})
        return {"flags": flags, "confusionIndex": index}

    graph = StateGraph(AuditState)
    for name, node in (("ingest", ingest), ("extract", extract), ("reconcile", reconcile), ("plan", plan), ("audit", audit),
                       ("investigate", investigate), ("reflect", reflect), ("ask", ask), ("remediate", remediate), ("finalize", finalize)):
        graph.add_node(name, node)
    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "extract")
    graph.add_edge("extract", "reconcile")
    graph.add_conditional_edges("reconcile", lambda s: "finalize" if s["halted"] else "plan", {"finalize": "finalize", "plan": "plan"})
    graph.add_edge("plan", "audit")
    graph.add_edge("audit", "investigate")
    graph.add_edge("investigate", "reflect")
    graph.add_conditional_edges("reflect", lambda s: "ask" if s.get("pendingQuestion") else "remediate", {"ask": "ask", "remediate": "remediate"})
    graph.add_edge("ask", "remediate")
    graph.add_edge("remediate", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=deps.checkpointer if deps.checkpointer is not None else newCheckpointer())


# ---------------------------------------------------------------- run / resume


@dataclass(frozen=True)
class AuditOutcome:
    summary: AuditSummary
    packet: Optional[SubmissionPacket]
    question: Optional[AgentQuestion]
    proofChecks: tuple[ProofCheck, ...]
    decisions: tuple[HumanDecision, ...]
    startProject: Project

    @property
    def paused(self) -> bool:
        return self.question is not None


def _summary(state: AuditState, project: Project, packetId: str, deps: AuditDependencies, startedAt: datetime,
             finished: bool, diff: Any = None) -> AuditSummary:
    pending = state.get("pendingQuestion")
    return AuditSummary(
        packetId=packetId, projectId=project.projectId, startedAt=startedAt, finishedAt=utcNow() if finished else None,
        plan=tuple(state.get("plan", [])), rules=tuple(state.get("rules", [])), effectiveDeadline=state.get("effectiveDeadline"),
        confusionIndex=state.get("confusionIndex", 0), confusionThreshold=deps.confusionThreshold, halted=state.get("halted", False),
        remediations=tuple(state.get("remediations", [])), agentSteps=tuple(state.get("steps", [])),
        questions=tuple(state.get("questions", [])) + ((pending,) if pending else ()),
        diff=diff, llmModel=deps.llmModel, budgetSeconds=deps.budgetSeconds, budgetExhausted=state.get("budgetExhausted", False),
    )


def initialState(project: Project, packetId: str) -> AuditState:
    return {"project": project, "packetId": packetId, "flags": [], "evidence": [], "reports": [], "plan": [],
            "proofChecks": list(project.proofChecks), "repoAudit": None, "halted": False, "confusionIndex": 0,
            "features": [], "alignmentAvailable": False, "remediations": [], "steps": [], "visited": [],
            "candidateLinks": [], "deadlineFlagIds": [], "pendingQuestion": None, "questions": [],
            "decisions": [], "budgetExhausted": False}


def runAudit(project: Project, packetId: str, deps: AuditDependencies, startedAt: datetime,
             resume: Optional[dict[str, Any]] = None) -> AuditOutcome:
    """Starts (resume=None) or resumes an audit. Returns a paused outcome with a question, or a finished packet."""
    graph = buildAuditGraph(deps)
    config = {"configurable": {"thread_id": packetId}}
    result = graph.invoke(initialState(project, packetId) if resume is None else Command(resume=resume), config)
    state: AuditState = graph.get_state(config).values
    project = state.get("project", project)  # the snapshot taken when the audit started, also after a pause
    if result.get("__interrupt__"):
        question = state.get("pendingQuestion")
        if question is None:
            raise RuntimeError("graph paused without a question")
        return AuditOutcome(summary=_summary(state, project, packetId, deps, startedAt, finished=False), packet=None,
                            question=question, proofChecks=tuple(state.get("proofChecks", project.proofChecks)),
                            decisions=tuple(state.get("decisions", [])), startProject=project)
    proofChecks = tuple(state.get("proofChecks", project.proofChecks))
    packet = SubmissionPacket(
        packetId=packetId, approvalState=ApprovalState.AUDITING, charter=project.charter, proofChecks=proofChecks,
        workbenchChecks=project.workbenchChecks, ledger=CoverageLedger(sources=tuple(state["sources"])),
        evidence=tuple(state["evidence"]), uncertaintyFlags=tuple(state["flags"]), reports=tuple(state["reports"]),
    )
    packet = transitionPacket(packet, ApprovalState.PENDING_HUMAN_APPROVAL, AGENT_ACTOR)
    diff = computeDiff(deps.previousPacket, list(state["flags"]), list(state["evidence"]))
    return AuditOutcome(summary=_summary(state, project, packetId, deps, startedAt, finished=True, diff=diff), packet=packet,
                        question=None, proofChecks=proofChecks, decisions=tuple(state.get("decisions", [])), startProject=project)
