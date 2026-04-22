"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useSession, signOut } from "next-auth/react";
import styles from "./Nav.module.css";

export default function Nav() {
  const pathname = usePathname();
  const { data: session } = useSession();
  const isPro = session?.isPro ?? false;

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
        <Link href="/dashboard" className={[styles.link, pathname === "/dashboard" ? styles.active : ""].join(" ")}>
          Daily Drops
        </Link>
        <Link href="/watchlists" className={[styles.link, pathname === "/watchlists" ? styles.active : ""].join(" ")}>
          Watchlists
        </Link>
        {isPro && (
          <button className={styles.link} onClick={handleManageBilling}>Manage plan</button>
        )}
        <button className={styles.logoutBtn} onClick={() => signOut({ callbackUrl: "/" })}>Log out</button>
      </div>
    </nav>
  );
}
