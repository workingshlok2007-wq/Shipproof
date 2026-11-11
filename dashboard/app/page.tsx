"use client";

import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { AuditView } from "@/components/AuditView";
import { ProjectForm } from "@/components/ProjectForm";
import { TierBoard } from "@/components/TierBoard";
import { Button, Card, Empty, ErrorNote, Field, SafeLink } from "@/components/ui";
import { useAction } from "@/components/useAction";
import { createClient, loadToken, saveToken } from "@/lib/api";
import type { PacketView, Project } from "@/lib/types";
import { useAuditStream } from "@/lib/useAuditStream";

const PACKET_REFRESH_MS = 2000;
const REFRESH_KINDS = new Set(["evidence", "flag", "alignment", "proofCheck", "plan", "done", "decision"]);

function TokenGate({ onToken }: { onToken: (token: string) => void }) {
  const [value, setValue] = useState("");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (value.trim().length >= 24) onToken(value.trim());
  };
  return (
    <main className="grid min-h-screen place-items-center p-6">
      <form onSubmit={submit} className="clay w-full max-w-md space-y-4 p-8">
        <h1 className="text-2xl font-semibold tracking-tight">ShipProof</h1>
        <p className="text-sm text-muted">Paste the engine API token (the contents of <span className="font-mono">.shipproof-token</span>). It is kept in this tab only.</p>
        <Field label="API token"><input className="field font-mono" type="password" autoComplete="off" minLength={24} required value={value} onChange={(e) => setValue(e.target.value)} /></Field>
        <Button type="submit" variant="primary" className="w-full">Connect</Button>
      </form>
    </main>
  );
}

export default function Home() {
  const [token, setToken] = useState<string | null>(null);
  const [tokenLoaded, setTokenLoaded] = useState(false);
  const [projectIds, setProjectIds] = useState<string[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [creating, setCreating] = useState(false);
  const [packetId, setPacketId] = useState<string | null>(null);
  const [view, setView] = useState<PacketView | null>(null);
  const { busy, error, run, setError } = useAction();

  useEffect(() => {
    setToken(loadToken());
    setTokenLoaded(true);
  }, []);
  const client = useMemo(() => (token ? createClient(token) : null), [token]);
  const stream = useAuditStream(client, token, packetId);

  const signOut = useCallback(() => {
    saveToken(null);
    setToken(null);
    setProject(null);
    setPacketId(null);
    setView(null);
  }, []);

  useEffect(() => {
    if (!client) return;
    void run(async () => {
      try {
        const { projectIds: ids } = await client.listProjects();
        setProjectIds(ids);
        setCreating(ids.length === 0);
      } catch (failure) {
        if (failure instanceof Error && "status" in failure && (failure as { status: number }).status === 401) signOut();
        throw failure;
      }
    });
  }, [client, run, signOut]);

  const openProject = useCallback(async (projectId: string) => {
    if (!client) return;
    const loaded = await run(() => client.getProject(projectId));
    if (loaded) {
      setProject(loaded);
      setCreating(false);
      setPacketId(loaded.packetIds.at(-1) ?? null);
    }
  }, [client, run]);

  const refreshPacket = useCallback(async () => {
    if (!client || !packetId) return;
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
    if (lastEventKind === "done" && client && project) void client.getProject(project.projectId).then(setProject, () => undefined);
  }, [stream.events.length, lastEventKind, refreshPacket, client, project?.projectId]);

  useEffect(() => {
    if (view?.packet.approvalState !== "AUDITING") return;
    const timer = setInterval(() => void refreshPacket(), PACKET_REFRESH_MS);
    return () => clearInterval(timer);
  }, [view?.packet.approvalState, refreshPacket]);

  const triggerAudit = async () => {
    if (!client || !project) return;
    const started = await run(() => client.triggerAudit(project.projectId));
    if (started) {
      setProject({ ...project, packetIds: [...project.packetIds, started.packetId] });
      setPacketId(started.packetId);
    }
  };

  if (!tokenLoaded) return null;
  if (!token || !client) {
    return <TokenGate onToken={(value) => { saveToken(value); setToken(value); }} />;
  }

  return (
    <main className="mx-auto max-w-7xl space-y-6 px-4 pb-40 pt-8 sm:px-6">
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">ShipProof</h1>
          <p className="text-sm text-muted">Evidence ledger and human approval boundary. The agent prepares; you decide.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select className="field w-48" aria-label="Project" value={project?.projectId ?? ""} onChange={(e) => e.target.value && openProject(e.target.value)}>
            <option value="">Select project</option>
            {projectIds.map((id) => <option key={id} value={id}>{id}</option>)}
          </select>
          <Button onClick={() => { setCreating(true); setProject(null); setPacketId(null); }}>New project</Button>
          <Button onClick={signOut}>Sign out</Button>
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
                action={<Button variant="primary" disabled={busy || view?.packet.approvalState === "AUDITING"} onClick={triggerAudit}>Run audit</Button>}>
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
                       onPacket={(packet) => setView({ packet, summary: view.summary })} />
          ) : packetId ? <Empty>Loading audit…</Empty> : <Empty>No audits yet. Add Charter clauses and interpretations, then run an audit.</Empty>}
        </>
      )}
    </main>
  );
}
