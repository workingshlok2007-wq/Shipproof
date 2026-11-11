"use client";

import { useState } from "react";
import type { ApiClient } from "@/lib/api";
import type { AgentQuestion, AgentStep, AuditDiff, StepOrigin } from "@/lib/types";
import { Button, Card, Empty, ErrorNote } from "./ui";
import { useAction } from "./useAction";

const ORIGIN_STYLE: Record<StepOrigin, [string, string]> = {
  BASELINE: ["baseline", "bg-unverified/12 text-unverified"],
  AGENT: ["agent chose", "bg-accent text-white"],
  FOLLOW_UP: ["follow-up", "bg-stale/15 text-stale"],
  MEMORY: ["memory", "bg-pass/12 text-pass"],
};

/** The agent paused and needs a decision only a human can make. */
export function QuestionCard({ client, packetId, question, onAnswered }: {
  client: ApiClient; packetId: string; question: AgentQuestion; onAnswered: () => void;
}) {
  const [choice, setChoice] = useState("");
  const { busy, error, run } = useAction();
  const submit = async () => {
    const result = await run(() => client.answerQuestion(packetId, question.questionId, choice));
    if (result) onAnswered();
  };
  return (
    <section className="clay border-2 border-accent/60 p-6" aria-label="Agent question">
      <p className="text-xs font-semibold uppercase tracking-wide text-accent-deep">The agent paused and needs your decision</p>
      <h2 className="mt-1 text-lg font-semibold">{question.prompt}</h2>
      <fieldset className="mt-4 space-y-2">
        <legend className="sr-only">Options</legend>
        {question.options.map((option) => (
          <label key={option.value} className={`clay-inset flex cursor-pointer items-start gap-3 p-3 text-sm ${choice === option.value ? "ring-2 ring-accent" : ""}`}>
            <input type="radio" name={`q-${question.questionId}`} className="mt-1" value={option.value}
                   checked={choice === option.value} onChange={() => setChoice(option.value)} />
            <span className="break-words">{option.label}</span>
          </label>
        ))}
      </fieldset>
      <p className="mt-3 text-[11px] text-muted">Your answer is saved to project memory, so a later audit with the same conflict will not ask again.</p>
      <Button className="mt-3" variant="primary" disabled={!choice || busy} onClick={submit}>{busy ? "Resuming audit…" : "Answer and resume"}</Button>
      <ErrorNote message={error} />
    </section>
  );
}

export function AgentTrace({ steps, model, budgetSeconds, budgetExhausted }: {
  steps: AgentStep[]; model: string | null; budgetSeconds: number; budgetExhausted: boolean;
}) {
  const chosen = steps.filter((s) => s.origin === "AGENT" && s.tool !== "investigate").length;
  return (
    <Card title="Agent trace" subtitle={`Every tool call and why. ${chosen} step(s) chosen by the model · model: ${model ?? "none"} · budget ${budgetSeconds}s${budgetExhausted ? " (exhausted)" : ""}`}>
      {steps.length === 0 ? <Empty>No steps yet.</Empty> : (
        <ol className="space-y-1.5">
          {steps.map((step) => {
            const [label, style] = ORIGIN_STYLE[step.origin];
            return (
              <li key={step.stepId} className="clay-inset grid gap-1 px-3 py-2 text-xs sm:grid-cols-[6.5rem_9rem_1fr]">
                <span className={`h-fit w-fit rounded-full px-2 py-0.5 text-[10px] font-semibold ${style}`}>{label}</span>
                <span className="font-mono font-semibold">{step.tool}</span>
                <span className="min-w-0">
                  <span className="block break-all text-muted">{step.target}</span>
                  <span className="block">{step.reason}</span>
                  {step.outcome && <span className="block text-ink/70">→ {step.outcome}</span>}
                </span>
              </li>
            );
          })}
        </ol>
      )}
    </Card>
  );
}

export function DiffCard({ diff }: { diff: AuditDiff | null }) {
  if (!diff) return null;
  const groups: [string, string[], string][] = [
    ["Fixed", diff.fixedEvidence, "text-pass"],
    ["Regressed", diff.regressedEvidence, "text-fail"],
    ["New flags", diff.newFlags, "text-fail"],
    ["Resolved flags", diff.resolvedFlags, "text-pass"],
    ["Still failing", diff.stillFailing, "text-stale"],
  ];
  const empty = groups.every(([, items]) => items.length === 0);
  return (
    <Card title="Since the last audit" subtitle={`Compared with ${diff.previousPacketId}.`}>
      {empty ? <Empty>No changes.</Empty> : (
        <div className="grid gap-4 sm:grid-cols-2">
          {groups.filter(([, items]) => items.length).map(([title, items, tone]) => (
            <div key={title}>
              <p className={`text-xs font-semibold ${tone}`}>{title} ({items.length})</p>
              <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs">{items.map((item) => <li key={item} className="break-words">{item}</li>)}</ul>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
