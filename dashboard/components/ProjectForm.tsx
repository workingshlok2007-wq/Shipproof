"use client";

import { useState, type FormEvent } from "react";
import type { ApiClient } from "@/lib/api";
import { isSafeHttpUrl } from "@/lib/api";
import type { LinkTarget, Project } from "@/lib/types";
import { Button, Card, ErrorNote, Field } from "./ui";
import { useAction } from "./useAction";

/** Parses "Label | https://url" lines. Rejects any malformed line instead of silently skipping it. */
export function parseLinks(raw: string): LinkTarget[] {
  return raw.split("\n").map((line) => line.trim()).filter(Boolean).map((line, position) => {
    const [label, url, ...rest] = line.split("|").map((part) => part.trim());
    if (!label || !url || rest.length || !isSafeHttpUrl(url)) {
      throw new Error(`Link line ${position + 1} must be "Label | https://url"`);
    }
    return { label, url };
  });
}

export function parseUrls(raw: string): string[] {
  return raw.split("\n").map((line) => line.trim()).filter(Boolean).map((url, position) => {
    if (!isSafeHttpUrl(url)) throw new Error(`Rules page line ${position + 1} is not an http(s) URL`);
    return url;
  });
}

export function parseEmailQuery(raw: string): string | null {
  const query = raw.trim();
  if (!query) return null;
  if (/^https?:\/\//i.test(query)) throw new Error("Gmail search must be a Gmail query such as from:organisers@event.dev, not a URL");
  return query;
}

export function ProjectForm({ client, onCreated }: { client: ApiClient; onCreated: (project: Project) => void }) {
  const [projectId, setProjectId] = useState("");
  const [repo, setRepo] = useState("");
  const [proposalText, setProposalText] = useState("");
  const [links, setLinks] = useState("");
  const [rulePages, setRulePages] = useState("");
  const [emailQuery, setEmailQuery] = useState("");
  const { busy, error, run } = useAction();

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const created = await run(() => client.createProject({
      projectId, repo: repo.trim(), proposalText, links: parseLinks(links), rulePages: parseUrls(rulePages),
      emailQuery: parseEmailQuery(emailQuery),
    }));
    if (created) onCreated(created);
  };

  return (
    <Card title="New project" subtitle="What will be audited. All fields except the proposal and repository are optional.">
      <form onSubmit={submit} className="grid gap-4 md:grid-cols-2">
        <Field label="Project id"><input className="field" required pattern="[A-Za-z0-9_.:\-]{1,64}" value={projectId} onChange={(e) => setProjectId(e.target.value)} /></Field>
        <Field label="GitHub repository"><input className="field" required placeholder="owner/name" value={repo} onChange={(e) => setRepo(e.target.value)} /></Field>
        <div className="md:col-span-2">
          <Field label="Original proposal or pitch" hint="Scope alignment compares this text with the code.">
            <textarea className="field min-h-28" required maxLength={20000} value={proposalText} onChange={(e) => setProposalText(e.target.value)} />
          </Field>
        </div>
        <Field label="Submission links" hint="One per line: Demo video | https://...">
          <textarea className="field min-h-20 font-mono" placeholder="Demo video | https://youtu.be/..." value={links} onChange={(e) => setLinks(e.target.value)} />
        </Field>
        <Field label="Rules pages" hint="One URL per line.">
          <textarea className="field min-h-20 font-mono" placeholder="https://event.dev/rules" value={rulePages} onChange={(e) => setRulePages(e.target.value)} />
        </Field>
        <Field label="Gmail search (optional)" hint="A Gmail query, not a URL. Needs Gmail authorised on the engine.">
          <input className="field" maxLength={500} placeholder="from:organisers@event.dev newer_than:30d" value={emailQuery} onChange={(e) => setEmailQuery(e.target.value)} />
        </Field>
        <div className="flex items-end"><Button type="submit" variant="primary" disabled={busy}>Create project</Button></div>
        <div className="md:col-span-2"><ErrorNote message={error} /></div>
      </form>
    </Card>
  );
}
