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
}

function WatchlistCard({ watchlist }: { watchlist: Watchlist }) {
  return (
    <div className={styles.card}>
      <div className={styles.cardHeader}>
        <span className={styles.cardName}>{watchlist.name}</span>
        <div className={styles.cardMeta}>
          <span className={styles.metaPill}>score ≥ {watchlist.min_score}</span>
          <span className={styles.metaPill}>{watchlist.alert_tier_threshold}</span>
        </div>
      </div>
      <div className={styles.keywords}>
        {watchlist.keywords.map(kw => (
          <span key={kw} className={styles.keyword}>{kw}</span>
        ))}
      </div>
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

  async function handleCreate(e: React.FormEvent) {
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
              <p className={styles.hint}>Describe it naturally — we&apos;ll extract the keywords.</p>
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
            {watchlists.map(wl => <WatchlistCard key={wl.id} watchlist={wl} />)}
          </div>
        )}
      </main>
    </>
  );
}
