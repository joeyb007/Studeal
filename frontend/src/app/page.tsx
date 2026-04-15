"use client";

import { useState, useRef } from "react";
import ParticleField from "@/components/ParticleField";
import PipelineVisualizer from "@/components/PipelineVisualizer";
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
}

type Phase = "idle" | "running" | "done";

const STAGE_DELAYS = [800, 1600, 2800, 4200];

export default function Home() {
  const [query, setQuery] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [activeStage, setActiveStage] = useState(-1);
  const [deals, setDeals] = useState<Deal[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);

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
      try {
        const res = await fetch(`/api/deals?limit=6`);
        if (res.ok) {
          const data = await res.json();
          setDeals(data);
        }
      } catch {
        // Show empty state gracefully
      } finally {
        setActiveStage(4);
        setPhase("done");
      }
    }, 5500);
  };

  return (
    <>
      <ParticleField active={phase === "running"} />

      <main className={styles.main}>
        <nav className={styles.nav}>
          <span className={styles.wordmark}>studeal</span>
          <div className={styles.navLinks}>
            <a href="/login" className={styles.navLink}>Log in</a>
            <a href="/signup" className={styles.navSignup}>Sign up</a>
          </div>
        </nav>

        <section className={styles.hero}>
          <p className={styles.eyebrow}>AI deal hunting for students</p>
          <h1 className={styles.headline}>
            Never overpay<br />for anything.
          </h1>
          <p className={styles.subline}>
            Tell us what you want. We watch the internet and alert you when the price is right.
          </p>

          <form onSubmit={handleSearch} className={styles.searchForm}>
            <input
              ref={inputRef}
              type="text"
              className={styles.searchInput}
              placeholder="What do you want to save on?"
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
                    <span className={[
                      styles.dealTier,
                      deal.alert_tier === "push" ? styles.tierPush : "",
                      deal.alert_tier === "digest" ? styles.tierDigest : "",
                    ].join(" ")}>
                      {deal.alert_tier}
                    </span>
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
              <a href="/signup" className={styles.ctaBtn}>Create free account →</a>
            </div>
          </section>
        )}

        {phase === "done" && deals.length === 0 && (
          <div className={styles.emptyState}>
            <p>No deals found today for that search. Try something else or check back tomorrow.</p>
          </div>
        )}
      </main>
    </>
  );
}
