"use client";

import styles from "./PoolCard.module.css";

// The pool listing card, shared by Daily Drops and the Catalog. One shape,
// one look; the pages own layout, this owns the card.

export interface PoolListing {
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

export const MARKETPLACE_LABELS: Record<string, string> = {
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
  studeal: "Studeal",
};

const CONDITION_LABELS: Record<string, string> = {
  new: "New",
  used: "Used",
  refurb: "Refurb",
  refurbished: "Refurb",
};

export function timeAgo(iso: string): string {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 3600) return `${Math.max(1, Math.floor(seconds / 60))}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export default function PoolCard({
  listing,
  index = 0,
}: {
  listing: PoolListing;
  index?: number;
}) {
  const conditionLabel =
    listing.condition && listing.condition !== "unknown"
      ? CONDITION_LABELS[listing.condition] ?? listing.condition
      : null;
  return (
    <div className={styles.card} style={{ animationDelay: `${Math.min(index, 12) * 50}ms` }}>
      <div className={styles.cardBody}>
        <div className={styles.cardTop}>
          <span className={styles.source}>
            {MARKETPLACE_LABELS[listing.marketplace] ?? listing.marketplace}
          </span>
          <span className={styles.seenAt}>{timeAgo(listing.last_seen_at)}</span>
        </div>
        <p className={styles.title}>{listing.title}</p>
        <div className={styles.prices}>
          <span className={styles.salePrice}>${listing.price.toFixed(2)}</span>
          <span className={styles.currency}>{listing.currency}</span>
          {listing.location && <span className={styles.location}>{listing.location}</span>}
        </div>
      </div>
      <div className={styles.cardFooter}>
        <div className={styles.badges}>
          {conditionLabel && (
            <span
              className={[
                styles.condBadge,
                conditionLabel === "Used" ? styles.condUsed : "",
                conditionLabel === "Refurb" ? styles.condRefurb : "",
              ].join(" ")}
            >
              {conditionLabel}
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
