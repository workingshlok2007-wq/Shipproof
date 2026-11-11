"use client";

import { useCallback, useEffect, useState } from "react";
import { AuditView } from "@/components/AuditView";
import { ProjectForm } from "@/components/ProjectForm";
import { TierBoard } from "@/components/TierBoard";
import { Button, Card, Empty, ErrorNote, SafeLink } from "@/components/ui";
import { useAction } from "@/components/useAction";
import { createClient } from "@/lib/api";
import type { Health, PacketView, Project } from "@/lib/types";
import { useAuditStream } from "@/lib/useAuditStream";

const PACKET_REFRESH_MS = 2000;
const REFRESH_KINDS = new Set(["evidence", "flag", "alignment", "proofCheck", "plan", "done", "decision", "paused", "agentStep",
  "questionAnswered", "riskAccepted", "remediation"]);

export default function Home() {
  const [projectIds, setProjectIds] = useState<string[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [creating, setCreating] = useState(false);
  const [packetId, setPacketId] = useState<string | null>(null);
  const [view, setView] = useState<PacketView | null>(null);
  const { busy, error, run, setError } = useAction();

  const [client] = useState(createClient);
  const stream = useAuditStream(client, packetId);
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;
    const load = async () => {
      try {
        const next = await client.health();
        if (cancelled) return;
        setHealth(next);
        if (next.llm.status === "starting") timer = setTimeout(load, 3000);
      } catch {
        if (!cancelled) timer = setTimeout(load, 5000); // engine not up yet; the page-level error explains it
      }
    };
    void load();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [client]);

  useEffect(() => {
    void run(async () => {
      const { projectIds: ids } = await client.listProjects();
      setProjectIds(ids);
      setCreating(ids.length === 0);
    });
  }, [client, run]);

  const openProject = useCallback(async (projectId: string) => {
    const loaded = await run(() => client.getProject(projectId));
    if (loaded) {
      setProject(loaded);
      setCreating(false);
      setPacketId(loaded.packetIds.at(-1) ?? null);
    }
  }, [client, run]);

  const refreshPacket = useCallback(async () => {
    if (!packetId) return;
    try {
      setView(await client.getPacket(packetId));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    }
  }, [client, packetId, setError]);

  useEffect(() => {
    setView(null);
    void refreshPacket();
  }, [refreshPacket]);

  const lastEventKind = stream.events.at(-1)?.kind;
  useEffect(() => {
    if (lastEventKind && REFRESH_KINDS.has(lastEventKind)) void refreshPacket();
    if (lastEventKind === "done" && project) void client.getProject(project.projectId).then(setProject, () => undefined);
  }, [stream.events.length, lastEventKind, refreshPacket, client, project?.projectId]);

  useEffect(() => {
    if (view?.packet.approvalState !== "AUDITING") return;
    const timer = setInterval(() => void refreshPacket(), PACKET_REFRESH_MS);
    return () => clearInterval(timer);
  }, [view?.packet.approvalState, refreshPacket]);

  const triggerAudit = async () => {
    if (!project) return;
    const started = await run(() => client.triggerAudit(project.projectId));
    if (started) {
      setProject({ ...project, packetIds: [...project.packetIds, started.packetId] });
      setPacketId(started.packetId);
    }
  };

  return (
    <main className="mx-auto max-w-7xl space-y-6 px-4 pb-40 pt-8 sm:px-6">
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">ShipProof</h1>
          <p className="text-sm text-muted">Evidence ledger and human approval boundary. The agent prepares; you decide.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {health && (
            <span title={health.llm.reason ?? health.llm.chain?.join(" → ") ?? ""}
                  className={`max-w-xs truncate rounded-full px-3 py-1 text-[11px] font-semibold ${
                    health.llmMode === "replay" || health.llm.status === "ready" || health.llm.status === "injected" ? "bg-pass/12 text-pass"
                      : health.llm.status === "starting" ? "bg-stale/15 text-stale" : "bg-fail/10 text-fail"}`}>
              {health.llmMode === "replay" ? "Model: replaying saved answers"
                : health.llm.status === "ready" ? `Model: ${health.llm.model}${health.llmMode === "record" ? " (recording)" : ""}`
                : health.llm.status === "starting" ? "Model: checking…" : health.llm.status === "injected" ? "Model: test" : "Model unavailable"}
              {health.demo ? " · demo" : ""}
            </span>
          )}
          <select className="field w-48" aria-label="Project" value={project?.projectId ?? ""} onChange={(e) => e.target.value && openProject(e.target.value)}>
            <option value="">Select project</option>
            {projectIds.map((id) => <option key={id} value={id}>{id}</option>)}
          </select>
          <Button onClick={() => { setCreating(true); setProject(null); setPacketId(null); }}>New project</Button>
        </div>
      </header>
      <ErrorNote message={error} />

      {creating && <ProjectForm client={client} onCreated={(created) => {
        setProjectIds((ids) => [...new Set([...ids, created.projectId])].sort());
        setProject(created);
        setCreating(false);
        setPacketId(null);
      }} />}

      {!creating && !project && <Empty>Select a project to begin.</Empty>}

      {project && (
        <>
          <Card title={project.projectId} subtitle="Audit target"
                action={<Button variant="primary" disabled={busy || view?.packet.approvalState === "AUDITING" || view?.packet.approvalState === "AWAITING_INPUT"} onClick={triggerAudit}>Run audit</Button>}>
            <div className="grid gap-4 text-sm md:grid-cols-3">
              <div>
                <p className="text-xs font-medium text-muted">Repository</p>
                <SafeLink href={`https://github.com/${project.repo}`}>{project.repo}</SafeLink>
              </div>
              <div>
                <p className="text-xs font-medium text-muted">Submission links</p>
                {project.links.length === 0 ? <p className="text-xs text-muted">None</p> :
                  project.links.map((link) => <p key={link.url} className="text-xs">{link.label}: <SafeLink href={link.url} /></p>)}
              </div>
              <div>
                <p className="text-xs font-medium text-muted">Rule sources</p>
                {project.rulePages.map((url) => <p key={url}><SafeLink href={url} /></p>)}
                <p className="text-xs text-muted">{project.emailQuery ? `Gmail: ${project.emailQuery}` : "No inbox query"}</p>
              </div>
            </div>
            {project.packetIds.length > 1 && (
              <div className="mt-4 flex items-center gap-2">
                <span className="text-xs text-muted">Audit history</span>
                <select className="field w-56" value={packetId ?? ""} onChange={(e) => setPacketId(e.target.value || null)}>
                  {[...project.packetIds].reverse().map((id) => <option key={id} value={id}>{id}</option>)}
                </select>
              </div>
            )}
          </Card>

          <TierBoard project={project} client={client} onChange={setProject} />

          {view ? (
            <AuditView client={client} view={view} events={stream.events} streamMode={stream.mode} streamError={stream.error}
                       onPacket={(packet) => setView({ packet, summary: view.summary })}
                       onRefresh={() => void refreshPacket()}
                       onRemediation={(updated) => setView({ ...view, summary: view.summary && {
                         ...view.summary,
                         remediations: view.summary.remediations.map((r) => r.remediationId === updated.remediationId ? updated : r),
                       } })} />
          ) : packetId ? <Empty>Loading audit…</Empty> : <Empty>No audits yet. Add Charter clauses and interpretations, then run an audit.</Empty>}
        </>
      )}
    </main>
  );
}
