import type { AuditEvent, PacketView, Project, SourceRef, SubmissionPacket } from "./types";

const TOKEN_KEY = "shipproofToken";
const API_BASE = "/api";
const REQUEST_TIMEOUT_MS = 30_000;

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
  }
}

// sessionStorage: the token dies with the tab and is never shared across tabs or written to disk long-term.
export function loadToken(): string | null {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null; // storage blocked (private mode / policy): the user re-enters the token each load
  }
}

export function saveToken(token: string | null): void {
  try {
    if (token) window.sessionStorage.setItem(TOKEN_KEY, token);
    else window.sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // storage blocked: token stays in memory for this page only
  }
}

function describeDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item && typeof item === "object" && "msg" in item) {
          const loc = Array.isArray((item as { loc?: unknown }).loc) ? (item as { loc: unknown[] }).loc.join(".") : "";
          return loc ? `${loc}: ${String((item as { msg: unknown }).msg)}` : String((item as { msg: unknown }).msg);
        }
        return String(item);
      })
      .join("; ");
  }
  return "request failed";
}

async function request<T>(token: string, path: string, method: "GET" | "POST" = "GET", body?: unknown): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers: { Authorization: `Bearer ${token}`, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
      cache: "no-store",
    });
  } catch (error) {
    throw new ApiError(0, controller.signal.aborted ? "request timed out" : `engine unreachable: ${String(error)}`);
  } finally {
    clearTimeout(timer);
  }
  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      // The Next.js proxy answers with a plain-text 500 when nothing is listening at SHIPPROOF_API_URL.
      throw new ApiError(response.status, response.status >= 500
        ? "Engine unreachable. Start it in another terminal: uvicorn shipproof.api:buildApp --factory --host 127.0.0.1 --port 8000"
        : `engine returned non-JSON (${response.status})`);
    }
  }
  if (!response.ok) {
    const detail = payload && typeof payload === "object" && "detail" in payload ? (payload as { detail: unknown }).detail : null;
    throw new ApiError(response.status, describeDetail(detail) || `HTTP ${response.status}`);
  }
  return payload as T;
}

const enc = encodeURIComponent;

export function createClient(token: string) {
  return {
    listProjects: () => request<{ projectIds: string[] }>(token, "/projects"),
    getProject: (projectId: string) => request<Project>(token, `/projects/${enc(projectId)}`),
    createProject: (body: Omit<Project, "charter" | "proofChecks" | "workbenchChecks" | "packetIds">) =>
      request<Project>(token, "/projects", "POST", body),
    addClause: (projectId: string, body: { clauseId: string; text: string; sourceRef: SourceRef }) =>
      request<Project>(token, `/projects/${enc(projectId)}/clauses`, "POST", body),
    amend: (projectId: string, body: { amendmentId: string; clauseId: string; newText: string; sourceRef: SourceRef }) =>
      request<Project>(token, `/projects/${enc(projectId)}/amendments`, "POST", body),
    addProof: (projectId: string, body: { checkId: string; clauseId: string; interpretation: string }) =>
      request<Project>(token, `/projects/${enc(projectId)}/proof-checks`, "POST", body),
    reconfirm: (projectId: string, checkId: string, interpretation: string) =>
      request<Project>(token, `/projects/${enc(projectId)}/proof-checks/${enc(checkId)}/reconfirm`, "POST", { interpretation }),
    promote: (projectId: string, checkId: string) =>
      request<Project>(token, `/projects/${enc(projectId)}/proof-checks/${enc(checkId)}/promote`, "POST"),
    addWorkbench: (projectId: string, body: { checkId: string; tier: number; label: string; description: string | null }) =>
      request<Project>(token, `/projects/${enc(projectId)}/workbench`, "POST", body),
    triggerAudit: (projectId: string) => request<{ packetId: string }>(token, `/projects/${enc(projectId)}/trigger-audit`, "POST"),
    getPacket: (packetId: string) => request<PacketView>(token, `/packets/${enc(packetId)}`),
    getEvents: (packetId: string, after: number) =>
      request<{ events: AuditEvent[]; next: number }>(token, `/packets/${enc(packetId)}/events?after=${after}`),
    confirmCoverage: (packetId: string) => request<SubmissionPacket>(token, `/packets/${enc(packetId)}/coverage/confirm`, "POST"),
    approve: (packetId: string) => request<SubmissionPacket>(token, `/packets/${enc(packetId)}/approve`, "POST"),
    reject: (packetId: string) => request<SubmissionPacket>(token, `/packets/${enc(packetId)}/reject`, "POST"),
  };
}

export type ApiClient = ReturnType<typeof createClient>;

export function isSafeHttpUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}
