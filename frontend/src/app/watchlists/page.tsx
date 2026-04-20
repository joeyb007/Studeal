"use client";

import { useEffect, useState } from "react";
import { useSession } from "next-auth/react";
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
  const [watchlists, setWatchlists] = useState<Watchlist[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

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
      setFormError(data.detail ?? "Failed to create watchlist");
      return;
    }
    setWatchlists(prev => [...prev, data]);
    setName("");
    setDescription("");
    setShowForm(false);
  }

  return (
    <>
      <Nav />
      <main className={styles.main}>
        <div className={styles.header}>
          <h1 className={styles.heading}>Watchlists</h1>
          <button className={styles.addBtn} onClick={() => setShowForm(v => !v)}>
            {showForm ? "Cancel" : "+ New watchlist"}
          </button>
        </div>

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
