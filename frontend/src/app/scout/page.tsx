"use client";

import { useEffect, useState } from "react";

// Same typing treatment Scout has everywhere else: interval-sliced text with
// a blinking cursor until done.
function useTyped(text: string, speed = 16, delay = 250) {
  const [shown, setShown] = useState("");
  const [done, setDone] = useState(false);
  useEffect(() => {
    setShown("");
    setDone(false);
    if (!text) { setDone(true); return; }
    let interval: ReturnType<typeof setInterval> | null = null;
    const starter = setTimeout(() => {
      let i = 0;
      interval = setInterval(() => {
        i++;
        setShown(text.slice(0, i));
        if (i >= text.length) {
          if (interval) clearInterval(interval);
          setDone(true);
        }
      }, speed);
    }, delay);
    return () => { clearTimeout(starter); if (interval) clearInterval(interval); };
  }, [text, speed, delay]);
  return { shown, done };
}
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

interface ResolveResult {
  listing: {
    id: number; title: string; price: number; currency: string;
    marketplace: string; url: string; image_url: string | null;
  } | null;
  watchlist: { id: number; name: string } | null;
}

export default function ScoutPage() {
  const [items, setItems] = useState<InspectedItem[] | null>(null);
  const [open, setOpen] = useState<InspectedItem | null>(null);

  // Check-a-listing: paste a marketplace link, Scout finds it in the pool.
  const [linkDraft, setLinkDraft] = useState("");
  const [resolving, setResolving] = useState(false);
  const [resolved, setResolved] = useState<ResolveResult | "nomatch" | null>(null);
  const [fetching, setFetching] = useState(false);
  const [fetchNote, setFetchNote] = useState<string | null>(null);
  const [needsPrice, setNeedsPrice] = useState<{ title: string; image_url: string | null; location: string | null } | null>(null);
  const [priceDraft, setPriceDraft] = useState("");
  const [linkOpen, setLinkOpen] = useState<{ listing: InspectListing; watchlistId?: number } | null>(null);

  const fetchLink = async (manualPrice?: number) => {
    const url = linkDraft.trim();
    if (!url || fetching) return;
    setFetching(true);
    setFetchNote(null);
    try {
      const res = await fetch("/api/listings/fetch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, manual_price: manualPrice ?? null }),
      });
      if (res.status === 403) {
        const data = await res.json();
        setFetchNote(data.detail ?? "Out of fresh looks this month.");
        return;
      }
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      if (data.status === "fetched" && data.listing) {
        setResolved({ listing: data.listing, watchlist: data.watchlist ?? null });
        setNeedsPrice(null);
      } else if (data.status === "needs_price" && data.partial) {
        setNeedsPrice(data.partial);
      } else if (data.status === "unsupported") {
        setFetchNote("Scout can only grab listings from Kijiji, eBay, and Facebook Marketplace right now.");
      } else {
        setFetchNote("Couldn't get a clean grab of that page. The site may be slow or blocking; worth trying again in a minute.");
      }
    } catch {
      setFetchNote("Couldn't get a clean grab of that page. The site may be slow or blocking; worth trying again in a minute.");
    } finally {
      setFetching(false);
    }
  };

  const checkLink = async () => {
    const url = linkDraft.trim();
    if (!url || resolving) return;
    setResolving(true);
    setResolved(null);
    setFetchNote(null);
    try {
      const res = await fetch("/api/listings/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      if (!res.ok) throw new Error(String(res.status));
      const data: ResolveResult = await res.json();
      setResolved(data.listing ? data : "nomatch");
    } catch {
      setResolved("nomatch");
    } finally {
      setResolving(false);
    }
  };

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

      <div className={styles.checkBar}>
        <input
          className={styles.checkInput}
          type="text"
          value={linkDraft}
          onChange={e => { setLinkDraft(e.target.value); setResolved(null); setNeedsPrice(null); setFetchNote(null); }}
          onKeyDown={e => { if (e.key === "Enter") checkLink(); }}
          placeholder="Paste a listing link and Scout will check it out…"
        />
        <button className={styles.checkBtn} onClick={checkLink} disabled={resolving || !linkDraft.trim()}>
          {resolving ? "Looking…" : "Check it"}
        </button>
      </div>

      {needsPrice && (
        <div className={styles.resolveCard}>
          {needsPrice.image_url && (
            <img src={needsPrice.image_url} alt="" loading="lazy" referrerPolicy="no-referrer" className={styles.resolveThumb} />
          )}
          <div className={styles.resolveBody}>
            <span className={styles.resolveTitle}>{needsPrice.title}</span>
            <span className={styles.resolveMeta}>
              {needsPrice.location ? `${needsPrice.location} · ` : ""}Facebook hides the price on this one.
              What&apos;s it listed at?
            </span>
            <div className={styles.resolveActions}>
              <input
                className={styles.priceInput}
                type="number"
                placeholder="$"
                value={priceDraft}
                onChange={e => setPriceDraft(e.target.value)}
                onKeyDown={e => {
                  const parsed = parseFloat(priceDraft);
                  if (e.key === "Enter" && !isNaN(parsed) && parsed > 0) fetchLink(parsed);
                }}
              />
              <button
                className={styles.resolveGo}
                disabled={fetching || !(parseFloat(priceDraft) > 0)}
                onClick={() => fetchLink(parseFloat(priceDraft))}
              >
                {fetching ? "Grabbing it…" : "Add price & fetch"}
              </button>
            </div>
          </div>
        </div>
      )}

      {resolved === "nomatch" && !needsPrice && (
        <ScoutInvite
          fetching={fetching}
          note={fetchNote}
          onSend={() => fetchLink()}
        />
      )}

      {resolved !== null && resolved !== "nomatch" && resolved.listing && (
        <div className={styles.resolveCard}>
          {resolved.listing.image_url && (
            <img src={resolved.listing.image_url} alt="" loading="lazy" referrerPolicy="no-referrer" className={styles.resolveThumb} />
          )}
          <div className={styles.resolveBody}>
            <span className={styles.resolveTitle}>{resolved.listing.title}</span>
            <span className={styles.resolveMeta}>
              ${resolved.listing.price.toFixed(0)} · {MARKETPLACE_LABELS[resolved.listing.marketplace] ?? resolved.listing.marketplace}
              {resolved.watchlist && <> · looks like one for your <b>{resolved.watchlist.name}</b> agent</>}
            </span>
            <div className={styles.resolveActions}>
              {resolved.watchlist && (
                <button
                  className={styles.resolveGo}
                  onClick={() => setLinkOpen({ listing: resolved.listing!, watchlistId: resolved.watchlist!.id })}
                >
                  Ask Scout · with {resolved.watchlist.name}&apos;s numbers
                </button>
              )}
              <button
                className={resolved.watchlist ? styles.resolvePlain : styles.resolveGo}
                onClick={() => setLinkOpen({ listing: resolved.listing! })}
              >
                {resolved.watchlist ? "Just the basics" : "Ask Scout"}
              </button>
            </div>
          </div>
        </div>
      )}

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
                <span className={[
                  styles.rowPrice,
                  item.sold ? styles.rowPriceSold : item.price_dropped ? styles.rowPriceDrop : "",
                ].join(" ")}>
                  ${item.price.toFixed(0)} {item.currency}
                </span>
                {item.sold ? (
                  <span className={styles.statusSold}>sold</span>
                ) : (
                  <span className={styles.statusLive} title={item.price_dropped ? `dropped since you asked (was $${item.price_at_inspection.toFixed(0)})` : undefined}>
                    live{item.price_dropped ? " · dropped" : ""}
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

      {linkOpen && (
        <InspectorPanel
          listing={linkOpen.listing}
          watchlistId={linkOpen.watchlistId}
          onClose={() => { setLinkOpen(null); load(); }}
        />
      )}
    </main>
  );
}

function ScoutInvite({
  fetching,
  note,
  onSend,
}: {
  fetching: boolean;
  note: string | null;
  onSend: () => void;
}) {
  const text = note
    ?? (fetching
      ? "On my way. Give me twenty seconds or so…"
      : "New one on me. Want me to go check it out? Takes about twenty seconds.");
  const { shown, done } = useTyped(text);
  return (
    <div className={styles.scoutInvite}>
      <span className={styles.inviteAvatar} aria-hidden>
        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
          <path d="M12 3 L14 12 L12 21 L10 12 Z" />
          <path d="M3 12 L12 10 L21 12 L12 14 Z" />
        </svg>
      </span>
      <p className={styles.inviteText}>
        {shown}
        {!done && <span className={styles.inviteCursor}>▍</span>}
      </p>
      <button className={styles.inviteBtn} onClick={onSend} disabled={fetching}>
        {fetching ? "On it…" : note ? "Try again" : "Send Scout"}
      </button>
    </div>
  );
}
