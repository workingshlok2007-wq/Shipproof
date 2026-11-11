"use client";

import { useMemo, useState, type FormEvent } from "react";
import type { ApiClient } from "@/lib/api";
import type { CharterClause, Project, ProofCheck, SourceRefKind } from "@/lib/types";
import { Button, Card, Empty, ErrorNote, Field, StatusBadge, formatTime } from "./ui";
import { useAction } from "./useAction";

const ID_PATTERN = "[A-Za-z0-9_.:\\-]{1,64}";

function currentClauses(project: Project): Map<string, CharterClause> {
  const latest = new Map<string, CharterClause>();
  for (const clause of project.charter.clauses) latest.set(clause.clauseId, clause);
  return latest;
}

function SourceRefInputs({ kind, setKind, value, setValue }: {
  kind: SourceRefKind; setKind: (k: SourceRefKind) => void; value: string; setValue: (v: string) => void;
}) {
  return (
    <div className="grid grid-cols-[9rem_1fr] gap-2">
      <Field label="Source type">
        <select className="field" value={kind} onChange={(e) => setKind(e.target.value as SourceRefKind)}>
          <option value="URL">URL</option>
          <option value="EMAIL_MESSAGE_ID">Email message id</option>
        </select>
      </Field>
      <Field label="Source reference (required)">
        <input className="field" required value={value} onChange={(e) => setValue(e.target.value)}
               placeholder={kind === "URL" ? "https://event.dev/rules" : "<id@mail.example>"} />
      </Field>
    </div>
  );
}

function CharterColumn({ project, client, onChange }: { project: Project; client: ApiClient; onChange: (p: Project) => void }) {
  const clauses = useMemo(() => currentClauses(project), [project]);
  const [mode, setMode] = useState<"add" | "amend">("add");
  const [clauseId, setClauseId] = useState("");
  const [amendmentId, setAmendmentId] = useState("");
  const [text, setText] = useState("");
  const [refKind, setRefKind] = useState<SourceRefKind>("URL");
  const [refValue, setRefValue] = useState("");
  const { busy, error, run } = useAction();

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const sourceRef = { kind: refKind, value: refValue.trim() };
    const updated = await run(() => mode === "add"
      ? client.addClause(project.projectId, { clauseId, text, sourceRef })
      : client.amend(project.projectId, { amendmentId, clauseId, newText: text, sourceRef }));
    if (updated) {
      onChange(updated);
      setText("");
      setRefValue("");
      setAmendmentId("");
    }
  };

  return (
    <Card title="Charter" subtitle="Tier 1. Client and organiser requirements. Append-only; changes enter as dated amendments with a source.">
      <div className="space-y-3">
        {clauses.size === 0 && <Empty>No clauses yet. Add the exact requirement text and where it came from.</Empty>}
        {[...clauses.values()].map((clause) => (
          <article key={clause.clauseId} className="clay-inset p-3">
            <div className="flex items-center justify-between text-xs">
              <span className="font-mono font-semibold">{clause.clauseId}</span>
              <span className="text-muted">v{clause.version} · {formatTime(clause.effectiveAt)}</span>
            </div>
            <p className="mt-1.5 whitespace-pre-wrap text-sm">{clause.text}</p>
            <p className="mt-1.5 break-all text-[11px] text-muted">Source: {clause.sourceRef.value}</p>
          </article>
        ))}
      </div>
      <form onSubmit={submit} className="mt-5 space-y-3">
        <div className="flex gap-2">
          <Button variant={mode === "add" ? "primary" : "quiet"} onClick={() => setMode("add")}>New clause</Button>
          <Button variant={mode === "amend" ? "primary" : "quiet"} onClick={() => setMode("amend")} disabled={clauses.size === 0}>Amend</Button>
        </div>
        {mode === "add" ? (
          <Field label="Clause id"><input className="field" required pattern={ID_PATTERN} value={clauseId} onChange={(e) => setClauseId(e.target.value)} /></Field>
        ) : (
          <div className="grid grid-cols-2 gap-2">
            <Field label="Clause">
              <select className="field" required value={clauseId} onChange={(e) => setClauseId(e.target.value)}>
                <option value="">Select</option>
                {[...clauses.keys()].map((id) => <option key={id} value={id}>{id}</option>)}
              </select>
            </Field>
            <Field label="Amendment id"><input className="field" required pattern={ID_PATTERN} value={amendmentId} onChange={(e) => setAmendmentId(e.target.value)} /></Field>
          </div>
        )}
        <Field label={mode === "add" ? "Exact requirement text" : "New clause text"}>
          <textarea className="field min-h-20" required maxLength={20000} value={text} onChange={(e) => setText(e.target.value)} />
        </Field>
        <SourceRefInputs kind={refKind} setKind={setRefKind} value={refValue} setValue={setRefValue} />
        {mode === "amend" && <p className="text-[11px] text-stale">Amending marks every Proof check on this clause STALE.</p>}
        <Button type="submit" variant="primary" disabled={busy}>{mode === "add" ? "Add clause" : "Submit amendment"}</Button>
        <ErrorNote message={error} />
      </form>
    </Card>
  );
}

function PromotionDialog({ check, clause, onCancel, onConfirm, busy }: {
  check: ProofCheck; clause: CharterClause | undefined; onCancel: () => void; onConfirm: () => void; busy: boolean;
}) {
  const [compared, setCompared] = useState(false);
  return (
    <div role="dialog" aria-modal="true" aria-labelledby="promote-title" className="fixed inset-0 z-50 grid place-items-center bg-ink/30 p-4">
      <div className="clay w-full max-w-3xl p-6">
        <h3 id="promote-title" className="text-base font-semibold">Promote {check.checkId} to a gating check</h3>
        <p className="mt-1 text-xs text-muted">Read both texts. Promotion makes this interpretation block approval if it later fails.</p>
        <div className="mt-4 grid gap-4 md:grid-cols-2">
          <div className="clay-inset p-4">
            <p className="text-[11px] font-semibold uppercase tracking-wide text-muted">Charter clause {clause ? `${clause.clauseId} v${clause.version}` : "(missing)"}</p>
            <p className="mt-2 whitespace-pre-wrap text-sm">{clause?.text ?? "Clause not found."}</p>
          </div>
          <div className="clay-inset p-4">
            <p className="text-[11px] font-semibold uppercase tracking-wide text-muted">Interpretation (bound to v{check.clauseVersion})</p>
            <p className="mt-2 whitespace-pre-wrap text-sm">{check.interpretation}</p>
          </div>
        </div>
        <label className="mt-4 flex items-center gap-2 text-sm">
          <input type="checkbox" checked={compared} onChange={(e) => setCompared(e.target.checked)} />
          I compared the interpretation against the exact clause text.
        </label>
        <div className="mt-5 flex justify-end gap-2">
          <Button onClick={onCancel}>Cancel</Button>
          <Button variant="primary" disabled={!compared || busy || !clause} onClick={onConfirm}>Promote</Button>
        </div>
      </div>
    </div>
  );
}

function ProofColumn({ project, client, onChange }: { project: Project; client: ApiClient; onChange: (p: Project) => void }) {
  const clauses = useMemo(() => currentClauses(project), [project]);
  const [checkId, setCheckId] = useState("");
  const [clauseId, setClauseId] = useState("");
  const [interpretation, setInterpretation] = useState("");
  const [promoting, setPromoting] = useState<ProofCheck | null>(null);
  const [reconfirmText, setReconfirmText] = useState<Record<string, string>>({});
  const { busy, error, run } = useAction();

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const updated = await run(() => client.addProof(project.projectId, { checkId, clauseId, interpretation }));
    if (updated) {
      onChange(updated);
      setCheckId("");
      setInterpretation("");
    }
  };

  return (
    <Card title="Proof" subtitle="Tier 2. Your interpretation of each clause. Text comparison only: reports possible misreadings, never 'verified'.">
      <div className="space-y-3">
        {project.proofChecks.length === 0 && <Empty>No interpretations yet.</Empty>}
        {project.proofChecks.map((check) => (
          <article key={check.checkId} className="clay-inset p-3">
            <div className="flex items-center justify-between gap-2 text-xs">
              <span className="font-mono font-semibold">{check.checkId} <span className="font-normal text-muted">→ {check.clauseId} v{check.clauseVersion}</span></span>
              <StatusBadge status={check.status} />
            </div>
            <p className="mt-1.5 whitespace-pre-wrap text-sm">{check.interpretation}</p>
            <p className="mt-1 text-[11px] text-muted">
              {check.status === "PASS" && "No misreading detected (text comparison only, not verified)."}
              {check.status === "FAIL" && "Possible misreading detected."}
              {check.status === "STALE" && "Clause was amended. Re-confirm against the new text."}
              {check.status === "UNVERIFIED" && "Not yet checked. Run an audit."}
              {check.promotedBy && ` Promoted ${formatTime(check.promotedAt)}; gates approval.`}
            </p>
            {check.status === "STALE" ? (
              <div className="mt-2 space-y-2">
                <textarea className="field min-h-16" placeholder="Updated interpretation" value={reconfirmText[check.checkId] ?? check.interpretation}
                          onChange={(e) => setReconfirmText({ ...reconfirmText, [check.checkId]: e.target.value })} />
                <Button disabled={busy} onClick={async () => {
                  const updated = await run(() => client.reconfirm(project.projectId, check.checkId, reconfirmText[check.checkId] ?? check.interpretation));
                  if (updated) onChange(updated);
                }}>Re-confirm</Button>
              </div>
            ) : !check.promotedBy && (
              <Button className="mt-2" disabled={check.status !== "PASS" || busy} onClick={() => setPromoting(check)}
                      title={check.status !== "PASS" ? "Only PASS checks can be promoted" : undefined}>Review and promote</Button>
            )}
          </article>
        ))}
      </div>
      <form onSubmit={submit} className="mt-5 space-y-3">
        <div className="grid grid-cols-2 gap-2">
          <Field label="Check id"><input className="field" required pattern={ID_PATTERN} value={checkId} onChange={(e) => setCheckId(e.target.value)} /></Field>
          <Field label="Clause">
            <select className="field" required value={clauseId} onChange={(e) => setClauseId(e.target.value)}>
              <option value="">Select</option>
              {[...clauses.keys()].map((id) => <option key={id} value={id}>{id}</option>)}
            </select>
          </Field>
        </div>
        <Field label="Your interpretation"><textarea className="field min-h-20" required maxLength={20000} value={interpretation} onChange={(e) => setInterpretation(e.target.value)} /></Field>
        <Button type="submit" variant="primary" disabled={busy || clauses.size === 0}>Add interpretation</Button>
        <ErrorNote message={error} />
      </form>
      {promoting && (
        <PromotionDialog check={promoting} clause={clauses.get(promoting.clauseId)} busy={busy} onCancel={() => setPromoting(null)}
          onConfirm={async () => {
            const updated = await run(() => client.promote(project.projectId, promoting.checkId));
            if (updated) onChange(updated);
            setPromoting(null);
          }} />
      )}
    </Card>
  );
}

function WorkbenchColumn({ project, client, onChange }: { project: Project; client: ApiClient; onChange: (p: Project) => void }) {
  const [checkId, setCheckId] = useState("");
  const [tier, setTier] = useState(3);
  const [label, setLabel] = useState("");
  const [description, setDescription] = useState("");
  const { busy, error, run } = useAction();

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const updated = await run(() => client.addWorkbench(project.projectId, { checkId, tier, label, description: description.trim() || null }));
    if (updated) {
      onChange(updated);
      setCheckId("");
      setLabel("");
      setDescription("");
    }
  };

  return (
    <Card title="Workbench" subtitle="Tier 3+. Scratch space. Non-authoritative; never gates approval.">
      <div className="space-y-3">
        {project.workbenchChecks.length === 0 && <Empty>Nothing here yet.</Empty>}
        {project.workbenchChecks.map((check) => (
          <article key={check.checkId} className="clay-inset p-3">
            <div className="flex items-center justify-between text-xs">
              <span className="font-mono font-semibold">{check.checkId} <span className="font-normal text-muted">tier {check.tier}</span></span>
              <StatusBadge status={check.status} />
            </div>
            <p className="mt-1.5 text-sm font-medium">{check.label}</p>
            {check.description && <p className="mt-1 whitespace-pre-wrap text-xs text-muted">{check.description}</p>}
          </article>
        ))}
      </div>
      <form onSubmit={submit} className="mt-5 space-y-3">
        <div className="grid grid-cols-[1fr_6rem] gap-2">
          <Field label="Check id"><input className="field" required pattern={ID_PATTERN} value={checkId} onChange={(e) => setCheckId(e.target.value)} /></Field>
          <Field label="Tier"><input className="field" type="number" min={3} max={16} required value={tier} onChange={(e) => setTier(Number(e.target.value))} /></Field>
        </div>
        <Field label="Label"><input className="field" required maxLength={512} value={label} onChange={(e) => setLabel(e.target.value)} /></Field>
        <Field label="Notes"><textarea className="field min-h-16" maxLength={20000} value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
        <Button type="submit" variant="primary" disabled={busy}>Add to workbench</Button>
        <ErrorNote message={error} />
      </form>
    </Card>
  );
}

export function TierBoard(props: { project: Project; client: ApiClient; onChange: (p: Project) => void }) {
  return (
    <div className="grid gap-6 xl:grid-cols-3">
      <CharterColumn {...props} />
      <ProofColumn {...props} />
      <WorkbenchColumn {...props} />
    </div>
  );
}
