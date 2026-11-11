"use client";

import { useEffect, useRef, useState } from "react";
import type { ApiClient } from "./api";
import type { AuditEvent } from "./types";

const POLL_INTERVAL_MS = 1500;
const MAX_EVENTS = 1000;
const WS_OPEN_TIMEOUT_MS = 4000;

export type StreamMode = "idle" | "websocket" | "polling" | "closed";

/**
 * Live audit events. Tries the WebSocket through the same-origin proxy first; if the proxy cannot carry
 * WebSockets (common behind port forwarders), falls back to polling the event history endpoint.
 * Stops once a "done" event arrives.
 */
export function useAuditStream(client: ApiClient | null, token: string | null, packetId: string | null) {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [mode, setMode] = useState<StreamMode>("idle");
  const [error, setError] = useState<string | null>(null);
  const doneRef = useRef(false);

  useEffect(() => {
    setEvents([]);
    setError(null);
    doneRef.current = false;
    if (!client || !token || !packetId) {
      setMode("idle");
      return;
    }
    let cancelled = false;
    let socket: WebSocket | null = null;
    let pollTimer: ReturnType<typeof setTimeout> | null = null;
    let cursor = 0;
    let receivedOverSocket = false;

    const append = (incoming: AuditEvent[]) => {
      if (incoming.some((e) => e.kind === "done")) doneRef.current = true;
      setEvents((previous) => [...previous, ...incoming].slice(-MAX_EVENTS));
    };

    const poll = async () => {
      if (cancelled) return;
      try {
        const page = await client.getEvents(packetId, cursor);
        cursor = page.next;
        if (page.events.length) append(page.events);
        setError(null);
      } catch (pollError) {
        setError(pollError instanceof Error ? pollError.message : String(pollError));
      }
      if (cancelled) return;
      if (doneRef.current) {
        setMode("closed");
        return;
      }
      pollTimer = setTimeout(poll, POLL_INTERVAL_MS);
    };

    const startPolling = () => {
      if (cancelled || pollTimer !== null) return;
      socket?.close();
      setMode("polling");
      void poll();
    };

    try {
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${scheme}://${window.location.host}/api/ws/packets/${encodeURIComponent(packetId)}`);
    } catch {
      startPolling();
      return () => { cancelled = true; };
    }
    const openTimer = setTimeout(() => { if (!receivedOverSocket) startPolling(); }, WS_OPEN_TIMEOUT_MS);
    socket.onopen = () => socket?.send(JSON.stringify({ token }));
    socket.onmessage = (message) => {
      try {
        const event = JSON.parse(String(message.data)) as AuditEvent;
        if (!receivedOverSocket) {
          receivedOverSocket = true;
          setMode("websocket");
        }
        cursor += 1;
        append([event]);
        if (event.kind === "done") {
          setMode("closed");
          socket?.close();
        }
      } catch {
        setError("malformed event from engine");
      }
    };
    socket.onclose = (closeEvent) => {
      if (cancelled || doneRef.current) return;
      if (closeEvent.code === 4401) {
        setError("event stream rejected the token");
        setMode("closed");
        return;
      }
      startPolling(); // proxy without WebSocket support, or the connection dropped mid-audit: resume from cursor
    };

    return () => {
      cancelled = true;
      clearTimeout(openTimer);
      if (pollTimer !== null) clearTimeout(pollTimer);
      socket?.close();
    };
  }, [client, token, packetId]);

  return { events, mode, error, done: events.some((e) => e.kind === "done") };
}
