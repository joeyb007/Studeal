"use client";

import { useEffect, useMemo, useState } from "react";
import { HuntEvent, useHuntStream } from "@/lib/huntStream";
import styles from "./AgentCard.module.css";

export interface HuntSummary {
  id: number;
  watchlist_id: number;
  watchlist_name: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  offer_count: number;
  persisted_count: number;
  new_listing_count: number;
  error: string | null;
}

export interface AlertEvent {
  title: string;
  price: number;
  currency: string;
  score: number;
  url: string;
}

function eventTime(iso: string): string {
  return new Date(iso).toTimeString().slice(0, 8);
}

/** One in-flight hunt: the agent's live viewport, current action, counters,
 * and a collapsible activity log — all driven by the SSE stream. */
export default function AgentCard({
  hunt,
  onAlert,
}: {
  hunt: HuntSummary;
  onAlert: (alert: AlertEvent) => void;
}) {
  const { events, connected } = useHuntStream(hunt.watchlist_id);
  const [elapsed, setElapsed] = useState("0:00");

  interface Lane {
    marketplace: string;
    query: string;
    shot: string | null;
    action: string | null;
    pages: number;
    error: string | null;
    done: boolean;
    doneReason: string | null;
  }

  const derived = useMemo(() => {
    let pages = 0;
    let extractions = 0;
    let offers: number | null = null;
    let fresh: number | null = null;
    let finished: { status: string; duration: number } | null = null;
    const lanes = new Map<string, Lane>();

    const lane = (marketplace: string, query: string): Lane => {
      const key = `${marketplace}::${query}`;
      let l = lanes.get(key);
      if (!l) {
        l = { marketplace, query, shot: null, action: null, pages: 0, error: null, done: false, doneReason: null };
        lanes.set(key, l);
      }
      return l;
    };

    for (const event of events) {
      switch (event.type) {
        case "explorer.turn": {
          pages += 1;
          const l = lane(event.marketplace, event.query);
          l.pages += 1;
          l.action = event.action;
          l.error = null;
          break;
        }
        case "explorer.screenshot":
          lane(event.marketplace, event.query).shot = event.image_data_url;
          break;
        case "extraction.submitted":
          extractions += 1;
          break;
        case "explorer.error":
          lane(event.marketplace, event.query).error = event.error;
          break;
        case "lane.finished": {
          const l = lane(event.marketplace, event.query);
          l.done = true;
          l.doneReason = event.done_reason;
          if (event.pages > l.pages) l.pages = event.pages;
          break;
        }
        case "hunt.persisted":
          offers = event.offer_count;
          fresh = event.new_for_watchlist;
          break;
        case "hunt.finished":
          finished = { status: event.status, duration: event.duration_s };
          break;
      }
    }
    return { pages, extractions, offers, fresh, finished, lanes: [...lanes.values()] };
  }, [events]);

  // surface alert moments to the page (toast)
  useEffect(() => {
    const last = events[events.length - 1];
    if (last?.type === "alert.created") {
      onAlert({
        title: last.title, price: last.price, currency: last.currency,
        score: last.score, url: last.url,
      });
    }
  }, [events, onAlert]);

  useEffect(() => {
    const startMs = new Date(hunt.started_at).getTime();
    const tick = () => {
      const s = Math.max(0, Math.floor((Date.now() - startMs) / 1000));
      setElapsed(`${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`);
    };
    tick();
    const interval = setInterval(tick, 1000);
    return () => clearInterval(interval);
  }, [hunt.started_at]);

  const done = derived.finished !== null;
  const logEvents = useMemo(
    () =>
      events.filter(
        (e): e is Extract<HuntEvent, { type: "explorer.turn" | "extraction.submitted" | "explorer.error" | "alert.created" | "hunt.persisted" }> =>
          ["explorer.turn", "extraction.submitted", "explorer.error", "alert.created", "hunt.persisted"].includes(e.type),
      ).slice(-30).reverse(),
    [events],
  );

  return (
    <article className={styles.card}>
      <div className={styles.head}>
        <span className={styles.title}>{hunt.watchlist_name}</span>
        <span className={done ? styles.statusDone : styles.statusHunting}>
          {done
            ? derived.finished!.status === "cached"
              ? `Checked the fleet's latest findings — ${derived.fresh ?? 0} new match${derived.fresh === 1 ? "" : "es"}, no browse needed`
              : `Done · ${Math.round(derived.finished!.duration)}s`
            : connected
              ? "Hunting"
              : "Connecting"}
        </span>
        {!done && <span className={styles.elapsed}>{elapsed}</span>}
      </div>

      {derived.lanes.length === 0 ? (
        <div className={styles.shotEmpty}>agents dispatching…</div>
      ) : (
        <div className={styles.laneGrid}>
          {derived.lanes.map(l => (
            <div
              key={`${l.marketplace}::${l.query}`}
              className={[
                styles.lane,
                l.done ? styles.laneDone : "",
                l.error && !l.done ? styles.laneError : "",
              ].join(" ")}
            >
              <div className={styles.laneHead}>
                <span className={styles.laneMkt}>{l.marketplace.replace(/_/g, " ")}</span>
                <span className={styles.laneQuery} title={l.query}>{l.query}</span>
              </div>
              <div className={styles.laneShotWrap}>
                {l.done ? (
                  <div className={styles.laneDoneOverlay}>
                    <span className={styles.laneCheck}>✓</span>
                    <span className={styles.laneDoneReason}>
                      {l.doneReason ?? "complete"} · {l.pages} pages
                    </span>
                  </div>
                ) : l.shot ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={l.shot} alt={`${l.marketplace} viewport`} className={styles.laneShot} />
                ) : (
                  <div className={styles.laneWaiting}>connecting…</div>
                )}
              </div>
              {!l.done && (
                <span className={[styles.laneAction, l.error ? styles.laneActionError : ""].join(" ")}>
                  {l.error ?? l.action ?? "starting up"}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      <div className={styles.meta}>
        <span><b>{derived.pages}</b> pages</span>
        <span aria-hidden>·</span>
        <span><b>{derived.extractions}</b> extractions</span>
        <span aria-hidden>·</span>
        <span><b>{derived.offers ?? "—"}</b> offers</span>
        <span aria-hidden>·</span>
        <span className={styles.fresh}><b>{derived.fresh ?? "—"}</b> new</span>
      </div>

      <details className={styles.log}>
        <summary>Activity log</summary>
        <ul className={styles.ticker}>
          {logEvents.map((event, i) => (
            <li key={`${event.ts}-${i}`} className={event.type === "alert.created" ? styles.alertLine : event.type === "explorer.error" ? styles.errLine : ""}>
              <span className={styles.tTime}>{eventTime(event.ts)}</span>
              <span className={styles.tMkt}>
                {"marketplace" in event ? event.marketplace : event.type === "alert.created" ? "match" : "fleet"}
              </span>
              <span>
                {event.type === "explorer.turn" && event.action}
                {event.type === "extraction.submitted" && "page handed to extractor"}
                {event.type === "explorer.error" && event.error}
                {event.type === "alert.created" && `${event.title} — $${event.price}`}
                {event.type === "hunt.persisted" && `${event.persisted_count} listings saved, ${event.new_for_watchlist} new`}
              </span>
            </li>
          ))}
        </ul>
      </details>
    </article>
  );
}
