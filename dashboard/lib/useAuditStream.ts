"use client";

import { useEffect, useState } from "react";
import type { ApiClient } from "./api";
import type { AuditEvent } from "./types";

const POLL_INTERVAL_MS = 1500;
const MAX_EVENTS = 1000;

export type StreamMode = "idle" | "polling" | "closed";

/**
 * Live audit events via the dashboard server, which holds the engine token. Polls the event history endpoint
 * from a cursor (no duplicates, survives dropped requests) and stops once a "done" event arrives.
 */
export function useAuditStream(client: ApiClient, packetId: string | null) {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [mode, setMode] = useState<StreamMode>("idle");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setEvents([]);
    setError(null);
    if (!packetId) {
      setMode("idle");
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let cursor = 0;
    setMode("polling");

    const poll = async () => {
      let done = false;
      try {
        const page = await client.getEvents(packetId, cursor);
        if (cancelled) return;
        cursor = page.next;
        done = page.events.some((e) => e.kind === "done");
        if (page.events.length) setEvents((previous) => [...previous, ...page.events].slice(-MAX_EVENTS));
        setError(null);
      } catch (failure) {
        if (cancelled) return;
        setError(failure instanceof Error ? failure.message : String(failure));
      }
      if (done) {
        setMode("closed");
        return;
      }
      timer = setTimeout(poll, POLL_INTERVAL_MS);
    };
    void poll();

    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
    };
  }, [client, packetId]);

  return { events, mode, error };
}
