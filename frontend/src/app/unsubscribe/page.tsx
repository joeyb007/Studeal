"use client";

import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import styles from "./page.module.css";

const TYPE_LABELS: Record<string, string> = {
  alerts: "new-match alerts",
  price_drops: "price-drop emails",
  digest: "the daily digest",
};

function UnsubscribeInner() {
  const params = useSearchParams();
  const token = params.get("token");
  const [state, setState] = useState<"working" | "done" | "error">("working");
  const [emailType, setEmailType] = useState<string | null>(null);

  useEffect(() => {
    if (!token) {
      setState("error");
      return;
    }
    let cancelled = false;
    fetch("/api/email/unsubscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    })
      .then(async (res) => {
        if (cancelled) return;
        if (!res.ok) {
          setState("error");
          return;
        }
        const data = await res.json();
        setEmailType(data.type ?? null);
        setState("done");
      })
      .catch(() => {
        if (!cancelled) setState("error");
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  return (
    <div className={styles.card}>
      {state === "working" && <p className={styles.body}>One moment&hellip;</p>}
      {state === "done" && (
        <>
          <h1 className={styles.title}>You&apos;re unsubscribed</h1>
          <p className={styles.body}>
            We won&apos;t send you {TYPE_LABELS[emailType ?? ""] ?? "these emails"} anymore.
            You can turn them back on any time in{" "}
            <Link href="/settings" className={styles.link}>settings</Link>.
          </p>
        </>
      )}
      {state === "error" && (
        <>
          <h1 className={styles.title}>That link didn&apos;t work</h1>
          <p className={styles.body}>
            It may have expired. You can manage every email type from{" "}
            <Link href="/settings" className={styles.link}>settings</Link>.
          </p>
        </>
      )}
    </div>
  );
}

export default function UnsubscribePage() {
  return (
    <main className={styles.main}>
      <nav className={styles.nav}>
        <Link href="/" className={styles.wordmark}>
          <img src="/logo.svg" alt="" className={styles.logoIcon} />
          studeal
        </Link>
      </nav>
      <Suspense fallback={null}>
        <UnsubscribeInner />
      </Suspense>
    </main>
  );
}
