"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useSession, signOut } from "next-auth/react";
import styles from "./Nav.module.css";

export default function Nav() {
  const pathname = usePathname();
  useSession();    // keeps the session fresh while the nav is mounted

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
        <Link href="/dashboard" className={[styles.link, pathname === "/dashboard" ? styles.active : ""].join(" ")}>
          Daily Drops
        </Link>
        <Link href="/scout" className={[styles.link, pathname === "/scout" ? styles.active : ""].join(" ")}>
          Scout
        </Link>
        <Link href="/settings" className={[styles.link, pathname === "/settings" ? styles.active : ""].join(" ")}>
          Settings
        </Link>
        <button className={styles.logoutBtn} onClick={() => signOut({ callbackUrl: "/" })}>Log out</button>
      </div>
    </nav>
  );
}
