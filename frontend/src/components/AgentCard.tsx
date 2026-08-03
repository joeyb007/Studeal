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
  next_hunt_at?: string | null;
}

export interface AlertEvent {
  title: string;
  price: number;
  currency: string;
  score: number;
  url: string;
}

/** Nav actions arrive as the model's raw JSON ('{"action":"scroll"}').
 *  Render words, not wire format. */
function prettyAction(raw: string | null): string | null {
  if (!raw) return raw;
  const trimmed = raw.trim();
  if (!trimmed.startsWith("{")) return raw;
  try {
    const parsed = JSON.parse(trimmed);
    const verb = parsed.action ?? "working";
    const detail = parsed.reason ?? parsed.text ?? parsed.url ?? parsed.id ?? null;
    return detail ? `${verb}: ${String(detail)}` : String(verb);
  } catch {
    return raw;
  }
}

/** done_reason arrives as the model's full explanation paragraph. The tile
 *  shows a clean completion state; prose only survives as a short label for
 *  the outcomes a user actually cares to distinguish. */
function prettyDone(reason: string | null): string | null {
  if (!reason) return null;
  const r = reason.toLowerCase();
  if (r.includes("no_results") || r.includes("no results")) return "no results here";
  if (r.includes("captcha")) return "blocked by captcha";
  if (r.includes("auth") || r.includes("login")) return "hit a login wall";
  if (r.includes("error")) return "stopped on an error";
  return null; // healthy completion needs no explanation
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

  // Refresh-survival: pub/sub has no replay, so seed lane state from the DB
  // and let the live stream overlay it. Screenshots re-arrive within a turn.
  interface SeedLane {
    query: string;
    marketplace: string;
    status: string;
    pages: number;
    done_reason: string | null;
  }
  const [seedLanes, setSeedLanes] = useState<SeedLane[]>([]);
  useEffect(() => {
    let cancelled = false;
    fetch(`/api/hunts/${hunt.id}/lanes`)
      .then(r => (r.ok ? r.json() : []))
      .then(data => {
        if (!cancelled && Array.isArray(data)) setSeedLanes(data);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [hunt.id]);

  interface Lane {
    marketplace: string;
    query: string;
    shot: string | null;
    action: string | null;
    pages: number;
    error: string | null;
    done: boolean;
    doneReason: string | null;
    queued: boolean;
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
        l = { marketplace, query, shot: null, action: null, pages: 0, error: null, done: false, doneReason: null, queued: false };
        lanes.set(key, l);
      }
      return l;
    };

    for (const seed of seedLanes) {
      const l = lane(seed.marketplace, seed.query);
      l.queued = seed.status === "queued";
      l.pages = seed.pages;
      if (seed.status === "done" || seed.status === "error") {
        l.done = true;
        l.doneReason = seed.done_reason;
      }
    }

    for (const event of events) {
      switch (event.type) {
        case "lanes.planned":
          for (const marketplace of event.marketplaces) {
            lane(marketplace, event.query).queued = true;
          }
          break;
        case "explorer.turn": {
          pages += 1;
          const l = lane(event.marketplace, event.query);
          l.queued = false;
          l.pages += 1;
          l.action = event.action;
          l.error = null;
          break;
        }
        case "explorer.screenshot": {
          const l = lane(event.marketplace, event.query);
          l.queued = false;
          l.shot = event.image_data_url;
          break;
        }
        case "extraction.submitted":
          extractions += 1;
          break;
        case "explorer.error":
          lane(event.marketplace, event.query).error = event.error;
          break;
        case "lane.finished": {
          const l = lane(event.marketplace, event.query);
          l.queued = false;
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
  }, [events, seedLanes]);

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

  const done = derived.finished !== null || hunt.status !== "running";

  const [countdown, setCountdown] = useState<string | null>(null);
  useEffect(() => {
    if (!done || !hunt.next_hunt_at) {
      setCountdown(null);
      return;
    }
    const target = new Date(hunt.next_hunt_at).getTime();
    const tick = () => {
      const ms = target - Date.now();
      if (ms <= 0) {
        setCountdown("any minute now");
        return;
      }
      const h = Math.floor(ms / 3_600_000);
      const m = Math.floor((ms % 3_600_000) / 60_000);
      setCountdown(h > 0 ? `${h}h ${m}m` : `${m}m`);
    };
    tick();
    const interval = setInterval(tick, 30_000);
    return () => clearInterval(interval);
  }, [done, hunt.next_hunt_at]);
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
              ? `Checked the fleet's latest findings: ${derived.fresh ?? 0} new match${derived.fresh === 1 ? "" : "es"}, no browse needed`
              : `Done · ${Math.round(derived.finished!.duration)}s`
            : connected
              ? "Hunting"
              : "Connecting"}
        </span>
        {!done && <span className={styles.elapsed}>{elapsed}</span>}
      </div>

      {done ? (
        <div className={styles.runComplete}>
          <span className={styles.runCheck}>✓</span>
          <div className={styles.runComplete_body}>
            <span className={styles.runCompleteTitle}>
              {hunt.status === "cached"
                ? "Served from the fleet's findings"
                : "Hunt complete"}
            </span>
            <span className={styles.runCompleteStats}>
              {derived.lanes.filter(l => l.done).length || "—"} lanes ·{" "}
              {hunt.persisted_count} listings · {hunt.new_listing_count} new
              {countdown ? ` · next hunt in ${countdown}` : ""}
            </span>
          </div>
        </div>
      ) : derived.lanes.length === 0 ? (
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
                    <span className={styles.laneDoneLabel}>Complete</span>
                    <span className={styles.laneDoneReason}>
                      {prettyDone(l.doneReason) ?? `${l.pages} page${l.pages === 1 ? "" : "s"} searched`}
                    </span>
                  </div>
                ) : l.shot ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={l.shot} alt={`${l.marketplace} viewport`} className={styles.laneShot} />
                ) : l.queued && !l.action ? (
                  <div className={styles.laneQueued}>queued</div>
                ) : (
                  <div className={styles.laneWaiting}>connecting…</div>
                )}
              </div>
              {!l.done && (
                <span className={[styles.laneAction, l.error ? styles.laneActionError : ""].join(" ")}>
                  {l.error ?? prettyAction(l.action) ?? "starting up"}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      <div className={styles.meta}>
        {derived.lanes.length > 0 && (
          <>
            <span>
              <b>{derived.lanes.filter(l => l.done).length}</b>/{derived.lanes.length} lanes
            </span>
            <span aria-hidden>·</span>
          </>
        )}
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
                {event.type === "explorer.turn" && prettyAction(event.action)}
                {event.type === "extraction.submitted" && "page handed to extractor"}
                {event.type === "explorer.error" && event.error}
                {event.type === "alert.created" && `${event.title} · $${event.price}`}
                {event.type === "hunt.persisted" && `${event.persisted_count} listings saved, ${event.new_for_watchlist} new`}
              </span>
            </li>
          ))}
        </ul>
      </details>
    </article>
  );
}
