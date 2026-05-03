"use client";

import { useEffect, useState } from "react";
import { useSession } from "next-auth/react";
import { useRouter, useSearchParams } from "next/navigation";
import Nav from "@/components/Nav";
import styles from "./page.module.css";

interface Watchlist {
  id: number;
  name: string;
  keywords: string[];
  min_score: number;
  alert_tier_threshold: string;
  expires_at: string | null;
}

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
  real_discount_pct: number | null;
  student_eligible: boolean;
  condition: string;
}

const TIER_LABELS: Record<string, string> = { push: "Hot", digest: "Good", none: "Mild" };
const TIER_CLASS: Record<string, string> = {
  push: styles.tierPush,
  digest: styles.tierDigest,
  none: styles.tierNone,
};

function daysUntil(isoString: string): number {
  const ms = new Date(isoString).getTime() - Date.now();
  return Math.max(0, Math.ceil(ms / (1000 * 60 * 60 * 24)));
}

function pct(listed: number, sale: number) {
  return Math.round(((listed - sale) / listed) * 100);
}

function DealRow({ deal }: { deal: Deal }) {
  const discount = deal.real_discount_pct ?? pct(deal.listed_price, deal.sale_price);
  return (
    <div className={styles.dealRow}>
      <div className={styles.dealRowLeft}>
        <span className={styles.dealDiscount}>−{discount}%</span>
        <div>
          <p className={styles.dealTitle}>{deal.title}</p>
          <span className={styles.dealSource}>{deal.source} · {deal.category}</span>
        </div>
      </div>
      <div className={styles.dealRowRight}>
        <span className={styles.dealPrice}>${deal.sale_price.toFixed(2)}</span>
        <span className={[styles.dealTier, TIER_CLASS[deal.alert_tier] ?? ""].join(" ")}>
          {TIER_LABELS[deal.alert_tier] ?? deal.alert_tier}
        </span>
        {deal.url && (
          <a href={deal.url} target="_blank" rel="noopener noreferrer" className={styles.dealBuyBtn}>
            Buy →
          </a>
        )}
      </div>
    </div>
  );
}

function WatchlistCard({
  watchlist,
  onDelete,
}: {
  watchlist: Watchlist;
  onDelete: (id: number) => void;
}) {
  const [deals, setDeals] = useState<Deal[] | null>(null);
  const [loadingDeals, setLoadingDeals] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const days = watchlist.expires_at ? daysUntil(watchlist.expires_at) : null;

  async function loadDeals() {
    if (deals !== null) return;
    setLoadingDeals(true);
    try {
      const res = await fetch(`/api/watchlists/${watchlist.id}/deals`);
      const data = await res.json();
      setDeals(Array.isArray(data) ? data : []);
    } catch {
      setDeals([]);
    }
    setLoadingDeals(false);
  }

  function toggle() {
    if (!expanded) loadDeals();
    setExpanded(v => !v);
  }

  async function handleDelete() {
    if (!confirm(`Delete "${watchlist.name}"?`)) return;
    setDeleting(true);
    await fetch(`/api/watchlists/${watchlist.id}`, { method: "DELETE" });
    onDelete(watchlist.id);
  }

  return (
    <div className={styles.card}>
      <div className={styles.cardHeader}>
        <button className={styles.cardToggle} onClick={toggle}>
          <span className={styles.cardName}>{watchlist.name}</span>
          <span className={styles.toggleChevron}>{expanded ? "▲" : "▼"}</span>
        </button>
        <div className={styles.cardActions}>
          {days !== null && (
            <span className={styles.expiry}>
              {days === 0 ? "Expires today" : `${days}d left`}
            </span>
          )}
          <button className={styles.deleteBtn} onClick={handleDelete} disabled={deleting}>
            {deleting ? "…" : "✕"}
          </button>
        </div>
      </div>

      <div className={styles.keywords}>
        {watchlist.keywords.map(kw => (
          <span key={kw} className={styles.keyword}>{kw}</span>
        ))}
      </div>

      {expanded && (
        <div className={styles.dealsSection}>
          {loadingDeals && <p className={styles.dealsLoading}>Finding matches…</p>}
          {!loadingDeals && deals !== null && deals.length === 0 && (
            <p className={styles.dealsEmpty}>No deals matched yet — check back after the next hunt runs.</p>
          )}
          {!loadingDeals && deals && deals.length > 0 && (
            <div className={styles.dealsList}>
              {deals.map(d => <DealRow key={d.id} deal={d} />)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function WatchlistsPage() {
  const { data: session } = useSession();
  const token = session?.accessToken;
  const router = useRouter();
  const searchParams = useSearchParams();
  const [watchlists, setWatchlists] = useState<Watchlist[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [atCap, setAtCap] = useState(false);
  const [upgrading, setUpgrading] = useState(false);
  const [modal, setModal] = useState<{ type: "cancelled" | "error"; message: string } | null>(null);

  useEffect(() => {
    if (searchParams.get("checkout_cancelled") === "1") {
      setModal({ type: "cancelled", message: "No worries — you can upgrade anytime." });
      router.replace("/watchlists", { scroll: false });
    }
  }, []);

  const fetchWatchlists = () => {
    fetch("/api/watchlists", {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
      .then(r => r.json())
      .then(data => {
        setWatchlists(Array.isArray(data) ? data : []);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  };

  useEffect(() => {
    fetchWatchlists();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  async function handleCreate(e: React.SyntheticEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!token) return;
    setFormError(null);
    setSubmitting(true);
    const res = await fetch("/api/watchlists", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ name, description }),
    });
    const data = await res.json();
    setSubmitting(false);
    if (!res.ok) {
      if (res.status === 403) setAtCap(true);
      setFormError(data.detail ?? "Failed to create watchlist");
      return;
    }
    setAtCap(false);
    setWatchlists(prev => [...prev, data]);
    setName("");
    setDescription("");
    setShowForm(false);
  }

  async function handleUpgrade() {
    setUpgrading(true);
    try {
      const res = await fetch("/api/billing/checkout", { method: "POST" });
      if (res.ok) {
        const { url } = await res.json();
        window.location.href = url;
      } else {
        setModal({ type: "error", message: "Couldn't start checkout. Please try again." });
      }
    } catch {
      setModal({ type: "error", message: "Network error. Check your connection and try again." });
    }
    setUpgrading(false);
  }

  return (
    <>
      <Nav />

      {modal && (
        <div className={styles.modalOverlay} onClick={() => setModal(null)}>
          <div className={styles.modal} onClick={e => e.stopPropagation()}>
            <div className={modal.type === "error" ? styles.modalIconError : styles.modalIconCancelled}>
              {modal.type === "error" ? "✕" : "→"}
            </div>
            <p className={styles.modalTitle}>
              {modal.type === "error" ? "Something went wrong" : "Checkout cancelled"}
            </p>
            <p className={styles.modalMessage}>{modal.message}</p>
            <button className={styles.modalBtn} onClick={() => setModal(null)}>Got it</button>
          </div>
        </div>
      )}
      <main className={styles.main}>
        <div className={styles.header}>
          <h1 className={styles.heading}>Watchlists</h1>
          <button className={styles.addBtn} onClick={() => setShowForm(v => !v)}>
            {showForm ? "Cancel" : "+ New watchlist"}
          </button>
        </div>

        {atCap && (
          <div className={styles.upgradeBanner}>
            <p>You&apos;ve hit your watchlist limit. Upgrade to Pro for up to 5 watchlists, email digests, and more.</p>
            <button className={styles.upgradeBtn} onClick={handleUpgrade} disabled={upgrading}>
              {upgrading ? "Redirecting..." : "Upgrade to Pro — $7.99/mo"}
            </button>
          </div>
        )}

        {showForm && (
          <form className={styles.form} onSubmit={handleCreate}>
            <div className={styles.formRow}>
              <div className={styles.field}>
                <label className={styles.label}>Name</label>
                <input
                  className={styles.input}
                  type="text"
                  placeholder="e.g. Gaming gear"
                  value={name}
                  onChange={e => setName(e.target.value)}
                  required
                />
              </div>
            </div>
            <div className={styles.field}>
              <label className={styles.label}>What are you looking for?</label>
              <input
                className={styles.input}
                type="text"
                placeholder="e.g. cheap mechanical keyboard for studying"
                value={description}
                onChange={e => setDescription(e.target.value)}
                required
              />
              <p className={styles.hint}>Describe it naturally — we&apos;ll extract the keywords and start hunting immediately.</p>
            </div>
            {formError && <p className={styles.error}>{formError}</p>}
            <button className={styles.submitBtn} type="submit" disabled={submitting}>
              {submitting ? "Creating..." : "Create watchlist"}
            </button>
          </form>
        )}

        {loading ? (
          <div className={styles.empty}>Loading...</div>
        ) : watchlists.length === 0 ? (
          <div className={styles.empty}>No watchlists yet. Create one to start getting alerts.</div>
        ) : (
          <div className={styles.list}>
            {watchlists.map(wl => (
              <WatchlistCard
                key={wl.id}
                watchlist={wl}
                onDelete={id => setWatchlists(prev => prev.filter(w => w.id !== id))}
              />
            ))}
          </div>
        )}
      </main>
    </>
  );
}
