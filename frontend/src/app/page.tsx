"use client";

import { useState, useRef, useEffect } from "react";
import AgentWorkflow from "@/components/AgentWorkflow";
import AuthModal from "@/components/AuthModal";
import { useSession, signOut } from "next-auth/react";
import { useRouter } from "next/navigation";
import styles from "./page.module.css";

const EXAMPLE_QUERIES = [
  "AirPods Pro under $180",
  "cheap mechanical keyboard for studying",
  "laptop deals for college students",
  "Nintendo Switch games on sale",
  "dorm room essentials under $50",
  "noise cancelling headphones discount",
];

function useTypingPlaceholder(active: boolean): string {
  const [placeholder, setPlaceholder] = useState("What do you want to save on?");
  const queryIndex = useRef(0);
  const charIndex = useRef(0);
  const deleting = useRef(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout>>(undefined);

  useEffect(() => {
    if (!active) return;

    const tick = () => {
      const current = EXAMPLE_QUERIES[queryIndex.current];

      if (!deleting.current) {
        charIndex.current += 1;
        setPlaceholder(current.slice(0, charIndex.current));
        if (charIndex.current === current.length) {
          deleting.current = true;
          timeoutRef.current = setTimeout(tick, 1800);
          return;
        }
        timeoutRef.current = setTimeout(tick, 55);
      } else {
        charIndex.current -= 1;
        setPlaceholder(current.slice(0, charIndex.current));
        if (charIndex.current === 0) {
          deleting.current = false;
          queryIndex.current = (queryIndex.current + 1) % EXAMPLE_QUERIES.length;
          timeoutRef.current = setTimeout(tick, 400);
          return;
        }
        timeoutRef.current = setTimeout(tick, 28);
      }
    };

    timeoutRef.current = setTimeout(tick, 800);
    return () => clearTimeout(timeoutRef.current);
  }, [active]);

  return placeholder;
}

const ENTER_DURATION = 800;

export default function Home() {
  const { data: session } = useSession();
  const isLoggedIn = !!session;
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [workflowStarted, setWorkflowStarted] = useState(false);
  const [authModal, setAuthModal] = useState<{ open: boolean; tab: "login" | "signup" }>({ open: false, tab: "login" });
  const inputRef = useRef<HTMLInputElement>(null);
  const placeholder = useTypingPlaceholder(query === "");

  useEffect(() => {
    const t = setTimeout(() => setWorkflowStarted(true), ENTER_DURATION);
    return () => clearTimeout(t);
  }, []);

  const handleSearch = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!query.trim()) return;

    if (isLoggedIn) {
      router.push(`/dashboard?q=${encodeURIComponent(query.trim())}`);
    } else {
      setAuthModal({ open: true, tab: "signup" });
    }
  };

  return (
    <>
    <main className={`${styles.main} pageEnter`}>
        <nav className={[styles.nav, styles.enterDone].join(" ")}
          style={{ animationDelay: "0ms" }}>
          <div className={styles.wordmark}>
            <img src="/logo.svg" alt="" className={styles.logoIcon} />
            studeal
          </div>
          <div className={styles.navLinks}>
            {isLoggedIn ? (
              <>
                <a href="/dashboard" className={styles.navLink}>Daily Drops</a>
                <a href="/watchlists" className={styles.navLink}>Watchlists</a>
                <button className={styles.navLink} onClick={() => signOut({ callbackUrl: "/" })}>Log out</button>
              </>
            ) : (
              <>
                <button className={styles.navLink} onClick={() => setAuthModal({ open: true, tab: "login" })}>Log in</button>
                <button className={styles.navSignup} onClick={() => setAuthModal({ open: true, tab: "signup" })}>Sign up</button>
              </>
            )}
          </div>
        </nav>

        <section className={styles.hero}>
          <div className={styles.heroLeft}>
          <p className={[styles.eyebrow, styles.enterDone].join(" ")}
            style={{ animationDelay: "80ms" }}>
            AI deal hunting for students
          </p>
          <h1 className={[styles.headline, styles.enterDone].join(" ")}
            style={{ animationDelay: "160ms" }}>
            Never overpay<br />
            for <em className={styles.headlineItalic}>anything.</em>
          </h1>
          <p className={[styles.subline, styles.enterDone].join(" ")}
            style={{ animationDelay: "240ms" }}>
            Tell us what you want. We watch the internet and alert you when the price is right.
          </p>

          <form onSubmit={handleSearch}
            className={[styles.searchForm, styles.enterDone].join(" ")}
            style={{ animationDelay: "320ms" }}>
            <input
              ref={inputRef}
              type="text"
              className={styles.searchInput}
              placeholder={placeholder}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              autoFocus
            />
            <button
              type="submit"
              className={styles.searchBtn}
              disabled={!query.trim()}
            >
              Hunt deals
            </button>
          </form>
          </div>

          <div className={[styles.heroRight, styles.enterDone].join(" ")}
            style={{ animationDelay: "420ms" }}>
            <AgentWorkflow started={workflowStarted} />
          </div>
        </section>
      </main>

      <AuthModal
        isOpen={authModal.open}
        defaultTab={authModal.tab}
        onClose={() => setAuthModal(v => ({ ...v, open: false }))}
      />
    </>
  );
}
