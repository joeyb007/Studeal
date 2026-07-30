"use client";

import { useCallback, useEffect, useState } from "react";
import styles from "./AlertFeed.module.css";

export interface Alert {
  id: number;
  watchlist_id: number;
  watchlist_name: string;
  listing_id: number;
  title: string;
  price: number;
  currency: string;
  marketplace: string;
  url: string;
  image_url: string | null;
  score: number;
  reason: string | null;
  created_at: string;
  read_at: string | null;
}

function timeAgo(iso: string): string {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export default function AlertFeed() {
  const [alerts, setAlerts] = useState<Alert[] | null>(null);
  const [unread, setUnread] = useState(0);

  const load = useCallback(async () => {
    const res = await fetch("/api/alerts?limit=20");
    if (!res.ok) return;
    const data = await res.json();
    setAlerts(data.alerts);
    setUnread(data.unread_count);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function openAlert(alert: Alert) {
    window.open(alert.url, "_blank", "noopener,noreferrer");
    if (!alert.read_at) {
      setAlerts((prev) =>
        prev?.map((a) => (a.id === alert.id ? { ...a, read_at: new Date().toISOString() } : a)) ?? null,
      );
      setUnread((n) => Math.max(0, n - 1));
      await fetch(`/api/alerts/${alert.id}/read`, { method: "POST" });
    }
  }

  async function readAll() {
    setAlerts((prev) => prev?.map((a) => ({ ...a, read_at: a.read_at ?? new Date().toISOString() })) ?? null);
    setUnread(0);
    await fetch("/api/alerts/read-all", { method: "POST" });
  }

  if (alerts === null) return null;

  return (
    <section id="alerts" className={styles.feed}>
      <div className={styles.head}>
        <h2 className={styles.title}>
          Alerts
          {unread > 0 && <span className={styles.unread}>{unread} new</span>}
        </h2>
        {unread > 0 && (
          <button className={styles.readAll} onClick={readAll}>
            Mark all read
          </button>
        )}
      </div>

      {alerts.length === 0 ? (
        <p className={styles.empty}>
          Your agents hunt around the clock — new finds land here.
        </p>
      ) : (
        <ul className={styles.list}>
          {alerts.map((alert) => (
            <li key={alert.id}>
              <button className={styles.row} onClick={() => openAlert(alert)}>
                {!alert.read_at && <span className={styles.dot} aria-label="unread" />}
                {alert.image_url ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={alert.image_url} alt="" className={styles.thumb} />
                ) : (
                  <span className={styles.thumbEmpty} />
                )}
                <span className={styles.body}>
                  <span className={styles.rowTitle}>{alert.title}</span>
                  <span className={styles.rowMeta}>
                    ${alert.price.toFixed(2)} {alert.currency} · {alert.marketplace} ·{" "}
                    {alert.watchlist_name}
                    {alert.reason ? <span className={styles.reason}> — {alert.reason}</span> : null}
                  </span>
                </span>
                <span className={styles.right}>
                  <span className={styles.score}>{Math.round(alert.score * 100)}%</span>
                  <span className={styles.time}>{timeAgo(alert.created_at)}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
