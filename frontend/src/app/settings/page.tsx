"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { signOut, useSession } from "next-auth/react";
import styles from "./page.module.css";

type Prefs = { alerts: boolean; price_drops: boolean; digest: boolean };
type Me = { email: string; is_pro: boolean; has_password: boolean };

const PREF_ROWS: { key: keyof Prefs; label: string; hint: string }[] = [
  { key: "alerts", label: "New-match alerts", hint: "When an agent finds fresh listings worth a look" },
  { key: "price_drops", label: "Price drops", hint: "When a listing Scout inspected for you gets cheaper" },
  { key: "digest", label: "Daily digest", hint: "One morning summary of everything overnight (Pro)" },
];

export default function SettingsPage() {
  const { status } = useSession();
  const [me, setMe] = useState<Me | null>(null);
  const [prefs, setPrefs] = useState<Prefs | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const [curPw, setCurPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [pwNote, setPwNote] = useState<string | null>(null);

  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deletePw, setDeletePw] = useState("");
  const [deleteNote, setDeleteNote] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/me").then(async (r) => r.ok && setMe(await r.json())).catch(() => {});
    fetch("/api/email/prefs").then(async (r) => r.ok && setPrefs(await r.json())).catch(() => {});
  }, []);

  const togglePref = async (key: keyof Prefs) => {
    if (!prefs) return;
    const next = { ...prefs, [key]: !prefs[key] };
    setPrefs(next);
    const res = await fetch("/api/email/prefs", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [key]: next[key] }),
    }).catch(() => null);
    if (!res?.ok) {
      setPrefs(prefs);
      setNote("Couldn't save that. Try again.");
    } else {
      setNote(null);
    }
  };

  const changePassword = async () => {
    setPwNote(null);
    const res = await fetch("/api/auth/account", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "change-password", current_password: curPw, new_password: newPw }),
    }).catch(() => null);
    if (res?.ok) {
      setPwNote("Password updated.");
      setCurPw("");
      setNewPw("");
    } else {
      const data = await res?.json().catch(() => null);
      setPwNote(data?.detail ?? "Couldn't update the password.");
    }
  };

  const openPortal = async () => {
    const res = await fetch("/api/billing/portal", { method: "POST" }).catch(() => null);
    const data = await res?.json().catch(() => null);
    if (data?.url) window.location.href = data.url;
  };

  const deleteAccount = async () => {
    setDeleteNote(null);
    const res = await fetch("/api/auth/account", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "delete-account",
        ...(me?.has_password ? { password: deletePw } : {}),
      }),
    }).catch(() => null);
    if (res?.ok) {
      await signOut({ callbackUrl: "/" });
    } else {
      const data = await res?.json().catch(() => null);
      setDeleteNote(data?.detail ?? "Couldn't delete the account.");
    }
  };

  if (status === "unauthenticated") {
    return (
      <main className={styles.main}>
        <div className={styles.card}>
          <p className={styles.hint}>
            <Link href="/" className={styles.link}>Sign in</Link> to manage your account.
          </p>
        </div>
      </main>
    );
  }

  return (
    <main className={styles.main}>
      <header className={styles.header}>
        <h1 className={styles.title}>Settings</h1>
        {me && <span className={styles.email}>{me.email}</span>}
      </header>

      <section className={styles.card}>
        <h2 className={styles.sectionTitle}>Emails</h2>
        {!prefs && <p className={styles.hint}>Loading&hellip;</p>}
        {prefs && PREF_ROWS.map(({ key, label, hint }) => (
          <div key={key} className={styles.prefRow}>
            <div>
              <div className={styles.prefLabel}>{label}</div>
              <div className={styles.hint}>{hint}</div>
            </div>
            <button
              className={`${styles.toggle} ${prefs[key] ? styles.toggleOn : ""}`}
              onClick={() => togglePref(key)}
              aria-pressed={prefs[key]}
            >
              <span className={styles.knob} />
            </button>
          </div>
        ))}
        {note && <p className={styles.error}>{note}</p>}
      </section>

      {me?.has_password && (
        <section className={styles.card}>
          <h2 className={styles.sectionTitle}>Password</h2>
          <input
            className={styles.input} type="password" placeholder="Current password"
            value={curPw} onChange={(e) => setCurPw(e.target.value)}
          />
          <input
            className={styles.input} type="password" placeholder="New password (8+ characters)"
            value={newPw} onChange={(e) => setNewPw(e.target.value)}
          />
          <button
            className={styles.button}
            disabled={!curPw || newPw.length < 8}
            onClick={changePassword}
          >
            Update password
          </button>
          {pwNote && <p className={styles.hint}>{pwNote}</p>}
        </section>
      )}

      <section className={styles.card}>
        <h2 className={styles.sectionTitle}>Plan &amp; billing</h2>
        <p className={styles.hint}>
          {me?.is_pro
            ? "You're on Pro · 5 agents, live hunts, auto-inspections, daily digest."
            : "You're on the free plan · 1 agent, pool-served refreshes."}
        </p>
        {me?.is_pro ? (
          <button className={styles.button} onClick={openPortal}>Manage billing</button>
        ) : (
          <Link href="/watchlists" className={styles.button}>Upgrade to Pro</Link>
        )}
      </section>

      <section className={`${styles.card} ${styles.danger}`}>
        <h2 className={styles.sectionTitle}>Delete account</h2>
        <p className={styles.hint}>
          Deletes your agents, alerts, and Scout conversations, and cancels any
          subscription. This can&apos;t be undone.
        </p>
        {!confirmDelete ? (
          <button className={styles.dangerButton} onClick={() => setConfirmDelete(true)}>
            Delete my account
          </button>
        ) : (
          <div className={styles.confirmBlock}>
            {me?.has_password && (
              <input
                className={styles.input} type="password" placeholder="Confirm your password"
                value={deletePw} onChange={(e) => setDeletePw(e.target.value)}
              />
            )}
            <div className={styles.confirmRow}>
              <button
                className={styles.dangerButton}
                disabled={Boolean(me?.has_password) && !deletePw}
                onClick={deleteAccount}
              >
                Yes, delete everything
              </button>
              <button className={styles.button} onClick={() => setConfirmDelete(false)}>
                Keep my account
              </button>
            </div>
          </div>
        )}
        {deleteNote && <p className={styles.error}>{deleteNote}</p>}
      </section>
    </main>
  );
}
