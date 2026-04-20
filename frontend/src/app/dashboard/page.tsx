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
  real_discount_pct: number | null;
  student_eligible: boolean;
  scraped_at: string;
}

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

function pct(listed: number, sale: number) {
  return Math.round(((listed - sale) / listed) * 100);
}

function DealCard({ deal }: { deal: Deal }) {
  const discount = deal.real_discount_pct ?? pct(deal.listed_price, deal.sale_price);
  return (
    <a href={deal.url} target="_blank" rel="noopener noreferrer" className={styles.card}>
      <div className={styles.cardTop}>
        <div className={styles.cardMeta}>
          <span className={styles.source}>{deal.source}</span>
          <span className={styles.category}>{deal.category}</span>
        </div>
        <div className={styles.badges}>
          {deal.student_eligible && (
            <span className={styles.studentBadge}>Student</span>
          )}
          <span className={[styles.tierBadge, TIER_CLASS[deal.alert_tier] ?? ""].join(" ")}>
            {TIER_LABELS[deal.alert_tier] ?? deal.alert_tier}
          </span>
        </div>
      </div>

      <p className={styles.title}>{deal.title}</p>

      <div className={styles.cardBottom}>
        <div className={styles.prices}>
          <span className={styles.salePrice}>${deal.sale_price.toFixed(2)}</span>
          <span className={styles.listedPrice}>${deal.listed_price.toFixed(2)}</span>
          <span className={styles.discount}>−{discount}%</span>
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
  const [tier, setTier] = useState<string>("all");

  useEffect(() => {
    setLoading(true);
    const params = tier !== "all" ? `?tier=${tier}` : "";
    fetch(`/api/deals${params}`)
      .then(r => r.json())
      .then(data => {
        setDeals(Array.isArray(data) ? data : []);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [token, tier]);

  return (
    <>
      <Nav />
      <main className={styles.main}>
        <div className={styles.header}>
          <h1 className={styles.heading}>Today&apos;s Deals</h1>
          <div className={styles.filters}>
            {["all", "push", "digest", "none"].map(t => (
              <button
                key={t}
                className={[styles.filterBtn, tier === t ? styles.filterActive : ""].join(" ")}
                onClick={() => setTier(t)}
              >
                {t === "all" ? "All" : TIER_LABELS[t]}
              </button>
            ))}
          </div>
        </div>

        {loading ? (
          <div className={styles.empty}>Loading deals...</div>
        ) : deals.length === 0 ? (
          <div className={styles.empty}>No deals found. The pipeline may still be running.</div>
        ) : (
          <div className={styles.grid}>
            {deals.map(deal => <DealCard key={deal.id} deal={deal} />)}
          </div>
        )}
      </main>
    </>
  );
}
