"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useSession, signOut } from "next-auth/react";
import { useEffect, useState } from "react";
import styles from "./Nav.module.css";

export default function Nav() {
  const pathname = usePathname();
  const { data: session } = useSession();
  const isPro = session?.isPro ?? false;
  const [unread, setUnread] = useState(0);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const res = await fetch("/api/alerts?unread_only=true&limit=1");
        if (res.ok && !cancelled) {
          const data = await res.json();
          setUnread(data.unread_count ?? 0);
        }
      } catch {
        /* nav badge is best-effort */
      }
    }
    poll();
    const interval = setInterval(poll, 60_000);
    window.addEventListener("focus", poll);
    return () => {
      cancelled = true;
      clearInterval(interval);
      window.removeEventListener("focus", poll);
    };
  }, []);

  async function handleManageBilling() {
    const res = await fetch("/api/billing/portal", { method: "POST" });
    if (res.ok) {
      const { url } = await res.json();
      window.location.href = url;
    }
  }

  return (
    <nav className={styles.nav}>
      <Link href="/" className={styles.logo}>
        <img src="/logo.svg" alt="" className={styles.logoIcon} />
        studeal
      </Link>
      <div className={styles.links}>
        <Link href="/watchlists" className={[styles.link, pathname === "/watchlists" ? styles.active : ""].join(" ")}>
          My Agents
        </Link>
        <Link href="/mission-control" className={[styles.link, pathname === "/mission-control" ? styles.active : ""].join(" ")}>
          Mission Control
        </Link>
        <Link href="/dashboard" className={[styles.link, pathname === "/dashboard" ? styles.active : ""].join(" ")}>
          Daily Drops
        </Link>
        {unread > 0 && (
          <span className={styles.alertIndicator} title="New finds waiting on My Agents">
            {unread} new {unread === 1 ? "find" : "finds"}
          </span>
        )}
        {isPro && (
          <button className={styles.link} onClick={handleManageBilling}>Manage plan</button>
        )}
        <button className={styles.logoutBtn} onClick={() => signOut({ callbackUrl: "/" })}>Log out</button>
      </div>
    </nav>
  );
}
