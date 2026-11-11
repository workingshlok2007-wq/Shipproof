// Mirrors the JSON emitted by shipproof.models / shipproof.agent. All string content is untrusted external data:
// render it as React text only, never as HTML.

export type CheckStatus = "PASS" | "FAIL" | "STALE" | "UNVERIFIED";
export type ApprovalState = "DRAFT" | "AUDITING" | "PENDING_HUMAN_APPROVAL" | "APPROVED" | "REJECTED";
export type SourceStatus = "CHECKED" | "UNREACHABLE" | "NOT_CHECKED";
export type Severity = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
export type SourceRefKind = "URL" | "EMAIL_MESSAGE_ID";

export interface Actor { actorId: string; kind: "HUMAN" | "AGENT"; role: string | null }
export interface SourceRef { kind: SourceRefKind; value: string }

export interface CharterClause { clauseId: string; version: number; text: string; sourceRef: SourceRef; effectiveAt: string }
export interface CharterAmendment {
  amendmentId: string; clauseId: string; newText: string; sourceRef: SourceRef; submittedBy: Actor; submittedAt: string;
}
export interface Charter { clauses: CharterClause[]; amendments: CharterAmendment[] }

export interface ProofCheck {
  checkId: string; clauseId: string; clauseVersion: number; interpretation: string; status: CheckStatus;
  authoredBy: Actor; promotedBy: Actor | null; promotedAt: string | null;
}
export interface WorkbenchCheck {
  checkId: string; tier: number; label: string; description: string | null; status: CheckStatus; authoredBy: Actor;
}

export interface SourceRecord {
  sourceId: string; kind: string; location: string; status: SourceStatus; sourceDate: string | null;
  retrievedAt: string | null; contentSha256: string | null; detail: string | null;
}
export interface CoverageLedger {
  sources: SourceRecord[]; humanConfirmedComplete: boolean; confirmedBy: Actor | null; confirmedAt: string | null;
}
export interface EvidenceItem {
  evidenceId: string; url: string; method: string; finalStatus: number | null; finalUrl: string | null;
  authWallDetected: boolean; status: CheckStatus; checkedAt: string; detail: string | null;
}
export interface UncertaintyFlag {
  flagId: string; kind: string; detail: string; severity: Severity; sourceIds: string[]; raisedAt: string;
}
export interface AlignmentReport {
  alignmentScore: number; missingFeatures: string[]; evidenceBasis: string[]; caveat: string; generatedAt: string;
}
export interface SubmissionPacket {
  packetId: string; approvalState: ApprovalState; charter: Charter; proofChecks: ProofCheck[];
  workbenchChecks: WorkbenchCheck[]; ledger: CoverageLedger; evidence: EvidenceItem[];
  uncertaintyFlags: UncertaintyFlag[]; reports: AlignmentReport[]; decidedBy: Actor | null; decidedAt: string | null;
}
export interface PlannedTask { tool: string; target: string; reason: string }
export interface GroundedRule {
  sourceId: string; ruleText: string; category: string; verbatimQuote: string; issuedAt: string | null;
  deadlineAt: string | null; dateAmbiguous: boolean; supersedesEarlierRule: boolean;
}
export type RemediationKind = "ADD_LICENSE" | "IMPROVE_README" | "FIX_README_LINKS" | "MISSING_FEATURE_GUIDANCE";
export type RemediationStatus = "PROPOSED" | "APPLIED" | "DISMISSED";
export interface Remediation {
  remediationId: string; kind: RemediationKind; title: string; rationale: string;
  changes: { path: string; newContent: string }[]; advice: string[]; requiredInputs: string[]; applicable: boolean;
  status: RemediationStatus; pullRequestUrl: string | null; draftedFrom: string;
}

export interface AuditSummary {
  packetId: string; projectId: string; startedAt: string; finishedAt: string | null; plan: PlannedTask[];
  rules: GroundedRule[]; effectiveDeadline: string | null; confusionIndex: number; confusionThreshold: number;
  halted: boolean; error: string | null; remediations: Remediation[];
}
export interface PacketView { packet: SubmissionPacket; summary: AuditSummary | null }

export interface LinkTarget { label: string; url: string }
export interface Project {
  projectId: string; repo: string; proposalText: string; links: LinkTarget[]; rulePages: string[];
  emailQuery: string | null; charter: Charter; proofChecks: ProofCheck[]; workbenchChecks: WorkbenchCheck[];
  packetIds: string[];
}

export interface AuditEvent { packetId: string; kind: string; at: string; data: Record<string, unknown> }
