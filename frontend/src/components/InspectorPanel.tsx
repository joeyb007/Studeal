"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import styles from "./InspectorPanel.module.css";
import { MARKETPLACE_LABELS } from "./PoolCard";

// Minimal listing shape the panel needs; both Daily Drops (PoolListing) and
// the watchlist match rows satisfy it structurally.
export interface InspectListing {
  id: number;
  title: string;
  price: number;
  currency: string;
  marketplace: string;
}

// "Send it to Scout": the inspection thread. Scout takes a look at the real
// listing page (first open runs the browser visit + vision call), the report
// lands as Scout's opening message, and the user chats about it after.

interface Comp {
  id: number;
  title: string;
  price: number;
  marketplace: string;
}

interface Report {
  identification: string;
  condition: string;
  red_flags: string[];
  cant_tell: string;
  seller_questions: string[];
  legitimacy: { level: string; reason: string };
  market_position: string;
  summary: string;
  comps: Comp[];
}

interface Inspection {
  status: "ok" | "listing_gone" | "error";
  report: Report | null;
  comps: Comp[];
  cached: boolean;
}

interface ChatMsg {
  role: "user" | "assistant";
  content: string;
}

export default function InspectorPanel({
  listing,
  watchlistId,
  onClose,
}: {
  listing: InspectListing;
  watchlistId?: number;
  onClose: () => void;
}) {
  const [inspection, setInspection] = useState<Inspection | null>(null);
  const [failed, setFailed] = useState(false);
  const [capped, setCapped] = useState<string | null>(null);
  const [verdict, setVerdict] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [draft, setDraft] = useState("");
  const [replying, setReplying] = useState(false);
  const threadRef = useRef<HTMLDivElement>(null);

  const inspect = useCallback(async () => {
    setFailed(false);
    setCapped(null);
    setInspection(null);
    try {
      const res = await fetch(`/api/listings/${listing.id}/inspect`, { method: "POST" });
      if (res.status === 403) {
        const data = await res.json();
        setCapped(data.detail ?? "Out of free looks this month.");
        return;
      }
      if (!res.ok) throw new Error(String(res.status));
      setInspection(await res.json());
    } catch {
      setFailed(true);
    }
  }, [listing.id]);

  // Tier B: with a watchlist in context, fetch Scout's personal take once the
  // report is in. Cheap text call; failure just means no second message.
  useEffect(() => {
    if (!watchlistId || inspection?.status !== "ok") return;
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`/api/listings/${listing.id}/verdict`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ watchlist_id: watchlistId }),
        });
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setVerdict(data.verdict);
      } catch {
        /* verdict is a bonus, not a blocker */
      }
    })();
    return () => { cancelled = true; };
  }, [watchlistId, inspection?.status, listing.id]);

  useEffect(() => {
    inspect();
  }, [inspect]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, inspection, replying, verdict]);

  const send = async () => {
    const text = draft.trim();
    if (!text || replying || inspection?.status !== "ok") return;
    const next: ChatMsg[] = [...messages, { role: "user", content: text }];
    setMessages(next);
    setDraft("");
    setReplying(true);
    try {
      const res = await fetch(`/api/listings/${listing.id}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: next, watchlist_id: watchlistId ?? null }),
      });
      const data = await res.json();
      setMessages([...next, {
        role: "assistant",
        content: res.ok ? data.reply : "I hit a snag answering that one. Give it another try in a moment.",
      }]);
    } catch {
      setMessages([...next, {
        role: "assistant",
        content: "I hit a snag answering that one. Give it another try in a moment.",
      }]);
    } finally {
      setReplying(false);
    }
  };

  const report = inspection?.status === "ok" ? inspection.report : null;

  return createPortal(
    <div className={styles.overlay} onClick={onClose}>
      <div className={styles.panel} onClick={e => e.stopPropagation()}>
        <div className={styles.head}>
          <div className={styles.headText}>
            <span className={styles.headTitle}>{listing.title}</span>
            <span className={styles.headMeta}>
              ${listing.price.toFixed(2)} {listing.currency} ·{" "}
              {MARKETPLACE_LABELS[listing.marketplace] ?? listing.marketplace}
            </span>
          </div>
          <button className={styles.close} onClick={onClose} aria-label="Close">✕</button>
        </div>

        <div className={styles.thread} ref={threadRef}>
          {!inspection && !failed && !capped && (
            <div className={styles.working}>
              <span className={styles.workingDot} />
              Scout is taking a look at this one. Opening the listing, reading
              the photos. Give it a few seconds.
            </div>
          )}

          {capped && (
            <div className={styles.scoutMsg}>
              <p>{capped}</p>
            </div>
          )}

          {failed && (
            <div className={styles.scoutMsg}>
              <p>Could not get a good look just now. The site may be slow.</p>
              <button className={styles.retry} onClick={inspect}>Try again</button>
            </div>
          )}

          {inspection?.status === "error" && (
            <div className={styles.scoutMsg}>
              <p>
                I could not get a clean look at this one just now. It happens,
                some pages load rough. Worth trying again in a minute.
              </p>
              <button className={styles.retry} onClick={inspect}>Try again</button>
            </div>
          )}

          {inspection?.status === "listing_gone" && (
            <div className={styles.scoutMsg}>
              <p>
                That one is gone. The page says it is no longer available, so
                it likely sold. I have taken it out of your feed.
              </p>
              {inspection.comps.length > 0 && (
                <>
                  <p className={styles.compLead}>Closest live matches right now:</p>
                  <CompStrip comps={inspection.comps} />
                </>
              )}
            </div>
          )}

          {report && (
            <div className={styles.scoutMsg}>
              <ReportSection label="What it is" text={report.identification} />
              <ReportSection label="Condition" text={report.condition} />
              {report.red_flags.length > 0 && (
                <div className={styles.section}>
                  <span className={styles.sectionLabel}>Red flags</span>
                  <ul className={styles.list}>
                    {report.red_flags.map((f, i) => <li key={i}>{f}</li>)}
                  </ul>
                </div>
              )}
              <ReportSection label="What I can't tell from here" text={report.cant_tell} />
              {report.seller_questions.length > 0 && (
                <div className={styles.section}>
                  <span className={styles.sectionLabel}>Ask the seller</span>
                  <ul className={styles.list}>
                    {report.seller_questions.map((q, i) => <li key={i}>{q}</li>)}
                  </ul>
                </div>
              )}
              {report.legitimacy.level !== "fine" && (
                <div className={[styles.section, styles.legit].join(" ")}>
                  <span className={styles.sectionLabel}>
                    {report.legitimacy.level === "likely_scam" ? "Likely scam" : "Caution"}
                  </span>
                  <p className={styles.sectionText}>{report.legitimacy.reason}</p>
                </div>
              )}
              <ReportSection label="Price check" text={report.market_position} />
              {report.comps.length > 0 && <CompStrip comps={report.comps} />}
              <ReportSection label="Bottom line" text={report.summary} />
            </div>
          )}

          {verdict && (
            <div className={styles.scoutMsg}>
              <div className={styles.section}>
                <span className={styles.sectionLabel}>Scout&apos;s take for you</span>
                <p className={styles.sectionText}>{verdict}</p>
              </div>
            </div>
          )}

          {messages.map((m, i) => (
            <div key={i} className={m.role === "user" ? styles.userMsg : styles.scoutMsg}>
              <p className={styles.sectionText}>{m.content}</p>
            </div>
          ))}

          {replying && (
            <div className={styles.working}>
              <span className={styles.workingDot} />
              Scout is thinking…
            </div>
          )}
        </div>

        <div className={styles.composer}>
          <input
            className={styles.input}
            placeholder={
              inspection?.status === "ok"
                ? "Ask Scout about this listing…"
                : "Scout needs a successful look before you can chat"
            }
            value={draft}
            disabled={inspection?.status !== "ok" || replying}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") send(); }}
          />
          <button
            className={styles.send}
            onClick={send}
            disabled={inspection?.status !== "ok" || replying || !draft.trim()}
          >
            Send
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

function ReportSection({ label, text }: { label: string; text: string }) {
  if (!text) return null;
  return (
    <div className={styles.section}>
      <span className={styles.sectionLabel}>{label}</span>
      <p className={styles.sectionText}>{text}</p>
    </div>
  );
}

function CompStrip({ comps }: { comps: Comp[] }) {
  return (
    <div className={styles.comps}>
      {comps.map(c => (
        <div key={c.id} className={styles.compRow}>
          <span className={styles.compTitle}>{c.title}</span>
          <span className={styles.compMeta}>
            ${c.price.toFixed(0)} · {MARKETPLACE_LABELS[c.marketplace] ?? c.marketplace}
          </span>
        </div>
      ))}
    </div>
  );
}
