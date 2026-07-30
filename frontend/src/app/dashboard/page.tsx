"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useSession } from "next-auth/react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import Nav from "@/components/Nav";
import styles from "./page.module.css";

interface PoolListing {
  id: number;
  title: string;
  price: number;
  currency: string;
  marketplace: string;
  url: string;
  image_url: string | null;
  location: string | null;
  condition: string;
  first_seen_at: string;
  last_seen_at: string;
  relevance: number | null;
}

const MARKETPLACES = [
  "kijiji", "fb_marketplace", "ebay", "craigslist", "bestbuy_outlet",
  "canada_computers", "visions_openbox", "newegg_ca", "openbox_ca", "refurbio",
];
const MARKETPLACE_LABELS: Record<string, string> = {
  kijiji: "Kijiji",
  fb_marketplace: "Facebook",
  ebay: "eBay",
  craigslist: "Craigslist",
  bestbuy_outlet: "Best Buy",
  canada_computers: "Canada Computers",
  visions_openbox: "Visions",
  newegg_ca: "Newegg",
  openbox_ca: "OpenBox.ca",
  refurbio: "REFURB.io",
};
const CONDITIONS = ["new", "used", "refurbished"];

function timeAgo(iso: string): string {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 3600) return `${Math.max(1, Math.floor(seconds / 60))}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

const CONDITION_LABELS: Record<string, string> = { new: "New", used: "Used", refurbished: "Refurb" };
const CONDITION_CLASS: Record<string, string> = {
  new: styles.condNew,
  used: styles.condUsed,
  refurbished: styles.condRefurb,
};

// Full sentences, not keywords — the bar takes natural language and embeds it.
const PLACEHOLDERS = [
  "a beginner-friendly road bike, nothing over $400",
  "quiet mechanical keyboard for late-night work",
  "lightly used monitor for a dorm desk",
  "noise cancelling headphones that aren't beat up",
  "a cheap laptop that can handle coursework",
];

// Chips are full requests, not keywords — clicking one demonstrates that the
// bar understands intent rather than matching words.
const SUGGESTIONS = [
  "a laptop that can handle coursework, under $600",
  "comfortable desk chair for long study sessions",
  "cheap second monitor for a dorm desk",
  "headphones for a noisy library",
  "a used bike for campus commuting",
  "mechanical keyboard that isn't loud",
];

function CarouselStrip({ feed }: { feed: PoolListing[] }) {
  if (feed.length === 0) return null;
  const doubled = [...feed, ...feed];
  return (
    <div className={styles.carousel}>
      <div className={styles.carouselLabel}>Fresh finds</div>
      <div className={styles.carouselViewport}>
        <div className={styles.carouselTrack}>
          {doubled.map((listing, i) => (
            <a key={`${listing.id}-${i}`} href={listing.url} target="_blank" rel="noopener noreferrer" className={styles.miniCard}>
              <span className={styles.miniSource}>
                {MARKETPLACE_LABELS[listing.marketplace] ?? listing.marketplace}
              </span>
              <span className={styles.miniTitle}>{listing.title}</span>
              <span className={styles.miniPrice}>${listing.price.toFixed(2)}</span>
            </a>
          ))}
        </div>
      </div>
    </div>
  );
}

function ListingCard({ listing, index }: { listing: PoolListing; index: number }) {
  return (
    <div className={styles.card} style={{ animationDelay: `${index * 60}ms` }}>
      <div className={styles.cardBody}>
        <div className={styles.cardTop}>
          <span className={styles.source}>
            {MARKETPLACE_LABELS[listing.marketplace] ?? listing.marketplace}
          </span>
          <span className={styles.seenAt}>{timeAgo(listing.last_seen_at)}</span>
        </div>
        <p className={styles.title}>{listing.title}</p>
        <div className={styles.prices}>
          <span className={styles.salePrice}>
            ${listing.price.toFixed(2)}
          </span>
          <span className={styles.currency}>{listing.currency}</span>
          {listing.location && <span className={styles.location}>{listing.location}</span>}
        </div>
      </div>
      <div className={styles.cardFooter}>
        <div className={styles.badges}>
          {listing.condition && listing.condition !== "unknown" && (
            <span className={[styles.condBadge, CONDITION_CLASS[listing.condition] ?? ""].join(" ")}>
              {CONDITION_LABELS[listing.condition] ?? listing.condition}
            </span>
          )}
        </div>
        <a href={listing.url} target="_blank" rel="noopener noreferrer" className={styles.buyBtn}>
          View →
        </a>
      </div>
    </div>
  );
}

function DashboardPageInner() {
  const { data: session } = useSession();
  const token = session?.accessToken;
  const router = useRouter();
  const searchParams = useSearchParams();
  const [upgraded, setUpgraded] = useState(false);

  useEffect(() => {
    if (searchParams.get("upgraded") === "1") {
      setUpgraded(true);
      router.replace("/dashboard", { scroll: false });
    }
    const q = searchParams.get("q");
    if (q) {
      setQuery(q);
      router.replace("/dashboard", { scroll: false });
    }
  }, []);
  const [query, setQuery] = useState("");
  const [listings, setListings] = useState<PoolListing[]>([]);
  const [searching, setSearching] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  const [typedPlaceholder, setTypedPlaceholder] = useState("");
  const [feed, setFeed] = useState<PoolListing[]>([]);
  const [semantic, setSemantic] = useState(true);
  // phase: 'idle' | 'fading' | 'shifted'
  const [phase, setPhase] = useState<"idle" | "fading" | "shifted">("idle");
  const [selectedMarketplaces, setSelectedMarketplaces] = useState<string[]>([]);
  const [selectedConditions, setSelectedConditions] = useState<string[]>([]);
  const phaseRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  function toggleSet<T>(set: T[], val: T): T[] {
    return set.includes(val) ? set.filter(v => v !== val) : [...set, val];
  }

  useEffect(() => {
    fetch("/api/listings/feed?limit=40")
      .then(r => r.json())
      .then(data => { if (Array.isArray(data?.listings)) setFeed(data.listings); })
      .catch(() => {});
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

  // Phase sequencing: idle → fading → shifted
  useEffect(() => {
    if (phaseRef.current) clearTimeout(phaseRef.current);
    if (query.length > 0) {
      setPhase("fading");
      phaseRef.current = setTimeout(() => setPhase("shifted"), 400);
    } else {
      setPhase("idle");
    }
    return () => { if (phaseRef.current) clearTimeout(phaseRef.current); };
  }, [query.length > 0]);  // only re-run when empty↔non-empty changes

  // Debounced search
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      if (!query.trim()) {
        setListings([]);
        setHasSearched(false);
        return;
      }
      const searchStart = Date.now();
      setSearching(true);
      fetch(`/api/listings/search?q=${encodeURIComponent(query.trim())}&limit=40`)
        .then(r => r.json())
        .then(data => {
          const elapsed = Date.now() - searchStart;
          const minVisible = 1400;
          const delay = Math.max(0, minVisible - elapsed);
          setTimeout(() => {
            setListings(Array.isArray(data?.listings) ? data.listings : []);
            setSemantic(data?.semantic !== false);
            setHasSearched(true);
            setSearching(false);
          }, delay);
        })
        .catch(() => setSearching(false));
    }, 900);
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current); };
  }, [query, token]);

  const isFading = phase === "fading";
  const isShifted = phase === "shifted";

  const filtered = listings
    .filter(l => selectedMarketplaces.length === 0 || selectedMarketplaces.includes(l.marketplace))
    .filter(l => selectedConditions.length === 0 || selectedConditions.includes(l.condition));

  const hasResults = hasSearched && listings.length > 0;
  const isEmpty = hasSearched && listings.length === 0 && !searching;
  const hasFilters = selectedMarketplaces.length > 0 || selectedConditions.length > 0;

  return (
    <>
      <Nav />
      <main className={`${styles.main} pageEnter`}>

        {upgraded && (
          <div className={styles.upgradedBanner}>
            <span>You&apos;re now a Pro member — enjoy unlimited agents and email digests.</span>
            <button onClick={() => setUpgraded(false)} className={styles.upgradedDismiss}>✕</button>
          </div>
        )}

        {/* Search hero */}
        <div className={[styles.hero, isShifted ? styles.heroShifted : ""].join(" ")}>

          {/* Sidebar — slides in from left when shifted */}
          <aside className={[styles.sidebar, isShifted ? styles.sidebarOpen : ""].join(" ")}>
            <div className={styles.sidebarSection}>
              <h3 className={styles.sidebarTitle}>Marketplace</h3>
              {MARKETPLACES.map(m => (
                <label key={m} className={styles.checkLabel}>
                  <input type="checkbox" className={styles.checkbox} checked={selectedMarketplaces.includes(m)} onChange={() => setSelectedMarketplaces(prev => toggleSet(prev, m))} />
                  {MARKETPLACE_LABELS[m] ?? m}
                </label>
              ))}
            </div>
            <div className={styles.sidebarSection}>
              <h3 className={styles.sidebarTitle}>Condition</h3>
              {CONDITIONS.map(c => (
                <label key={c} className={styles.checkLabel}>
                  <input type="checkbox" className={styles.checkbox} checked={selectedConditions.includes(c)} onChange={() => setSelectedConditions(prev => toggleSet(prev, c))} />
                  {CONDITION_LABELS[c]}
                </label>
              ))}
            </div>
            {hasFilters && (
              <button className={styles.clearBtn} onClick={() => { setSelectedMarketplaces([]); setSelectedConditions([]); }}>
                Clear filters
              </button>
            )}
          </aside>

          <div className={styles.heroInner}>
            <div className={[styles.idleContent, (isFading || isShifted) ? styles.idleContentHidden : ""].join(" ")}>
              <h1 className={styles.heading}>Daily Drops</h1>
              <p className={styles.subheading}>Our agents have been scanning the web. Search what they found.</p>
            </div>
            <div className={styles.searchWrap}>
              <svg className={styles.searchIcon} width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
              <input
                ref={inputRef}
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
            <div className={[styles.idleContent, (isFading || isShifted) ? styles.idleContentHidden : ""].join(" ")}>
              <p className={styles.searchHint}>
                Describe what you&apos;re after — plain English works better than keywords.
              </p>
              <div className={styles.suggestions}>
                {SUGGESTIONS.map(s => (
                  <button key={s} className={styles.suggestionPill} onClick={() => setQuery(s)}>
                    {s}
                  </button>
                ))}
              </div>
              <CarouselStrip feed={feed} />
            </div>
            {searching && <p className={styles.searching}>Searching the pool…</p>}

            {/* Fixed results panel */}
            <div className={[styles.resultsPanel, isShifted ? styles.resultsPanelOpen : ""].join(" ")}>
              {isEmpty && (
                <div className={styles.empty}>
                  <p>We haven&apos;t spotted that one yet.</p>
                  <Link href="/watchlists" className={styles.emptyCtaLink}>
                    Deploy an AI agent to find it for you →
                  </Link>
                </div>
              )}
              {hasResults && (
                <>
                  <div className={styles.resultsHeader}>
                    <span className={styles.resultsCount}>
                      {filtered.length} of {listings.length} listings
                      {!semantic && " · keyword match"}
                    </span>
                    <Link href="/catalog" className={styles.catalogInline}>Browse all →</Link>
                  </div>
                  <div className={styles.resultsScroll}>
                    <div className={styles.grid}>
                      {filtered.map((listing, i) => <ListingCard key={listing.id} listing={listing} index={i} />)}
                    </div>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      </main>
    </>
  );
}

export default function DashboardPage() {
  return (
    <Suspense>
      <DashboardPageInner />
    </Suspense>
  );
}
