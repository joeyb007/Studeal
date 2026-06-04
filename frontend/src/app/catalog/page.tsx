"use client";

import { useEffect, useState } from "react";
import { useSession } from "next-auth/react";
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
  deal_score: number | null;
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
const CATEGORIES = ["Electronics", "Laptops", "Tablets", "Phones", "Audio", "Gaming", "Accessories", "Software", "Books", "Clothing", "Food & Drink", "Travel", "Home", "Other"];
const CONDITIONS = ["new", "used", "refurb"];

function scoreColor(score: number | null): string {
  if (score == null) return "transparent";
  const s = Math.round((score / 100) * 75);
  const l = Math.round(32 + (score / 100) * 16);
  return `hsl(142, ${s}%, ${l}%)`;
}

function pct(listed: number, sale: number) {
  return Math.round(((listed - sale) / listed) * 100);
}

function DealCard({ deal, index = 0 }: { deal: Deal; index?: number }) {
  const discount = deal.real_discount_pct ?? pct(deal.listed_price, deal.sale_price);
  const buyUrl = deal.affiliate_url ?? deal.url;
  return (
    <div className={styles.card} style={{ animationDelay: `${index * 50}ms`, borderLeft: `3px solid ${scoreColor(deal.deal_score)}` }}>
      <div className={styles.cardBody}>
        <div className={styles.cardTop}>
          <span className={styles.source}>{deal.source}</span>
          {deal.student_eligible && <span className={styles.studentBadge}>Student</span>}
        </div>
        <p className={styles.title}>{deal.title}</p>
        <div className={styles.prices}>
          <span className={styles.salePrice}>${deal.sale_price.toFixed(2)}</span>
          <span className={styles.listedPrice}>${deal.listed_price.toFixed(2)}</span>
          <span className={styles.discountInline}>−{discount}%</span>
        </div>
      </div>
      <div className={styles.cardFooter}>
        <div className={styles.badges}>
          {deal.condition && deal.condition !== "unknown" && deal.condition !== "new" && (
            <span className={[styles.condBadge, CONDITION_CLASS[deal.condition] ?? ""].join(" ")}>
              {CONDITION_LABELS[deal.condition] ?? deal.condition}
            </span>
          )}
        </div>
        {buyUrl && (
          <a href={buyUrl} target="_blank" rel="noopener noreferrer" className={styles.buyBtn}>
            Buy →
          </a>
        )}
      </div>
    </div>
  );
}

export default function CatalogPage() {
  const { data: session } = useSession();
  const token = session?.accessToken;
  const [deals, setDeals] = useState<Deal[]>([]);
  const [loading, setLoading] = useState(true);
  const [dealsReady, setDealsReady] = useState(false);
  const [selectedCategories, setSelectedCategories] = useState<string[]>([]);
  const [selectedConditions, setSelectedConditions] = useState<string[]>([]);
  const [studentOnly, setStudentOnly] = useState(false);
  const [error, setError] = useState(false);
  const [sort, setSort] = useState<"score" | "discount" | "price">("score");

  useEffect(() => {
    let active = true;
    fetch("/api/deals")
      .then(r => { if (!r.ok) throw new Error(); return r.json(); })
      .then(data => {
        if (active) {
          setDeals(Array.isArray(data) ? data : []);
          setLoading(false);
          setTimeout(() => { if (active) setDealsReady(true); }, 300);
        }
      })
      .catch(() => { if (active) { setLoading(false); setError(true); } });
    return () => { active = false; };
  }, [token]);

  function toggleSet<T>(set: T[], val: T): T[] {
    return set.includes(val) ? set.filter(v => v !== val) : [...set, val];
  }

  const filtered = deals
    .filter(d => selectedCategories.length === 0 || selectedCategories.includes(d.category))
    .filter(d => selectedConditions.length === 0 || selectedConditions.includes(d.condition))
    .filter(d => !studentOnly || d.student_eligible)
    .sort((a, b) => {
      if (sort === "score") return (b.deal_score ?? 0) - (a.deal_score ?? 0);
      if (sort === "discount") {
        const da = a.real_discount_pct ?? pct(a.listed_price, a.sale_price);
        const db = b.real_discount_pct ?? pct(b.listed_price, b.sale_price);
        return db - da;
      }
      return a.sale_price - b.sale_price;
    });

  return (
    <>
      <Nav />
      <div className={`${styles.layout} pageEnter`}>
        <aside className={styles.sidebar}>
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
            <label className={styles.toggleLabel}>
              <span>Student deals only</span>
              <div className={[styles.toggle, studentOnly ? styles.toggleOn : ""].join(" ")} onClick={() => setStudentOnly(v => !v)}>
                <div className={styles.toggleThumb} />
              </div>
            </label>
          </div>
          {(selectedCategories.length > 0 || selectedConditions.length > 0 || studentOnly) && (
            <button className={styles.clearBtn} onClick={() => { setSelectedCategories([]); setSelectedConditions([]); setStudentOnly(false); }}>
              Clear filters
            </button>
          )}
        </aside>

        <main className={styles.main}>
          <div className={styles.topBar}>
            <div>
              <div className={styles.breadcrumb}><Link href="/dashboard">← Daily Drops</Link></div>
              <h1 className={styles.heading}>All Deals</h1>
              <span className={styles.count}>{filtered.length} deals</span>
            </div>
            <select className={styles.sortSelect} value={sort} onChange={e => setSort(e.target.value as typeof sort)}>
              <option value="score">Best Score</option>
              <option value="discount">Biggest Discount</option>
              <option value="price">Lowest Price</option>
            </select>
          </div>

          {loading ? (
            <div className={styles.empty}>Loading deals...</div>
          ) : error ? (
            <div className={styles.empty}>Could not load deals. Please try again later.</div>
          ) : filtered.length === 0 ? (
            <div className={styles.catalogCta}>
              <p className={styles.catalogCtaText}>We haven&apos;t caught that one yet.</p>
              <Link href="/watchlists" className={styles.catalogCtaLink}>
                Deploy an AI agent to find it for you →
              </Link>
            </div>
          ) : (
            <>
              <div className={[styles.grid, dealsReady ? styles.gridReady : ""].join(" ")}>
                {filtered.map((deal, i) => <DealCard key={deal.id} deal={deal} index={i} />)}
              </div>
              <div className={styles.catalogCta}>
                <p className={styles.catalogCtaText}>Not seeing what you came for?</p>
                <Link href="/watchlists" className={styles.catalogCtaLink}>
                  Deploy an AI agent to find it for you →
                </Link>
              </div>
            </>
          )}
        </main>
      </div>
    </>
  );
}
