"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { usePathname } from "next/navigation";
import { useSession, signOut } from "next-auth/react";
import styles from "./Nav.module.css";

export default function Nav() {
  const pathname = usePathname();
  const { data: session } = useSession();
  const [menuOpen, setMenuOpen] = useState(false);
  const [confirmLogout, setConfirmLogout] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  const email = session?.user?.email ?? "";
  const initial = email ? email[0].toUpperCase() : "•";

  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

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

        <div className={styles.profileWrap} ref={menuRef}>
          <button
            className={[styles.avatarBtn, menuOpen ? styles.avatarOpen : ""].join(" ")}
            onClick={() => setMenuOpen((v) => !v)}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            aria-label="Account"
          >
            {initial}
          </button>
          {menuOpen && (
            <div className={styles.menu} role="menu">
              {email && <div className={styles.menuEmail}>{email}</div>}
              <Link
                href="/settings"
                role="menuitem"
                className={styles.menuItem}
                onClick={() => setMenuOpen(false)}
              >
                Settings
              </Link>
              <button
                role="menuitem"
                className={styles.menuItem}
                onClick={() => {
                  setMenuOpen(false);
                  setConfirmLogout(true);
                }}
              >
                Log out
              </button>
            </div>
          )}
        </div>
      </div>

      {confirmLogout && createPortal(
        // Portaled to <body>: the nav's backdrop-filter makes it the
        // containing block for fixed descendants, which pinned this overlay
        // to the nav strip instead of the viewport.
        <div className={styles.modalOverlay} onClick={() => setConfirmLogout(false)}>
          <div className={styles.modal} onClick={(e) => e.stopPropagation()}>
            <p className={styles.modalTitle}>Log out?</p>
            <p className={styles.modalBody}>Your agents keep hunting while you&apos;re gone.</p>
            <div className={styles.modalRow}>
              <button className={styles.modalGhost} onClick={() => setConfirmLogout(false)}>
                Stay
              </button>
              <button
                className={styles.modalPrimary}
                onClick={() => signOut({ callbackUrl: "/" })}
              >
                Log out
              </button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </nav>
  );
}
