// Server-side proxy to the FastAPI engine. The engine token is attached here and never reaches the browser,
// so the dashboard server is the authentication boundary: run it on 127.0.0.1 (see package.json scripts).
import { readFileSync } from "node:fs";
import path from "node:path";
import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const ALLOWED_ROOTS = new Set(["health", "projects", "packets"]);
const SEGMENT = /^[A-Za-z0-9_.:-]{1,128}$/;
const MIN_TOKEN_CHARS = 24;
const UPSTREAM_TIMEOUT_MS = 30_000;
const MAX_BODY_BYTES = 256 * 1024;

function engineUrl(): string {
  const value = process.env.SHIPPROOF_API_URL ?? "http://127.0.0.1:8000";
  if (!/^https?:\/\/[^\s/]+$/.test(value)) throw new Error("SHIPPROOF_API_URL must be an origin like http://127.0.0.1:8000");
  return value;
}

function engineToken(): string {
  // Environment first; otherwise the token file the engine was started from (repo root, one level up).
  const fromEnv = process.env.SHIPPROOF_API_TOKEN?.trim();
  if (fromEnv) return fromEnv;
  const file = process.env.SHIPPROOF_TOKEN_FILE ?? path.join(/* turbopackIgnore: true */ process.cwd(), "..", ".shipproof-token");
  try {
    return readFileSync(/* turbopackIgnore: true */ file, "utf8").trim();
  } catch {
    return "";
  }
}

function fail(status: number, detail: string): NextResponse {
  return NextResponse.json({ detail }, { status });
}

async function forward(request: NextRequest, segments: string[], method: "GET" | "POST"): Promise<NextResponse> {
  if (!segments.length || !ALLOWED_ROOTS.has(segments[0] ?? "") || !segments.every((s) => SEGMENT.test(s) && s !== "." && s !== "..")) {
    return fail(404, "unknown endpoint");
  }
  if (method === "POST") {
    // CSRF: a cross-site page cannot set this header without a CORS preflight, which this route never grants.
    if (request.headers.get("x-shipproof-request") !== "1" || request.headers.get("sec-fetch-site") === "cross-site") {
      return fail(403, "cross-site request refused");
    }
  }
  const token = engineToken();
  if (token.length < MIN_TOKEN_CHARS) {
    return fail(500, "Dashboard has no engine token. Start it from the repo with .shipproof-token present, or set SHIPPROOF_API_TOKEN.");
  }
  let body: string | undefined;
  if (method === "POST") {
    body = await request.text();
    if (body.length > MAX_BODY_BYTES) return fail(413, "request body too large");
  }
  const search = request.nextUrl.search;
  let upstream: Response;
  try {
    upstream = await fetch(`${engineUrl()}/${segments.join("/")}${search}`, {
      method,
      headers: { Authorization: `Bearer ${token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body || undefined,
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
  } catch {
    return fail(502, "Engine unreachable. Start it: uvicorn shipproof.api:buildApp --factory --host 127.0.0.1 --port 8000");
  }
  if (upstream.status === 401) return fail(502, "Engine rejected the dashboard's token: both must use the same .shipproof-token.");
  return new NextResponse(await upstream.text(), {
    status: upstream.status,
    headers: { "Content-Type": upstream.headers.get("content-type") ?? "application/json", "Cache-Control": "no-store" },
  });
}

type Context = { params: Promise<{ path: string[] }> };

export async function GET(request: NextRequest, context: Context) {
  return forward(request, (await context.params).path, "GET");
}

export async function POST(request: NextRequest, context: Context) {
  return forward(request, (await context.params).path, "POST");
}
