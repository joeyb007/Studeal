"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { signOut } from "next-auth/react";
import styles from "./Nav.module.css";

export default function Nav() {
  const pathname = usePathname();

  return (
    <nav className={styles.nav}>
      <Link href="/" className={styles.logo}>
        <img src="/logo.svg" alt="" className={styles.logoIcon} />
        studeal
      </Link>
      <div className={styles.links}>
        <Link href="/dashboard" className={[styles.link, pathname === "/dashboard" ? styles.active : ""].join(" ")}>
          Deals
        </Link>
        <Link href="/watchlists" className={[styles.link, pathname === "/watchlists" ? styles.active : ""].join(" ")}>
          Watchlists
        </Link>
        <button className={styles.logoutBtn} onClick={() => signOut({ callbackUrl: "/" })}>Log out</button>
      </div>
    </nav>
  );
}
