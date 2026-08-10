"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import InspectorPanel, { InspectListing } from "@/components/InspectorPanel";
import { MARKETPLACE_LABELS, timeAgo } from "@/components/PoolCard";
import styles from "./page.module.css";

// Scout: everything you have sent to Scout, with the conversations intact.
// Click any row to reopen the thread exactly where you left it.

interface InspectedItem {
  listing_id: number;
  title: string;
  price: number;
  currency: string;
  marketplace: string;
  url: string;
  image_url: string | null;
  sold: boolean;
  price_dropped: boolean;
  price_at_inspection: number;
  last_message: string | null;
  inspected_at: string;
}

export default function ScoutPage() {
  const [items, setItems] = useState<InspectedItem[] | null>(null);
  const [open, setOpen] = useState<InspectedItem | null>(null);

  const load = async () => {
    try {
      const res = await fetch("/api/listings/inspected");
      if (!res.ok) return;
      const data = await res.json();
      setItems(data.items);
    } catch {
      setItems([]);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const asListing = (item: InspectedItem): InspectListing => ({
    id: item.listing_id,
    title: item.title,
    price: item.price,
    currency: item.currency,
    marketplace: item.marketplace,
    url: item.url,
    image_url: item.image_url,
  });

  return (
    <main className={styles.wrap}>
      <header className={styles.pagehead}>
        <h1>Scout</h1>
        <span className={styles.sub}>everything you have asked Scout to look at</span>
      </header>

      {items !== null && items.length === 0 && (
        <div className={styles.empty}>
          <p className={styles.emptyTitle}>Nothing here yet.</p>
          <p className={styles.emptySub}>
            Find something interesting in <Link href="/dashboard">Daily Drops</Link> and
            hit &quot;Send to Scout&quot;. Your conversations land here and Scout keeps an
            eye on the price after you ask.
          </p>
        </div>
      )}

      {items !== null && items.length > 0 && (
        <div className={styles.list}>
          {items.map((item, i) => (
            <button
              key={item.listing_id}
              type="button"
              className={styles.row}
              style={{ animationDelay: `${Math.min(i, 12) * 40}ms` }}
              onClick={() => setOpen(item)}
            >
              <div className={styles.thumbWrap}>
                {item.image_url ? (
                  <img
                    src={item.image_url}
                    alt=""
                    loading="lazy"
                    referrerPolicy="no-referrer"
                    className={styles.thumb}
                  />
                ) : (
                  <span className={styles.thumbEmpty} aria-hidden>
                    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                      <rect x="3" y="5" width="18" height="14" rx="2" />
                      <circle cx="9" cy="10" r="1.6" />
                      <path d="m5.5 19 5.5-5.5 3 3 2.5-2.5 2 2" />
                    </svg>
                  </span>
                )}
              </div>
              <div className={styles.rowBody}>
                <p className={styles.rowTitle}>{item.title}</p>
                <span className={styles.rowMeta}>
                  {MARKETPLACE_LABELS[item.marketplace] ?? item.marketplace}
                  {" · sent "}{timeAgo(item.inspected_at)}
                  {item.last_message && (
                    <span className={styles.snippet}> · {item.last_message}</span>
                  )}
                </span>
              </div>
              <div className={styles.rowRight}>
                {item.sold ? (
                  <span className={styles.soldBadge}>gone</span>
                ) : item.price_dropped ? (
                  <span className={styles.dropBadge}>
                    dropped to ${item.price.toFixed(0)}
                  </span>
                ) : (
                  <span className={styles.rowPrice}>
                    ${item.price.toFixed(2)} {item.currency}
                  </span>
                )}
              </div>
            </button>
          ))}
        </div>
      )}

      {open && (
        <InspectorPanel listing={asListing(open)} onClose={() => setOpen(null)} />
      )}
    </main>
  );
}
