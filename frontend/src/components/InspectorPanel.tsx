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
  url: string;
  image_url?: string | null;
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
  headline?: string;
  condition_grade?: string;
  identification: string;
  condition: string;
  red_flags: string[];
  cant_tell: string;
  seller_questions: string[];
  legitimacy: { level: string; reason: string };
  market_position: string;
  summary: string;
  comps: Comp[];
  price_read?: { level: string; text: string } | null;
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
  const [showNotes, setShowNotes] = useState(false);
  const [stage, setStage] = useState(0);
  const threadRef = useRef<HTMLDivElement>(null);

  // Staged progress while Scout works. The phases happen in this order for
  // real (navigate, screenshot/read, report against comps); timing is
  // client-approximated. Cached hits resolve before stage 1 ever shows.
  useEffect(() => {
    if (inspection || failed || capped) return;
    const t1 = setTimeout(() => setStage(1), 6_000);
    const t2 = setTimeout(() => setStage(2), 15_000);
    return () => { clearTimeout(t1); clearTimeout(t2); };
  }, [inspection, failed, capped]);

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

  // Rehydrate the persisted thread: a friend remembers the conversation.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`/api/listings/${listing.id}/messages`);
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled && Array.isArray(data.messages) && data.messages.length > 0) {
          setMessages(data.messages.map((m: { role: string; content: string }) => ({
            role: m.role === "user" ? "user" : "assistant",
            content: m.content,
          })));
        }
      } catch {
        /* history is a bonus; the live thread still works without it */
      }
    })();
    return () => { cancelled = true; };
  }, [listing.id]);

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
          <div className={styles.userMsg}>
            <div className={styles.sentCard}>
              {listing.image_url && (
                <img
                  src={listing.image_url}
                  alt=""
                  loading="lazy"
                  referrerPolicy="no-referrer"
                  className={styles.sentThumb}
                />
              )}
              <div className={styles.sentBody}>
                <span className={styles.sentTitle}>{listing.title}</span>
                <span className={styles.sentMeta}>
                  ${listing.price.toFixed(2)} {listing.currency} ·{" "}
                  {MARKETPLACE_LABELS[listing.marketplace] ?? listing.marketplace}
                </span>
              </div>
            </div>
            <p className={styles.sentCaption}>hey Scout, take a look at this one?</p>
          </div>

          {!inspection && !failed && !capped && (
            <div className={styles.scoutRow}>
              <ScoutAvatar />
              <div className={styles.working}>
                {STAGES.slice(0, stage + 1).map((label, i) => (
                  <span key={label} className={styles.stageLine}>
                    {i < stage ? (
                      <span className={styles.stageDone}>✓</span>
                    ) : (
                      <span className={styles.workingDot} />
                    )}
                    {label}
                  </span>
                ))}
              </div>
            </div>
          )}

          {capped && (
            <div className={styles.scoutMsg}>
              <p>{capped}</p>
            </div>
          )}

          {failed && (
            <div className={styles.scoutRow}>
            <ScoutAvatar />
            <div className={styles.scoutMsg}>
              <p>Could not get a good look just now. The site may be slow.</p>
              <button className={styles.retry} onClick={inspect}>Try again</button>
            </div>
            </div>
          )}

          {inspection?.status === "error" && (
            <div className={styles.scoutRow}>
            <ScoutAvatar />
            <div className={styles.scoutMsg}>
              <p>
                I could not get a clean look at this one just now. It happens,
                some pages load rough. Worth trying again in a minute.
              </p>
              <button className={styles.retry} onClick={inspect}>Try again</button>
            </div>
            </div>
          )}

          {inspection?.status === "listing_gone" && (
            <div className={styles.scoutRow}>
            <ScoutAvatar />
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
            </div>
          )}

          {report && (
            <div className={styles.scoutRow}>
            <ScoutAvatar />
            <div className={styles.scoutMsg}>
              {report.headline ? (
                <>
                  <div className={styles.overview}>
                    {listing.image_url && (
                      <img
                        src={listing.image_url}
                        alt=""
                        loading="lazy"
                        referrerPolicy="no-referrer"
                        className={styles.overviewThumb}
                      />
                    )}
                    <div className={styles.overviewBody}>
                      <p className={styles.overviewHeadline}>{report.headline}</p>
                      <div className={styles.badges}>
                        {report.price_read && (
                          <span className={[styles.badge, badgeTone(report.price_read.level)].join(" ")}>
                            {report.price_read.text}
                          </span>
                        )}
                        {report.condition_grade && report.condition_grade !== "unknown" && (
                          <span className={[styles.badge, badgeTone(report.condition_grade)].join(" ")}>
                            condition: {report.condition_grade}
                          </span>
                        )}
                        <span className={[styles.badge, badgeTone(report.legitimacy.level)].join(" ")}>
                          {report.legitimacy.level === "fine"
                            ? "looks legit"
                            : report.legitimacy.level === "likely_scam"
                              ? "likely scam"
                              : "caution"}
                        </span>
                      </div>
                    </div>
                  </div>
                  <button
                    type="button"
                    className={styles.notesToggle}
                    onClick={() => setShowNotes(v => !v)}
                  >
                    {showNotes ? "Hide Scout's full notes" : "Scout's full notes"}
                  </button>
                  {showNotes && <FullNotes report={report} />}
                </>
              ) : (
                <FullNotes report={report} />
              )}
            </div>
            </div>
          )}

          {verdict && (
            <div className={styles.scoutRow}>
            <ScoutAvatar />
            <div className={styles.scoutMsg}>
              <div className={styles.section}>
                <span className={styles.sectionLabel}>Scout&apos;s take for you</span>
                <p className={styles.sectionText}>{verdict}</p>
              </div>
            </div>
            </div>
          )}

          {messages.map((m, i) =>
            m.role === "user" ? (
              <div key={i} className={styles.userMsg}>
                <p className={styles.sectionText}>{m.content}</p>
              </div>
            ) : (
              <div key={i} className={styles.scoutRow}>
                <ScoutAvatar />
                <div className={styles.scoutMsg}>
                  <p className={styles.sectionText}>{m.content}</p>
                </div>
              </div>
            ),
          )}

          {replying && (
            <div className={styles.scoutRow}>
              <ScoutAvatar />
              <div className={styles.working}>
                <span className={styles.stageLine}>
                  <span className={styles.workingDot} />
                  Scout is thinking…
                </span>
              </div>
            </div>
          )}
        </div>

        {inspection?.status === "ok" && (
          <a
            href={listing.url}
            target="_blank"
            rel="noopener noreferrer"
            className={styles.buyCta}
          >
            Open on {MARKETPLACE_LABELS[listing.marketplace] ?? listing.marketplace} →
          </a>
        )}

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

const STAGES = [
  "Opening the listing…",
  "Reading the photos…",
  "Checking it against the market…",
];

function ScoutAvatar() {
  return (
    <span className={styles.avatar} aria-hidden>
      <img src="/logo.svg" alt="" className={styles.avatarImg} />
    </span>
  );
}

function badgeTone(level: string): string {
  if (["fair", "under", "good", "fine"].includes(level)) return styles.badgeGood;
  if (["over", "worn", "likely_scam"].includes(level)) return styles.badgeBad;
  return styles.badgeWarn;
}

function FullNotes({ report }: { report: Report }) {
  return (
    <>
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
    </>
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
