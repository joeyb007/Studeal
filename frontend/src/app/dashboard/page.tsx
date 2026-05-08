"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useSession } from "next-auth/react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import Nav from "@/components/Nav";
import styles from "./page.module.css";

interface Deal {
  id: number;
  title: string;
  source: string;
  url: string | null;
  affiliate_url: string | null;
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

const CATEGORIES = ["Electronics", "Laptops", "Tablets", "Phones", "Audio", "Gaming", "Accessories", "Software", "Books", "Clothing", "Food & Drink", "Travel", "Home", "Other"];
const CONDITIONS = ["new", "used", "refurb"];
const TIERS = ["push", "digest", "none"];

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

const SUGGESTIONS = [
  "Laptops under $500",
  "AirPods deals",
  "Student textbooks",
  "Gaming headsets",
  "Monitors on sale",
  "Mechanical keyboards",
  "iPad + tablet",
  "Refurbished MacBooks",
];

function CarouselStrip({ feed }: { feed: Deal[] }) {
  if (feed.length === 0) return null;
  const doubled = [...feed, ...feed];
  return (
    <div className={styles.carousel}>
      <div className={styles.carouselLabel}>Live deals</div>
      <div className={styles.carouselViewport}>
        <div className={styles.carouselTrack}>
          {doubled.filter(deal => deal.url).map((deal, i) => {
            const discount = deal.real_discount_pct ?? pct(deal.listed_price, deal.sale_price);
            return (
              <a key={`${deal.id}-${i}`} href={deal.url!} target="_blank" rel="noopener noreferrer" className={styles.miniCard}>
                <span className={styles.miniDiscount}>−{discount}%</span>
                <span className={styles.miniTitle}>{deal.title}</span>
                <span className={styles.miniPrice}>${deal.sale_price.toFixed(2)}</span>
              </a>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function pct(listed: number, sale: number) {
  return Math.round(((listed - sale) / listed) * 100);
}

function DealCard({ deal, index }: { deal: Deal; index: number }) {
  const discount = deal.real_discount_pct ?? pct(deal.listed_price, deal.sale_price);
  return (
    <div className={styles.card} style={{ animationDelay: `${index * 60}ms` }}>
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
        {(deal.affiliate_url || deal.url) && (
          <a href={deal.affiliate_url ?? deal.url!} target="_blank" rel="noopener noreferrer" className={styles.buyBtn}>
            Buy here →
          </a>
        )}
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
  const [deals, setDeals] = useState<Deal[]>([]);
  const [searching, setSearching] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  const [typedPlaceholder, setTypedPlaceholder] = useState("");
  const [feed, setFeed] = useState<Deal[]>([]);
  // phase: 'idle' | 'fading' | 'shifted'
  const [phase, setPhase] = useState<"idle" | "fading" | "shifted">("idle");
  const [selectedCategories, setSelectedCategories] = useState<string[]>([]);
  const [selectedConditions, setSelectedConditions] = useState<string[]>([]);
  const [selectedTiers, setSelectedTiers] = useState<string[]>([]);
  const [studentOnly, setStudentOnly] = useState(false);
  const phaseRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  function toggleSet<T>(set: T[], val: T): T[] {
    return set.includes(val) ? set.filter(v => v !== val) : [...set, val];
  }

  useEffect(() => {
    fetch("/api/deals")
      .then(r => r.json())
      .then(data => { if (Array.isArray(data)) setFeed(data.slice(0, 20)); })
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
        setDeals([]);
        setHasSearched(false);
        return;
      }
      const searchStart = Date.now();
      setSearching(true);
      fetch(`/api/deals/search?q=${encodeURIComponent(query.trim())}`)
        .then(r => r.json())
        .then(data => {
          const elapsed = Date.now() - searchStart;
          const minVisible = 1400;
          const delay = Math.max(0, minVisible - elapsed);
          setTimeout(() => {
            setDeals(Array.isArray(data) ? data : []);
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

  const filtered = deals
    .filter(d => selectedCategories.length === 0 || selectedCategories.includes(d.category))
    .filter(d => selectedConditions.length === 0 || selectedConditions.includes(d.condition))
    .filter(d => selectedTiers.length === 0 || selectedTiers.includes(d.alert_tier))
    .filter(d => !studentOnly || d.student_eligible);

  const hasResults = hasSearched && deals.length > 0;
  const isEmpty = hasSearched && deals.length === 0 && !searching;
  const hasFilters = selectedCategories.length > 0 || selectedConditions.length > 0 || selectedTiers.length > 0 || studentOnly;

  return (
    <>
      <Nav />
      <main className={`${styles.main} pageEnter`}>

        {upgraded && (
          <div className={styles.upgradedBanner}>
            <span>You&apos;re now a Pro member — enjoy unlimited watchlists and email digests.</span>
            <button onClick={() => setUpgraded(false)} className={styles.upgradedDismiss}>✕</button>
          </div>
        )}

        {/* Search hero */}
        <div className={[styles.hero, isShifted ? styles.heroShifted : ""].join(" ")}>

          {/* Sidebar — slides in from left when shifted */}
          <aside className={[styles.sidebar, isShifted ? styles.sidebarOpen : ""].join(" ")}>
            <div className={styles.sidebarSection}>
              <h3 className={styles.sidebarTitle}>Category</h3>
              {CATEGORIES.map(cat => (
                <label key={cat} className={styles.checkLabel}>
                  <input type="checkbox" className={styles.checkbox} checked={selectedCategories.includes(cat)} onChange={() => setSelectedCategories(prev => toggleSet(prev, cat))} />
                  {cat}
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
            <div className={styles.sidebarSection}>
              <h3 className={styles.sidebarTitle}>Deal Tier</h3>
              {TIERS.map(t => (
                <label key={t} className={styles.checkLabel}>
                  <input type="checkbox" className={styles.checkbox} checked={selectedTiers.includes(t)} onChange={() => setSelectedTiers(prev => toggleSet(prev, t))} />
                  {TIER_LABELS[t]}
                </label>
              ))}
            </div>
            <div className={styles.sidebarSection}>
              <label className={styles.toggleLabel}>
                <span>Student only</span>
                <div className={[styles.toggle, studentOnly ? styles.toggleOn : ""].join(" ")} onClick={() => setStudentOnly(v => !v)}>
                  <div className={styles.toggleThumb} />
                </div>
              </label>
            </div>
            {hasFilters && (
              <button className={styles.clearBtn} onClick={() => { setSelectedCategories([]); setSelectedConditions([]); setSelectedTiers([]); setStudentOnly(false); }}>
                Clear filters
              </button>
            )}
          </aside>

          <div className={styles.heroInner}>
            <div className={[styles.idleContent, (isFading || isShifted) ? styles.idleContentHidden : ""].join(" ")}>
              <h1 className={styles.heading}>Daily Drops</h1>
              <p className={styles.subheading}>Every deal the internet has right now — just tell us what you need.</p>
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
              <div className={styles.suggestions}>
                {SUGGESTIONS.map(s => (
                  <button key={s} className={styles.suggestionPill} onClick={() => setQuery(s)}>
                    {s}
                  </button>
                ))}
              </div>
              <CarouselStrip feed={feed} />
              <div className={styles.catalogLink}>
                <Link href="/catalog">Browse all deals →</Link>
              </div>
            </div>
            {searching && <p className={styles.searching}>Finding deals…</p>}

            {/* Fixed results panel */}
            <div className={[styles.resultsPanel, isShifted ? styles.resultsPanelOpen : ""].join(" ")}>
              {isEmpty && (
                <div className={styles.empty}>Nothing matched — try describing what you need differently.</div>
              )}
              {hasResults && (
                <>
                  <div className={styles.resultsHeader}>
                    <span className={styles.resultsCount}>{filtered.length} of {deals.length} deals</span>
                    <Link href="/catalog" className={styles.catalogInline}>Browse all →</Link>
                  </div>
                  <div className={styles.resultsScroll}>
                    <div className={styles.grid}>
                      {filtered.map((deal, i) => <DealCard key={deal.id} deal={deal} index={i} />)}
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
