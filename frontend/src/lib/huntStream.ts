"use client";

import { useEffect, useState } from "react";

// Mirror of the backend event contract (dealbot/events/schema.py — see the
// WS B plan's SSE contract section). Flat JSON envelope on every variant.

export type HuntEventBase = {
  v: number;
  ts: string;
  hunt_id: number;
  watchlist_id: number;
};

export type HuntEvent = HuntEventBase &
  (
    | { type: "hunt.started" }
    | { type: "hunt.queries_planned"; queries: string[] }
    | {
        type: "explorer.turn";
        query: string;
        marketplace: string;
        turn: number;
        url: string;
        action: string;
        result: string;
      }
    | {
        type: "explorer.screenshot";
        query: string;
        marketplace: string;
        turn: number;
        image_data_url: string;
      }
    | { type: "explorer.error"; query: string; marketplace: string; error: string }
    | { type: "extraction.submitted"; query: string; marketplace: string }
    | {
        type: "hunt.persisted";
        offer_count: number;
        persisted_count: number;
        new_for_watchlist: number;
      }
    | {
        type: "hunt.finished";
        status: "succeeded" | "failed" | "cached";
        duration_s: number;
        error: string | null;
      }
    | {
        type: "alert.created";
        alert_id: number;
        listing_id: number;
        title: string;
        price: number;
        currency: string;
        score: number;
        url: string;
      }
  );

const MAX_EVENTS = 200;

export type HuntStream = {
  events: HuntEvent[];
  latestScreenshot: string | null;
  connected: boolean;
};

/** Live hunt events for one watchlist via the SSE proxy.
 * EventSource handles reconnection natively; unknown event types are ignored
 * for forward compatibility; the buffer keeps the last MAX_EVENTS. */
export function useHuntStream(watchlistId: number | null): HuntStream {
  const [events, setEvents] = useState<HuntEvent[]>([]);
  const [latestScreenshot, setLatestScreenshot] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (watchlistId == null) return;

    const source = new EventSource(`/api/stream/${watchlistId}`);
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    source.onmessage = (message) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(message.data);
      } catch {
        return;
      }
      if (
        typeof parsed !== "object" ||
        parsed === null ||
        typeof (parsed as { type?: unknown }).type !== "string"
      ) {
        return;
      }
      const event = parsed as HuntEvent;
      if (event.type === "explorer.screenshot") {
        setLatestScreenshot(event.image_data_url);
      }
      setEvents((prev) => {
        const next =
          prev.length >= MAX_EVENTS ? prev.slice(prev.length - MAX_EVENTS + 1) : prev.slice();
        next.push(event);
        return next;
      });
    };

    return () => {
      source.close();
      setConnected(false);
      setEvents([]);
      setLatestScreenshot(null);
    };
  }, [watchlistId]);

  return { events, latestScreenshot, connected };
}
