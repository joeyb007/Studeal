"use client";

import { useState, useRef, useEffect } from "react";
import AgentWorkflow from "@/components/AgentWorkflow";
import PipelineVisualizer from "@/components/PipelineVisualizer";
import AuthModal from "@/components/AuthModal";
import { useAuth } from "@/lib/auth";
import styles from "./page.module.css";

interface Deal {
  id: number;
  title: string;
  source: string;
  url: string;
  listed_price: number;
  sale_price: number;
  score: number;
  alert_tier: string;
  real_discount_pct: number | null;
  student_eligible: boolean;
}

type Phase = "idle" | "running" | "done";

const STAGE_DELAYS = [800, 1600, 2800, 4200];

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

const ENTER_DURATION = 800; // ms for entrance to complete before workflow starts

export default function Home() {
  const { isLoggedIn } = useAuth();
  const [query, setQuery] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [activeStage, setActiveStage] = useState(-1);
  const [deals, setDeals] = useState<Deal[]>([]);
  const [workflowStarted, setWorkflowStarted] = useState(false);
  const [authModal, setAuthModal] = useState<{ open: boolean; tab: "login" | "signup" }>({ open: false, tab: "login" });
  const inputRef = useRef<HTMLInputElement>(null);
  const placeholder = useTypingPlaceholder(phase === "idle" && query === "");

  useEffect(() => {
    const t = setTimeout(() => setWorkflowStarted(true), ENTER_DURATION);
    return () => clearTimeout(t);
  }, []);

  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!query.trim() || phase === "running") return;

    setPhase("running");
    setActiveStage(0);
    setDeals([]);

    STAGE_DELAYS.forEach((delay, i) => {
      setTimeout(() => setActiveStage(i), delay);
    });

    setTimeout(async () => {
      setActiveStage(4);
      setPhase("done");

      if (!isLoggedIn) {
        setAuthModal({ open: true, tab: "signup" });
        return;
      }

      try {
        const res = await fetch(`/api/deals?limit=6`);
        if (res.ok) {
          const data = await res.json();
          setDeals(data);
        }
      } catch {
        // Show empty state gracefully
      }
    }, 5500);
  };

  return (
    <>
    <main className={styles.main}>
        <nav className={[styles.nav, styles.enterDone].join(" ")}
          style={{ animationDelay: "0ms" }}>
          <div className={styles.wordmark}>
            <img src="/logo.svg" alt="" className={styles.logoIcon} />
            studeal
          </div>
          <div className={styles.navLinks}>
            <button className={styles.navLink} onClick={() => setAuthModal({ open: true, tab: "login" })}>Log in</button>
            <button className={styles.navSignup} onClick={() => setAuthModal({ open: true, tab: "signup" })}>Sign up</button>
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
              disabled={phase === "running"}
              autoFocus
            />
            <button
              type="submit"
              className={styles.searchBtn}
              disabled={!query.trim() || phase === "running"}
            >
              {phase === "running" ? "Hunting..." : "Hunt deals"}
            </button>
          </form>

          {phase !== "idle" && (
            <div className={styles.pipelineWrapper}>
              <PipelineVisualizer activeStage={activeStage} />
            </div>
          )}
          </div>

          <div className={[styles.heroRight, styles.enterDone].join(" ")}
            style={{ animationDelay: "420ms" }}>
            <AgentWorkflow started={workflowStarted} />
          </div>
        </section>

        {deals.length > 0 && (
          <section className={styles.results}>
            <div className={styles.resultsGrid}>
              {deals.map((deal) => (
                <a
                  key={deal.id}
                  href={deal.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className={styles.dealCard}
                >
                  <div className={styles.dealHeader}>
                    <span className={styles.dealSource}>{deal.source}</span>
                    <div className={styles.dealBadges}>
                      {deal.student_eligible && (
                        <span className={styles.tierStudent}>student</span>
                      )}
                      <span className={[
                        styles.dealTier,
                        deal.alert_tier === "push" ? styles.tierPush : "",
                        deal.alert_tier === "digest" ? styles.tierDigest : "",
                      ].join(" ")}>
                        {deal.alert_tier}
                      </span>
                    </div>
                  </div>
                  <p className={styles.dealTitle}>{deal.title}</p>
                  <div className={styles.dealPricing}>
                    <span className={styles.salePrice}>${deal.sale_price.toFixed(2)}</span>
                    {deal.listed_price > deal.sale_price && (
                      <span className={styles.listedPrice}>${deal.listed_price.toFixed(2)}</span>
                    )}
                    {deal.real_discount_pct && (
                      <span className={styles.discount}>{deal.real_discount_pct.toFixed(0)}% off</span>
                    )}
                  </div>
                  <div className={styles.dealScore}>
                    <div className={styles.scoreBarTrack}>
                      <div className={styles.scoreBar} style={{ width: `${deal.score}%` }} />
                    </div>
                    <span className={styles.scoreLabel}>{deal.score}/100</span>
                  </div>
                </a>
              ))}
            </div>

            <div className={styles.cta}>
              <p className={styles.ctaText}>Save this watchlist and get daily alerts</p>
              <button className={styles.ctaBtn} onClick={() => setAuthModal({ open: true, tab: "signup" })}>Create free account →</button>
            </div>
          </section>
        )}

        {phase === "done" && deals.length === 0 && (
          <div className={styles.emptyState}>
            <p>No deals found today for that search. Try something else or check back tomorrow.</p>
          </div>
        )}
      </main>

      <AuthModal
        isOpen={authModal.open}
        defaultTab={authModal.tab}
        onClose={() => setAuthModal(v => ({ ...v, open: false }))}
      />
    </>
  );
}
