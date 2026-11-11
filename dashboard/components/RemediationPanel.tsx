"use client";

import { useState } from "react";
import type { ApiClient } from "@/lib/api";
import type { Remediation } from "@/lib/types";
import { Button, Card, Empty, ErrorNote, Field, SafeLink } from "./ui";
import { useAction } from "./useAction";

const LICENSES: [string, string][] = [
  ["mit", "MIT"], ["apache-2.0", "Apache 2.0"], ["bsd-3-clause", "BSD 3-Clause"], ["bsd-2-clause", "BSD 2-Clause"],
  ["mpl-2.0", "Mozilla 2.0"], ["gpl-3.0", "GPL 3.0"], ["agpl-3.0", "AGPL 3.0"], ["lgpl-3.0", "LGPL 3.0"], ["unlicense", "Unlicense"],
];

function RemediationCard({ client, packetId, remediation, onUpdated }: {
  client: ApiClient; packetId: string; remediation: Remediation; onUpdated: (r: Remediation) => void;
}) {
  const [licenseKey, setLicenseKey] = useState("");
  const [holder, setHolder] = useState("");
  const { busy, error, run } = useAction();
  const needsLicense = remediation.requiredInputs.includes("licenseKey");
  const inputsReady = !needsLicense || (licenseKey !== "" && holder.trim().length > 0);

  const apply = async () => {
    const updated = await run(() => client.applyRemediation(packetId, remediation.remediationId,
      needsLicense ? { licenseKey, copyrightHolder: holder.trim() } : {}));
    if (updated) onUpdated(updated);
  };
  const dismiss = async () => {
    const updated = await run(() => client.dismissRemediation(packetId, remediation.remediationId));
    if (updated) onUpdated(updated);
  };

  return (
    <article className="clay-inset p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="text-sm font-semibold">{remediation.title}</p>
          <p className="text-[11px] text-muted">Drafted from {remediation.draftedFrom}{remediation.applicable ? "" : " · advice only"}</p>
        </div>
        <span className="rounded-full bg-surface px-2.5 py-0.5 text-[11px] font-semibold text-muted">{remediation.status}</span>
      </div>
      <p className="mt-2 text-sm">{remediation.rationale}</p>
      {remediation.advice.length > 0 && (
        <ul className="mt-2 list-disc space-y-1 pl-5 text-xs">{remediation.advice.map((line) => <li key={line}>{line}</li>)}</ul>
      )}
      {remediation.changes.map((change) => (
        <details key={change.path} className="mt-3">
          <summary className="cursor-pointer text-xs font-medium text-accent-deep">Preview {change.path}</summary>
          <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap rounded-xl bg-surface p-3 font-mono text-[11px]">{change.newContent}</pre>
        </details>
      ))}
      {remediation.pullRequestUrl && <p className="mt-3 text-xs">Pull request: <SafeLink href={remediation.pullRequestUrl} /></p>}
      {remediation.status === "PROPOSED" && remediation.applicable && (
        <div className="mt-3 space-y-3">
          {needsLicense && (
            <div className="grid gap-2 sm:grid-cols-2">
              <Field label="License (your decision)">
                <select className="field" value={licenseKey} onChange={(e) => setLicenseKey(e.target.value)}>
                  <option value="">Choose</option>
                  {LICENSES.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
                </select>
              </Field>
              <Field label="Copyright holder"><input className="field" maxLength={100} value={holder} onChange={(e) => setHolder(e.target.value)} /></Field>
            </div>
          )}
          <div className="flex gap-2">
            <Button variant="primary" disabled={busy || !inputsReady} onClick={apply}>{busy ? "Opening pull request…" : "Apply as pull request"}</Button>
            <Button disabled={busy} onClick={dismiss}>Dismiss</Button>
          </div>
        </div>
      )}
      {remediation.status === "PROPOSED" && !remediation.applicable && (
        <div className="mt-3"><Button disabled={busy} onClick={dismiss}>Acknowledge</Button></div>
      )}
      <ErrorNote message={error} />
    </article>
  );
}

export function RemediationPanel({ client, packetId, remediations, auditing, onUpdated }: {
  client: ApiClient; packetId: string; remediations: Remediation[]; auditing: boolean; onUpdated: (r: Remediation) => void;
}) {
  return (
    <Card title="Suggested fixes" subtitle="Drafted by the agent from verified audit facts. Applying opens a pull request on a new branch; nothing is merged for you.">
      {remediations.length === 0 ? <Empty>{auditing ? "Drafting after the audit." : "No minor problems to fix."}</Empty> : (
        <div className="space-y-3">
          {remediations.map((remediation) => (
            <RemediationCard key={remediation.remediationId} client={client} packetId={packetId} remediation={remediation} onUpdated={onUpdated} />
          ))}
        </div>
      )}
    </Card>
  );
}
