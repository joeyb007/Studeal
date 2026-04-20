"use client";

import { useEffect, useState } from "react";
import { useSession } from "next-auth/react";
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

const CONDITION_LABELS: Record<string, string> = {
  new: "New",
  used: "Used",
  refurb: "Refurb",
};

const CONDITION_CLASS: Record<string, string> = {
  new: styles.condNew,
  used: styles.condUsed,
  refurb: styles.condRefurb,
};

const TIER_LABELS: Record<string, string> = {
  push: "Hot",
  digest: "Good",
  none: "Mild",
};

const TIER_CLASS: Record<string, string> = {
  push: styles.tierPush,
  digest: styles.tierDigest,
  none: styles.tierNone,
};

const CATEGORIES = ["Electronics", "Laptops", "Tablets", "Phones", "Audio", "Gaming", "Accessories", "Software", "Books", "Clothing", "Food & Drink", "Travel", "Home", "Other"];
const CONDITIONS = ["new", "used", "refurb"];
const TIERS = ["push", "digest", "none"];

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
          {deal.student_eligible && (
            <span className={styles.studentBadge}>Student</span>
          )}
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
  const [deals, setDeals] = useState<Deal[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedCategories, setSelectedCategories] = useState<string[]>([]);
  const [selectedConditions, setSelectedConditions] = useState<string[]>([]);
  const [selectedTiers, setSelectedTiers] = useState<string[]>([]);
  const [studentOnly, setStudentOnly] = useState(false);
  const [sort, setSort] = useState<"score" | "discount" | "price">("score");

  useEffect(() => {
    let active = true;
    fetch("/api/deals")
      .then(r => r.json())
      .then(data => { if (active) { setDeals(Array.isArray(data) ? data : []); setLoading(false); } })
      .catch(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [token]);

  function toggleSet<T>(set: T[], val: T): T[] {
    return set.includes(val) ? set.filter(v => v !== val) : [...set, val];
  }

  const filtered = deals
    .filter(d => selectedCategories.length === 0 || selectedCategories.includes(d.category))
    .filter(d => selectedConditions.length === 0 || selectedConditions.includes(d.condition))
    .filter(d => selectedTiers.length === 0 || selectedTiers.includes(d.alert_tier))
    .filter(d => !studentOnly || d.student_eligible)
    .sort((a, b) => {
      if (sort === "score") return b.score - a.score;
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
      <div className={styles.layout}>
        {/* Sidebar */}
        <aside className={styles.sidebar}>
          <div className={styles.sidebarSection}>
            <h3 className={styles.sidebarTitle}>Category</h3>
            {CATEGORIES.map(cat => (
              <label key={cat} className={styles.checkLabel}>
                <input
                  type="checkbox"
                  className={styles.checkbox}
                  checked={selectedCategories.includes(cat)}
                  onChange={() => setSelectedCategories(prev => toggleSet(prev, cat))}
                />
                {cat}
              </label>
            ))}
          </div>

          <div className={styles.sidebarSection}>
            <h3 className={styles.sidebarTitle}>Condition</h3>
            {CONDITIONS.map(c => (
              <label key={c} className={styles.checkLabel}>
                <input
                  type="checkbox"
                  className={styles.checkbox}
                  checked={selectedConditions.includes(c)}
                  onChange={() => setSelectedConditions(prev => toggleSet(prev, c))}
                />
                {CONDITION_LABELS[c]}
              </label>
            ))}
          </div>

          <div className={styles.sidebarSection}>
            <h3 className={styles.sidebarTitle}>Deal Tier</h3>
            {TIERS.map(t => (
              <label key={t} className={styles.checkLabel}>
                <input
                  type="checkbox"
                  className={styles.checkbox}
                  checked={selectedTiers.includes(t)}
                  onChange={() => setSelectedTiers(prev => toggleSet(prev, t))}
                />
                {TIER_LABELS[t]}
              </label>
            ))}
          </div>

          <div className={styles.sidebarSection}>
            <label className={styles.toggleLabel}>
              <span>Student deals only</span>
              <div
                className={[styles.toggle, studentOnly ? styles.toggleOn : ""].join(" ")}
                onClick={() => setStudentOnly(v => !v)}
              >
                <div className={styles.toggleThumb} />
              </div>
            </label>
          </div>

          {(selectedCategories.length > 0 || selectedConditions.length > 0 || selectedTiers.length > 0 || studentOnly) && (
            <button
              className={styles.clearBtn}
              onClick={() => { setSelectedCategories([]); setSelectedConditions([]); setSelectedTiers([]); setStudentOnly(false); }}
            >
              Clear filters
            </button>
          )}
        </aside>

        {/* Main */}
        <main className={styles.main}>
          <div className={styles.topBar}>
            <div>
              <h1 className={styles.heading}>Daily Drops</h1>
              <span className={styles.count}>{filtered.length} deals</span>
            </div>
            <select
              className={styles.sortSelect}
              value={sort}
              onChange={e => setSort(e.target.value as typeof sort)}
            >
              <option value="score">Best Score</option>
              <option value="discount">Biggest Discount</option>
              <option value="price">Lowest Price</option>
            </select>
          </div>

          {loading ? (
            <div className={styles.empty}>Loading deals...</div>
          ) : filtered.length === 0 ? (
            <div className={styles.empty}>No deals match your filters.</div>
          ) : (
            <div className={styles.grid}>
              {filtered.map(deal => <DealCard key={deal.id} deal={deal} />)}
            </div>
          )}
        </main>
      </div>
    </>
  );
}
