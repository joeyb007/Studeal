"use client";

import { useEffect, useRef, useState } from "react";
import { useSession } from "next-auth/react";
import Link from "next/link";
import Nav from "@/components/Nav";
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
  category: string;
  condition: string;
  real_discount_pct: number | null;
  student_eligible: boolean;
  scraped_at: string;
}

const CONDITION_LABELS: Record<string, string> = { new: "New", used: "Used", refurb: "Refurb" };
const CONDITION_CLASS: Record<string, string> = {
  new: styles.condNew,
  used: styles.condUsed,
  refurb: styles.condRefurb,
};
const TIER_LABELS: Record<string, string> = { push: "Hot", digest: "Good", none: "Mild" };
const TIER_CLASS: Record<string, string> = {
  push: styles.tierPush,
  digest: styles.tierDigest,
  none: styles.tierNone,
};

const PLACEHOLDERS = [
  "noise cancelling headphones under $100",
  "affordable laptop for college",
  "gaming gear on sale",
  "iPad deal for students",
  "mechanical keyboard cheap",
];

function pct(listed: number, sale: number) {
  return Math.round(((listed - sale) / listed) * 100);
}

function DealCard({ deal }: { deal: Deal }) {
  const discount = deal.real_discount_pct ?? pct(deal.listed_price, deal.sale_price);
  return (
    <a href={deal.url} target="_blank" rel="noopener noreferrer" className={styles.card}>
      <div className={styles.discountBadge}>−{discount}%</div>
      <div className={styles.cardBody}>
        <div className={styles.cardMeta}>
          <span className={styles.source}>{deal.source}</span>
          <span className={styles.category}>{deal.category}</span>
        </div>
        <p className={styles.title}>{deal.title}</p>
        <div className={styles.prices}>
          <span className={styles.salePrice}>${deal.sale_price.toFixed(2)}</span>
          <span className={styles.listedPrice}>${deal.listed_price.toFixed(2)}</span>
        </div>
      </div>
      <div className={styles.cardFooter}>
        <div className={styles.badges}>
          {deal.student_eligible && <span className={styles.studentBadge}>Student</span>}
          {deal.condition && deal.condition !== "unknown" && (
            <span className={[styles.condBadge, CONDITION_CLASS[deal.condition] ?? ""].join(" ")}>
              {CONDITION_LABELS[deal.condition] ?? deal.condition}
            </span>
          )}
          <span className={[styles.tierBadge, TIER_CLASS[deal.alert_tier] ?? ""].join(" ")}>
            {TIER_LABELS[deal.alert_tier] ?? deal.alert_tier}
          </span>
        </div>
        <div className={styles.scoreWrap}>
          <div className={styles.scoreTrack}>
            <div className={styles.scoreFill} style={{ width: `${deal.score}%` }} />
          </div>
          <span className={styles.scoreNum}>{deal.score}</span>
        </div>
      </div>
    </a>
  );
}

export default function DashboardPage() {
  const { data: session } = useSession();
  const token = session?.accessToken;
  const [query, setQuery] = useState("");
  const [deals, setDeals] = useState<Deal[]>([]);
  const [searching, setSearching] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  const [placeholderIdx, setPlaceholderIdx] = useState(0);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Rotate placeholder
  useEffect(() => {
    const id = setInterval(() => {
      setPlaceholderIdx(i => (i + 1) % PLACEHOLDERS.length);
    }, 3000);
    return () => clearInterval(id);
  }, []);

  // Debounced search
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      if (!query.trim()) {
        setDeals([]);
        setHasSearched(false);
        return;
      }
      setSearching(true);
      fetch(`/api/deals/search?q=${encodeURIComponent(query.trim())}`)
        .then(r => r.json())
        .then(data => {
          setDeals(Array.isArray(data) ? data : []);
          setHasSearched(true);
          setSearching(false);
        })
        .catch(() => setSearching(false));
    }, 400);
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current); };
  }, [query, token]);

  const hasResults = hasSearched && deals.length > 0;
  const isEmpty = hasSearched && deals.length === 0 && !searching;

  return (
    <>
      <Nav />
      <main className={[styles.main, hasResults ? styles.mainShifted : ""].join(" ")}>

        {/* Search hero */}
        <div className={[styles.hero, hasResults ? styles.heroTop : ""].join(" ")}>
          {!hasResults && (
            <>
              <h1 className={styles.heading}>Daily Drops</h1>
              <p className={styles.subheading}>Describe what you&apos;re looking for</p>
            </>
          )}
          <div className={styles.searchWrap}>
            <svg className={styles.searchIcon} width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
            <input
              ref={inputRef}
              className={styles.searchInput}
              type="text"
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder={`Try: ${PLACEHOLDERS[placeholderIdx]}`}
              autoFocus
            />
            {query && (
              <button className={styles.clearSearch} onClick={() => setQuery("")}>✕</button>
            )}
          </div>
          {searching && <p className={styles.searching}>Finding deals…</p>}
        </div>

        {/* Results */}
        {hasResults && (
          <div className={styles.results}>
            <div className={styles.resultsHeader}>
              <span className={styles.resultsCount}>{deals.length} deals found</span>
            </div>
            <div className={styles.grid}>
              {deals.map(deal => <DealCard key={deal.id} deal={deal} />)}
            </div>
            <div className={styles.catalogLink}>
              <Link href="/catalog">Browse all deals →</Link>
            </div>
          </div>
        )}

        {isEmpty && (
          <div className={styles.empty}>
            Nothing matched — try describing what you need differently.
          </div>
        )}

        {!hasSearched && !searching && (
          <div className={styles.catalogLink}>
            <Link href="/catalog">Browse all deals →</Link>
          </div>
        )}
      </main>
    </>
  );
}
