"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Nav from "@/components/Nav";
import AgentCard, { AlertEvent, HuntSummary } from "@/components/AgentCard";
import styles from "./page.module.css";

const POLL_MS = 30_000;

function timeOf(iso: string): string {
  return new Date(iso).toTimeString().slice(0, 5);
}

function durationOf(hunt: HuntSummary): string {
  if (!hunt.finished_at) return "—";
  const s = Math.round(
    (new Date(hunt.finished_at).getTime() - new Date(hunt.started_at).getTime()) / 1000,
  );
  return `${s}s`;
}

export default function MissionControlPage() {
  const [hunts, setHunts] = useState<HuntSummary[] | null>(null);
  const [toast, setToast] = useState<AlertEvent | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    const res = await fetch("/api/hunts?limit=30");
    if (!res.ok) return;
    const data = await res.json();
    setHunts(data.hunts);
  }, []);

  // Load-state on mount + poll: new hunts started by the scheduler appear
  // within one tick without a fleet-wide channel.
  useEffect(() => {
    load();
    const interval = setInterval(load, POLL_MS);
    return () => clearInterval(interval);
  }, [load]);

  const showToast = useCallback((alert: AlertEvent) => {
    setToast(alert);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 3200);
  }, []);

  const running = hunts?.filter((h) => h.status === "running") ?? [];
  const recent = hunts?.filter((h) => h.status !== "running").slice(0, 8) ?? [];
  const today = new Date().toDateString();
  const huntsToday = hunts?.filter((h) => new Date(h.started_at).toDateString() === today) ?? [];
  const newToday = huntsToday.reduce((n, h) => n + h.new_listing_count, 0);

  return (
    <>
      <Nav />
      <main className={styles.wrap}>
        <header className={styles.pagehead}>
          <h1>Mission Control</h1>
          <span className={styles.liveCount}>
            {running.length > 0 ? (
              <>
                <span className={styles.liveDot} />
                {running.length} agent{running.length === 1 ? "" : "s"} hunting
              </>
            ) : (
              "fleet idle — agents sweep on schedule"
            )}
          </span>
        </header>

        {hunts !== null && (
          <div className={styles.props}>
            <span>Today <b>{huntsToday.length} hunt{huntsToday.length === 1 ? "" : "s"}</b></span>
            <span>New finds <b>{newToday}</b></span>
            <span>Live <b>{running.length}</b></span>
          </div>
        )}

        {hunts === null ? null : running.length === 0 ? (
          <div className={styles.empty}>
            No hunts in flight. Your agents run on schedule — when one wakes up,
            you&apos;ll see it working here.
          </div>
        ) : (
          <div className={styles.cards}>
            {running.map((hunt) => (
              <AgentCard key={hunt.id} hunt={hunt} onAlert={showToast} />
            ))}
          </div>
        )}

        {recent.length > 0 && (
          <section className={styles.rail}>
            <h2>Recent hunts</h2>
            <div className={styles.railRows}>
              {recent.map((hunt) => (
                <div key={hunt.id} className={styles.railRow}>
                  <span className={styles.railName}>{hunt.watchlist_name}</span>
                  {hunt.status === "failed" ? (
                    <span className={styles.railStatusFailed}>failed</span>
                  ) : (
                    <span className={styles.railStatus}>
                      {hunt.persisted_count} found · {hunt.new_listing_count} new
                    </span>
                  )}
                  <span className={styles.railMeta}>
                    {durationOf(hunt)} · {timeOf(hunt.started_at)}
                  </span>
                </div>
              ))}
            </div>
          </section>
        )}
      </main>

      <div
        className={[styles.toast, toast ? styles.toastShow : ""].join(" ")}
        aria-live="polite"
      >
        {toast && (
          <>
            <div className={styles.toastTitle}>{toast.title}</div>
            <div className={styles.toastMeta}>
              ${toast.price.toFixed(2)} {toast.currency} · {Math.round(toast.score * 100)}% match
            </div>
          </>
        )}
      </div>
    </>
  );
}
