"use client";

import { useEffect, useMemo, useState } from "react";
import type { ApiClient } from "@/lib/api";
import type { AuditEvent, PacketView, Remediation, Severity, SubmissionPacket, UncertaintyFlag } from "@/lib/types";
import type { StreamMode } from "@/lib/useAuditStream";
import { Button, Card, Empty, ErrorNote, SafeLink, SeverityBadge, StatusBadge, formatTime } from "./ui";
import { AgentTrace, DiffCard, QuestionCard } from "./AgentPanels";
import { RemediationPanel } from "./RemediationPanel";
import { useAction } from "./useAction";

const STAGES = ["queued", "ingest", "extract", "reconcile", "plan", "audit", "investigate", "reflect", "remediate", "finalize"] as const;
const SEVERITY_ORDER: Record<Severity, number> = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 };

function StageTimeline({ events, finished, halted }: { events: AuditEvent[]; finished: boolean; halted: boolean }) {
  const reached = new Set(events.filter((e) => e.kind === "stage").map((e) => String(e.data.stage)));
  const current = [...events].reverse().find((e) => e.kind === "stage")?.data.stage;
  return (
    <ol className="flex flex-wrap items-center gap-2" aria-label="Audit stages">
      {STAGES.map((stage) => {
        const skipped = halted && ["plan", "audit", "investigate", "reflect", "remediate"].includes(stage);
        const done = (reached.has(stage) && (stage !== current || finished)) || (finished && !skipped && reached.has(stage));
        const active = stage === current && !finished;
        return (
          <li key={stage} className={`rounded-full px-3 py-1 text-xs font-medium ${
            skipped ? "bg-sunken text-muted line-through" : active ? "bg-accent text-white" : done ? "bg-pass/12 text-pass" : "clay-inset text-muted"}`}
              aria-current={active ? "step" : undefined}>
            {stage}
          </li>
        );
      })}
    </ol>
  );
}

function ConfusionMeter({ index, threshold, halted }: { index: number; threshold: number; halted: boolean }) {
  const over = index > threshold;
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <span className="text-xs font-medium text-muted">Confusion index (structural signals only)</span>
        <span className={`text-2xl font-semibold tabular-nums ${over ? "text-fail" : "text-ink"}`}>{index}<span className="text-sm text-muted"> / 100</span></span>
      </div>
      <div className="clay-inset relative mt-2 h-3 overflow-hidden" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={index}>
        <div className={`h-full rounded-full ${over ? "bg-fail" : "bg-accent"}`} style={{ width: `${Math.min(100, index)}%` }} />
        <div className="absolute inset-y-0 w-0.5 bg-ink/60" style={{ left: `${threshold}%` }} title={`threshold ${threshold}`} />
      </div>
      <p className="mt-1.5 text-[11px] text-muted">
        Threshold {threshold}. {halted ? "Agent halted before auditing and needs your input." : over ? "Above threshold: resolve flags and re-run before approving." : "Within threshold."}
      </p>
    </div>
  );
}

function approvalBlockers(packet: SubmissionPacket): string[] {
  const blockers: string[] = [];
  if (packet.approvalState !== "PENDING_HUMAN_APPROVAL") blockers.push(`Packet is ${packet.approvalState.replaceAll("_", " ").toLowerCase()}.`);
  if (!packet.ledger.humanConfirmedComplete) blockers.push("Coverage ledger not confirmed complete.");
  if (packet.proofChecks.some((c) => c.status === "STALE")) blockers.push("STALE proof checks need re-confirmation.");
  if (packet.proofChecks.some((c) => c.promotedBy && c.status === "FAIL")) blockers.push("A promoted proof check reports a possible misreading.");
  const critical = packet.uncertaintyFlags.filter((f) => f.severity === "CRITICAL" && !f.acceptedBy).length;
  if (critical) blockers.push(`${critical} CRITICAL flag(s) not accepted.`);
  return blockers;
}

function FlagItem({ flag, canAccept, onAccept, busy }: {
  flag: UncertaintyFlag; canAccept: boolean; onAccept: (reason: string) => void; busy: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  return (
    <li className={`clay-inset p-3 ${flag.acceptedBy ? "opacity-75" : ""}`}>
      <div className="flex flex-wrap items-center gap-2">
        <SeverityBadge severity={flag.severity} />
        <span className="font-mono text-[11px] text-muted">{flag.kind}</span>
        {flag.acceptedBy && <span className="rounded-full bg-pass/12 px-2 py-0.5 text-[10px] font-semibold text-pass">ACCEPTED</span>}
      </div>
      <p className="mt-1.5 whitespace-pre-wrap break-words text-sm">{flag.detail}</p>
      {flag.followUp && <p className="mt-1.5 border-l-2 border-stale/50 pl-2 text-xs"><span className="font-semibold">Agent follow-up:</span> {flag.followUp}</p>}
      {flag.acceptedReason && <p className="mt-1.5 text-xs text-pass">Risk accepted by {flag.acceptedBy?.actorId}: {flag.acceptedReason}</p>}
      {canAccept && !flag.acceptedBy && flag.severity === "CRITICAL" && (
        open ? (
          <div className="mt-2 flex flex-wrap gap-2">
            <input className="field min-w-0 flex-1" maxLength={500} placeholder="Why is this risk acceptable?" value={reason}
                   onChange={(e) => setReason(e.target.value)} />
            <Button variant="danger" disabled={busy || !reason.trim()} onClick={() => onAccept(reason.trim())}>Accept risk</Button>
            <Button onClick={() => setOpen(false)}>Cancel</Button>
          </div>
        ) : <button type="button" className="mt-2 text-xs font-medium text-fail underline-offset-2 hover:underline" onClick={() => setOpen(true)}>Accept this risk with a reason…</button>
      )}
    </li>
  );
}

export function AuditView({ client, view, events, streamMode, streamError, onPacket, onRemediation, onRefresh }: {
  client: ApiClient; view: PacketView; events: AuditEvent[]; streamMode: StreamMode; streamError: string | null;
  onPacket: (packet: SubmissionPacket) => void; onRemediation: (remediation: Remediation) => void; onRefresh: () => void;
}) {
  const { packet, summary } = view;
  const { busy, error, run } = useAction();
  const [reviewed, setReviewed] = useState(false);
  useEffect(() => setReviewed(false), [packet.packetId]);

  const awaiting = packet.approvalState === "AWAITING_INPUT";
  const auditing = packet.approvalState === "AUDITING" || awaiting;
  const pendingQuestion = summary?.questions.find((q) => q.answer === null) ?? null;
  const deadlineAnswer = summary?.questions.find((q) => q.kind === "DEADLINE_CHOICE" && q.answer && q.answer.value !== "unsure") ?? null;
  const liveConfusion = [...events].reverse().find((e) => e.kind === "confusion")?.data;
  const confusionIndex = summary?.confusionIndex ?? (typeof liveConfusion?.index === "number" ? liveConfusion.index : 0);
  const threshold = summary?.confusionThreshold ?? (typeof liveConfusion?.threshold === "number" ? liveConfusion.threshold : 15);
  const halted = summary?.halted ?? liveConfusion?.halted === true;
  const flags = useMemo(() => [...packet.uncertaintyFlags].sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]), [packet.uncertaintyFlags]);
  const blockers = approvalBlockers(packet);
  const report = packet.reports[0];
  const decided = packet.approvalState === "APPROVED" || packet.approvalState === "REJECTED";

  const act = async (action: () => Promise<SubmissionPacket>) => {
    const updated = await run(action);
    if (updated) onPacket(updated);
  };

  return (
    <div className="space-y-6">
      <Card title={`Audit ${packet.packetId}`} subtitle={summary ? `Started ${formatTime(summary.startedAt)} · finished ${formatTime(summary.finishedAt)}` : "Running"}
            action={<span className="text-[11px] text-muted">stream: {streamMode}</span>}>
        <StageTimeline events={events} finished={!auditing} halted={halted} />
        {awaiting && <p className="mt-3 text-sm font-medium text-accent-deep">Paused: waiting for your answer below.</p>}
        <ErrorNote message={streamError} />
        <div className="mt-6 grid gap-6 lg:grid-cols-2">
          <ConfusionMeter index={confusionIndex} threshold={threshold} halted={halted} />
          <div>
            <p className="text-xs font-medium text-muted">Effective deadline</p>
            <p className="mt-1 text-lg font-semibold">{summary?.effectiveDeadline ? formatTime(summary.effectiveDeadline) : "Unresolved"}</p>
            <p className="text-[11px] text-muted">{deadlineAnswer
              ? `Set by ${deadlineAnswer.answeredFromMemory ? "your earlier answer (from memory)" : "your answer"}.`
              : "Resolved by recency only when every competing rule is dated."}</p>
          </div>
        </div>
        {summary && summary.plan.length > 0 && (
          <div className="mt-6">
            <p className="mb-2 text-xs font-medium text-muted">Agent plan</p>
            <ol className="space-y-1.5">
              {summary.plan.map((task, position) => (
                <li key={`${task.tool}-${position}`} className="clay-inset flex flex-wrap gap-x-3 px-3 py-2 text-xs">
                  <span className="font-mono font-semibold">{task.tool}</span>
                  <span className="break-all">{task.target}</span>
                  <span className="text-muted">{task.reason}</span>
                </li>
              ))}
            </ol>
          </div>
        )}
      </Card>

      {awaiting && pendingQuestion && (
        <QuestionCard client={client} packetId={packet.packetId} question={pendingQuestion} onAnswered={onRefresh} />
      )}

      {summary && (
        <AgentTrace steps={summary.agentSteps} model={summary.llmModel} budgetSeconds={summary.budgetSeconds} budgetExhausted={summary.budgetExhausted} />
      )}
      <DiffCard diff={summary?.diff ?? null} />

      <div className="grid gap-6 xl:grid-cols-2">
        <Card title="Uncertainty flags" subtitle="What the agent could not confirm. CRITICAL flags block approval unless you accept them with a reason.">
          {flags.length === 0 ? <Empty>{auditing ? "Waiting for the audit." : "No flags raised."}</Empty> : (
            <ul className="space-y-2">
              {flags.map((flag) => (
                <FlagItem key={flag.flagId} flag={flag} busy={busy} canAccept={packet.approvalState === "PENDING_HUMAN_APPROVAL"}
                          onAccept={(reason) => act(() => client.acceptRisk(packet.packetId, flag.flagId, reason))} />
              ))}
            </ul>
          )}
        </Card>

        <Card title="Evidence ledger" subtitle="Checked as a logged-out visitor.">
          {packet.evidence.length === 0 ? <Empty>{auditing ? "Collecting evidence." : "No evidence collected."}</Empty> : (
            <ul className="space-y-2">
              {packet.evidence.map((item) => (
                <li key={item.evidenceId} className="clay-inset p-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-xs font-semibold">{item.evidenceId}</span>
                    <StatusBadge status={item.status} />
                  </div>
                  <div className="mt-1"><SafeLink href={item.url} /></div>
                  <p className="mt-1 text-xs text-muted">
                    {item.method.replaceAll("_", " ").toLowerCase()} · status {item.finalStatus ?? "none"}{item.authWallDetected ? " · login wall" : ""}
                  </p>
                  {item.detail && <p className="mt-1 text-sm">{item.detail}</p>}
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <div className="grid gap-6 xl:grid-cols-2">
        <Card title="Scope alignment" subtitle={report ? report.caveat : "Promised features with code evidence / all promised features."}>
          {report ? (
            <div>
              <p className="text-4xl font-semibold tabular-nums">{report.alignmentScore}<span className="text-base text-muted"> / 100</span></p>
              <p className="mt-1 text-[11px] text-muted">Basis: {report.evidenceBasis.join(", ").toLowerCase()}</p>
              <p className="mt-4 text-xs font-medium text-muted">Promised but no code evidence</p>
              {report.missingFeatures.length === 0 ? <p className="text-sm">None.</p> : (
                <ul className="mt-1 list-disc space-y-1 pl-5 text-sm">{report.missingFeatures.map((f) => <li key={f}>{f}</li>)}</ul>
              )}
            </div>
          ) : <Empty>{auditing ? "Pending." : "No alignment report. See flags for the reason."}</Empty>}
        </Card>

        <Card title="Coverage ledger" subtitle="The agent never claims completeness. You confirm whether these are all the rule sources."
              action={!decided && packet.approvalState === "PENDING_HUMAN_APPROVAL" && !packet.ledger.humanConfirmedComplete ? (
                <Button variant="primary" disabled={busy} onClick={() => act(() => client.confirmCoverage(packet.packetId))}>This is everything</Button>
              ) : undefined}>
          {packet.ledger.sources.length === 0 ? <Empty>No sources yet.</Empty> : (
            <ul className="space-y-2">
              {packet.ledger.sources.map((source) => (
                <li key={source.sourceId} className="clay-inset flex items-start justify-between gap-3 p-3">
                  <div className="min-w-0">
                    <p className="text-xs font-semibold">{source.kind.replaceAll("_", " ").toLowerCase()}</p>
                    <p className="break-all text-xs text-muted">{source.location}</p>
                    {source.detail && <p className="mt-0.5 text-xs">{source.detail}</p>}
                  </div>
                  <StatusBadge status={source.status} />
                </li>
              ))}
            </ul>
          )}
          {packet.ledger.humanConfirmedComplete && (
            <p className="mt-3 text-xs text-pass">Confirmed complete by {packet.ledger.confirmedBy?.actorId} at {formatTime(packet.ledger.confirmedAt)}.</p>
          )}
          <p className="mt-3 text-[11px] text-muted">Not covered automatically: Discord, mentor comments, login-gated pages, emails to other addresses.</p>
        </Card>
      </div>

      <RemediationPanel client={client} packetId={packet.packetId} remediations={summary?.remediations ?? []} auditing={auditing}
                        onUpdated={onRemediation} />

      {summary && summary.rules.length > 0 && (
        <Card title="Extracted rules" subtitle="Each rule quotes its source verbatim; unquotable rules were dropped.">
          <ul className="space-y-2">
            {summary.rules.map((rule, position) => (
              <li key={`${rule.sourceId}-${position}`} className="clay-inset p-3 text-sm">
                <div className="flex flex-wrap gap-2 text-[11px] text-muted">
                  <span className="font-semibold">{rule.category}</span>
                  {rule.deadlineAt && <span>deadline {formatTime(rule.deadlineAt)}</span>}
                  {rule.dateAmbiguous && <span className="text-stale">ambiguous date</span>}
                  {rule.supersedesEarlierRule && <span className="text-stale">correction</span>}
                </div>
                <p className="mt-1">{rule.ruleText}</p>
                <p className="mt-1 border-l-2 border-accent/40 pl-2 text-xs italic text-muted">{rule.verbatimQuote}</p>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <section className="clay sticky bottom-4 z-10 p-5" aria-label="Approval boundary">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <p className="text-xs font-medium text-muted">Human approval boundary</p>
            <p className="text-lg font-semibold">{packet.approvalState.replaceAll("_", " ")}</p>
            {decided && <p className="text-xs text-muted">by {packet.decidedBy?.actorId} at {formatTime(packet.decidedAt)}</p>}
          </div>
          {!decided && (
            <div className="flex flex-wrap items-center gap-3">
              <label className="flex items-center gap-2 text-xs">
                <input type="checkbox" checked={reviewed} onChange={(e) => setReviewed(e.target.checked)} disabled={auditing} />
                I reviewed every flag and evidence item.
              </label>
              <Button variant="danger" disabled={busy || auditing} onClick={() => act(() => client.reject(packet.packetId))}>Reject</Button>
              <Button variant="primary" disabled={busy || auditing || !reviewed || blockers.length > 0} onClick={() => act(() => client.approve(packet.packetId))}>Approve</Button>
            </div>
          )}
        </div>
        {!decided && blockers.length > 0 && !auditing && (
          <ul className="mt-3 flex flex-wrap gap-2">
            {blockers.map((b) => <li key={b} className="rounded-full bg-fail/10 px-3 py-1 text-[11px] text-fail">{b}</li>)}
          </ul>
        )}
        <ErrorNote message={error} />
      </section>
    </div>
  );
}
