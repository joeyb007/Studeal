"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import PoolCard, { MARKETPLACE_LABELS, PoolListing } from "@/components/PoolCard";
import { MultiSelect } from "@/components/Select";
import styles from "./page.module.css";

// Daily Drops IS the catalog: one page over the whole live pool. NL-query it
// or browse everything; the prefilters (store, condition, price) apply to
// both modes server-side. The only boundary is the lifecycle staleness rule.

const MARKETPLACES = Object.keys(MARKETPLACE_LABELS).filter(m => m !== "studeal");
const CONDITIONS: Array<[string, string]> = [
  ["new", "New"],
  ["used", "Used"],
  ["refurb", "Refurb"],
];
const PAGE_SIZE = 30;
const SEARCH_MAX = 300;

// Full sentences, not keywords: the bar takes natural language and embeds it.
const PLACEHOLDERS = [
  "a beginner-friendly road bike, nothing over $400",
  "quiet mechanical keyboard for late-night work",
  "lightly used monitor for a dorm desk",
  "noise cancelling headphones that aren't beat up",
  "a cheap laptop that can handle coursework",
];


function useCountUp(target: number | null, ms = 700): string {
  const [value, setValue] = useState<number | null>(null);
  const currentRef = useRef<number | null>(null);
  useEffect(() => {
    if (target === null) return;
    const from = currentRef.current ?? 0;
    if (from === target) {
      setValue(target);
      return;
    }
    let raf = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / ms);
      const eased = 1 - Math.pow(1 - t, 3);
      const next = Math.round(from + (target - from) * eased);
      currentRef.current = next;
      setValue(next);
      if (t < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, ms]);
  return value === null ? "–" : value.toLocaleString();
}

function DashboardPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [upgraded, setUpgraded] = useState(false);
  const [query, setQuery] = useState("");
  const [typedPlaceholder, setTypedPlaceholder] = useState("");

  const [listings, setListings] = useState<PoolListing[]>([]);
  const [total, setTotal] = useState(0);
  const [todayCount, setTodayCount] = useState<number | null>(null);
  const [semantic, setSemantic] = useState(true);
  const [loading, setLoading] = useState(true);
  // Search results are separate from the browse feed: the feed stays on
  // screen (with its counters) until results actually arrive. No mid-typing
  // mode flip, no stale-number flash.
  const [searchResults, setSearchResults] = useState<PoolListing[] | null>(null);
  const [searchLoading, setSearchLoading] = useState(false);
  const [marketplaces, setMarketplaces] = useState<string[]>([]);
  const [conditions, setConditions] = useState<string[]>([]);
  const [maxPrice, setMaxPrice] = useState("");
  const [offset, setOffset] = useState(0);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (searchParams.get("upgraded") === "1") {
      setUpgraded(true);
      router.replace("/dashboard", { scroll: false });
    }
    try {
      const cached = JSON.parse(localStorage.getItem("pool_stats") ?? "null");
      if (cached && typeof cached.total === "number") {
        setTotal(cached.total);
        setTodayCount(cached.today ?? null);
        setLoading(false);
      }
    } catch { /* cold cache is fine */ }
    const q = searchParams.get("q");
    if (q) {
      setQuery(q);
      router.replace("/dashboard", { scroll: false });
    }

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Typewriter placeholder
  useEffect(() => {
    let idx = 0;
    let charIdx = 0;
    let deleting = false;
    let timeoutId: ReturnType<typeof setTimeout>;

    function tick() {
      const word = "Try: " + PLACEHOLDERS[idx];
      if (!deleting) {
        charIdx++;
        setTypedPlaceholder(word.slice(0, charIdx));
        if (charIdx === word.length) {
          deleting = true;
          timeoutId = setTimeout(tick, 1800);
        } else {
          timeoutId = setTimeout(tick, 55);
        }
      } else {
        charIdx--;
        setTypedPlaceholder(word.slice(0, charIdx));
        if (charIdx === 0) {
          deleting = false;
          idx = (idx + 1) % PLACEHOLDERS.length;
          timeoutId = setTimeout(tick, 400);
        } else {
          timeoutId = setTimeout(tick, 30);
        }
      }
    }

    timeoutId = setTimeout(tick, 600);
    return () => clearTimeout(timeoutId);
  }, []);

  const buildParams = useCallback(
    (extra: Record<string, string>) => {
      const params = new URLSearchParams(extra);
      if (marketplaces.length) params.set("marketplace", marketplaces.join(","));
      if (conditions.length) params.set("condition", conditions.join(","));
      const price = parseFloat(maxPrice);
      if (!isNaN(price) && price > 0) params.set("max_price", String(price));
      return params.toString();
    },
    [marketplaces, conditions, maxPrice],
  );

  const loadFeed = useCallback(
    (nextOffset: number, append: boolean) => {
      setLoading(true);
      fetch(`/api/listings/feed?${buildParams({
        limit: String(PAGE_SIZE),
        offset: String(nextOffset),
      })}`)
        .then(r => r.json())
        .then(data => {
          const rows: PoolListing[] = Array.isArray(data?.listings) ? data.listings : [];
          setListings(prev => (append ? [...prev, ...rows] : rows));
          setTotal(data?.total_in_window ?? rows.length);
          if (typeof data?.added_today === "number") setTodayCount(data.added_today);
          try {
            localStorage.setItem("pool_stats", JSON.stringify({
              total: data?.total_in_window ?? rows.length,
              today: data?.added_today ?? null,
            }));
          } catch { /* private mode etc. */ }
          setOffset(nextOffset + rows.length);
        })
        .catch(() => {})
        .finally(() => setLoading(false));
    },
    [buildParams],
  );

  const [searchOffset, setSearchOffset] = useState(0);
  const [searchExhausted, setSearchExhausted] = useState(false);

  const runSearch = useCallback(
    (q: string, nextOffset = 0, append = false) => {
      setSearchLoading(true);
      fetch(`/api/listings/search?${buildParams({
        q,
        limit: String(PAGE_SIZE),
        offset: String(nextOffset),
      })}`)
        .then(r => r.json())
        .then(data => {
          const rows: PoolListing[] = Array.isArray(data?.listings) ? data.listings : [];
          setSearchResults(prev => (append && prev ? [...prev, ...rows] : rows));
          setSemantic(data?.semantic !== false);
          setSearchOffset(nextOffset + rows.length);
          setSearchExhausted(rows.length < PAGE_SIZE || nextOffset + rows.length >= SEARCH_MAX);
        })
        .catch(() => {})
        .finally(() => setSearchLoading(false));
    },
    [buildParams],
  );

  // Browse feed loads on mount + filter changes.
  useEffect(() => {
    loadFeed(0, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [marketplaces, conditions, maxPrice]);

  // Search runs debounced; clearing the query returns to browse instantly.
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const q = query.trim();
    if (!q) {
      setSearchResults(null);
      setSearchLoading(false);
      return;
    }
    debounceRef.current = setTimeout(() => runSearch(q), 700);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, marketplaces, conditions, maxPrice]);

  const searching = query.trim().length > 0;

  const liveCount = useCountUp(loading && total === 0 ? null : total);
  const todayDisplay = useCountUp(todayCount);

  const inSearchMode = searchResults !== null;
  const displayed = searchResults ?? listings;
  const canLoadMore = inSearchMode
    ? !searchExhausted
    : !searching && listings.length < total;
  const isEmpty = inSearchMode
    ? searchResults.length === 0 && !searchLoading
    : !loading && listings.length === 0;

  return (
    <main className={`${styles.main} pageEnter`}>
      {upgraded && (
        <div className={styles.upgradedBanner}>
          <span>You&apos;re now a Pro member. Enjoy unlimited agents and email digests.</span>
          <button onClick={() => setUpgraded(false)} className={styles.upgradedDismiss}>✕</button>
        </div>
      )}

      <div className={styles.heroUnified}>
        <h1 className={styles.heading}>Daily Drops</h1>
        <p className={styles.subheading}>
          {inSearchMode
            ? `Top ${searchResults.length} by fit${semantic ? "" : " · keyword match"}`
            : (
              <>
                <span className={styles.statNum}>{liveCount}</span> live listings in the pool ·{" "}
                <span className={styles.statNum}>{todayDisplay}</span> added today
              </>
            )}
        </p>
        <div className={styles.searchWrap}>
          <svg className={styles.searchIcon} width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
          <input
            className={styles.searchInput}
            type="text"
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder={typedPlaceholder}
            autoFocus
          />
          {query && (
            <button className={styles.clearSearch} onClick={() => setQuery("")}>✕</button>
          )}
        </div>
      </div>

      <div className={styles.filterBar}>
        <MultiSelect
          values={marketplaces}
          onChange={setMarketplaces}
          allLabel="All stores"
          options={MARKETPLACES.map(m => ({ value: m, label: MARKETPLACE_LABELS[m] }))}
        />
        <MultiSelect
          values={conditions}
          onChange={setConditions}
          allLabel="Any condition"
          options={CONDITIONS.map(([value, label]) => ({ value, label }))}
        />
        <div className={styles.priceWrap}>
          <span className={styles.pricePrefix}>$</span>
          <input
            className={styles.priceInput}
            type="number"
            min="0"
            placeholder="Max"
            value={maxPrice}
            onChange={e => setMaxPrice(e.target.value)}
          />
          <div className={styles.stepper}>
            <button
              type="button"
              className={styles.stepBtn}
              aria-label="Increase max price"
              onClick={() => setMaxPrice(prev => String((parseFloat(prev) || 0) + 25))}
            >
              <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round"><path d="m18 15-6-6-6 6"/></svg>
            </button>
            <button
              type="button"
              className={styles.stepBtn}
              aria-label="Decrease max price"
              onClick={() =>
                setMaxPrice(prev => {
                  const next = (parseFloat(prev) || 0) - 25;
                  return next > 0 ? String(next) : "";
                })
              }
            >
              <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round"><path d="m6 9 6 6 6-6"/></svg>
            </button>
          </div>
        </div>
        <span className={styles.filterCount}>
          {searchLoading
            ? "searching…"
            : inSearchMode
              ? `top ${searchResults.length} by fit`
              : `${total} listing${total === 1 ? "" : "s"}`}
        </span>
      </div>

      {loading && listings.length === 0 && !inSearchMode && (
        <p className={styles.searching}>Loading the pool…</p>
      )}

      {isEmpty ? (
        <div className={styles.poolEmpty}>
          <p className={styles.poolEmptyTitle}>
            {searching ? "We haven't spotted that one yet." : "The pool is quiet here."}
          </p>
          {searching ? (
            <Link href="/watchlists" className={styles.emptyCtaLink}>
              Deploy an AI agent to find it for you →
            </Link>
          ) : (
            <p className={styles.poolEmptySub}>
              Loosen a filter, or let the fleet&apos;s next sweep restock this view.
            </p>
          )}
        </div>
      ) : (
        <>
          <div className={styles.poolGrid} style={searchLoading ? { opacity: 0.55 } : undefined}>
            {displayed.map((listing, i) => (
              <PoolCard key={`${listing.id}-${i}`} listing={listing} index={i % PAGE_SIZE} />
            ))}
          </div>
          {canLoadMore && (
            <button
              className={styles.loadMore}
              onClick={() =>
                inSearchMode
                  ? runSearch(query.trim(), searchOffset, true)
                  : loadFeed(offset, true)
              }
              disabled={loading || searchLoading}
            >
              {loading || searchLoading ? "Loading…" : "Load more"}
            </button>
          )}
        </>
      )}
    </main>
  );
}

export default function DashboardPage() {
  return (
    <Suspense>
      <DashboardPageInner />
    </Suspense>
  );
}
