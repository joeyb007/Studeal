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
  role: "user" | "assistant" | "sys";
  content: string;
  images?: string[];               // media keys
}

interface PendingImage {
  key: string;
  previewUrl: string;
}

interface Checklist {
  items: {
    check: string;
    status: "open" | "satisfied" | "flagged";
    evidence: string | null;
    verify_via?: "ask_seller" | "at_pickup" | "confirmed" | null;
    added?: boolean;
  }[];
  ready: boolean;
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
  const [checklist, setChecklist] = useState<Checklist | null>(null);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [draft, setDraft] = useState("");
  const [replying, setReplying] = useState(false);
  const [pendingImages, setPendingImages] = useState<PendingImage[]>([]);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [showNotes, setShowNotes] = useState(false);
  const [stage, setStage] = useState(0);
  const [collapsed, setCollapsed] = useState(false);
  const threadRef = useRef<HTMLDivElement>(null);

  // Checklist deltas become in-stream system lines ("✓ checked off: …").
  const applyChecklist = (next: Checklist) => {
    setChecklist(prev => {
      if (prev) {
        const lines: ChatMsg[] = [];
        const satisfied = next.items.filter((item, i) =>
          item.status === "satisfied" && prev.items[i]?.status === "open");
        for (const item of satisfied) {
          lines.push({ role: "sys", content: `✓ checked off: ${item.check.replace(/\.$/, "")}` });
        }
        for (const item of next.items.slice(prev.items.length)) {
          lines.push({ role: "sys", content: `+ added to the list: ${item.check.replace(/\.$/, "")}` });
        }
        if (lines.length) setMessages(m => [...m, ...lines]);
      }
      return next;
    });
  };

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

  // With a watchlist in context: seed the ready-to-buy checklist first (the
  // verdict reads its state), then fetch Scout's personal take. Both are
  // bonuses, never blockers.
  useEffect(() => {
    if (!watchlistId || inspection?.status !== "ok") return;
    let cancelled = false;
    (async () => {
      try {
        const cl = await fetch(`/api/listings/${listing.id}/checklist?watchlist_id=${watchlistId}`);
        if (cl.ok && !cancelled) setChecklist(await cl.json());   // first load: no delta lines
      } catch {
        /* checklist is a bonus */
      }
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

  const toggleCheck = async (index: number, status: "open" | "satisfied") => {
    try {
      const res = await fetch(`/api/listings/${listing.id}/checklist`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ index, status }),
      });
      if (res.ok) setChecklist(await res.json());
    } catch {
      /* leave state as-is */
    }
  };

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
          setMessages(data.messages.map((m: { role: string; content: string; images?: string[] }) => ({
            role: m.role === "user" ? "user" : "assistant",
            content: m.content,
            images: m.images ?? [],
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

  const attachFiles = async (files: FileList | null) => {
    if (!files || uploading) return;
    const room = 4 - pendingImages.length;
    const picked = Array.from(files).slice(0, Math.max(0, room));
    if (picked.length === 0) return;
    setUploading(true);
    try {
      for (const file of picked) {
        if (file.size > 5 * 1024 * 1024) continue;
        const form = new FormData();
        form.append("file", file);
        const res = await fetch(`/api/listings/${listing.id}/images`, {
          method: "POST",
          body: form,
        });
        if (!res.ok) continue;
        const data = await res.json();
        setPendingImages(prev =>
          prev.length < 4
            ? [...prev, { key: data.key, previewUrl: URL.createObjectURL(file) }]
            : prev,
        );
      }
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const send = async () => {
    const text = draft.trim();
    if ((!text && pendingImages.length === 0) || replying || inspection?.status !== "ok") return;
    const imageKeys = pendingImages.map(p => p.key);
    const next: ChatMsg[] = [...messages, {
      role: "user",
      content: text || "Here are some screenshots, take a look?",
      images: imageKeys,
    }];
    setMessages(next);
    setDraft("");
    setPendingImages([]);
    setReplying(true);
    try {
      const res = await fetch(`/api/listings/${listing.id}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: next
            .filter(m => m.role !== "sys")
            .map(({ role, content }) => ({ role, content })),
          watchlist_id: watchlistId ?? null,
          image_keys: imageKeys,
        }),
      });
      const data = await res.json();
      setMessages([...next, {
        role: "assistant",
        content: res.ok ? data.reply : "I hit a snag answering that one. Give it another try in a moment.",
      }]);
      if (res.ok && data.checklist) applyChecklist(data.checklist);
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
        <div className={styles.dossier}>
          <div className={styles.dosTop}>
            {listing.image_url ? (
              <img src={listing.image_url} alt="" loading="lazy" referrerPolicy="no-referrer" className={styles.dosThumb} />
            ) : null}
            <span className={styles.dosTitle}>{listing.title}</span>
            <span className={styles.dosPrice}>${listing.price.toFixed(0)}</span>
            {collapsed && checklist && checklist.items.length > 0 && (
              <span className={styles.dosCount}>
                {checklist.items.filter(i => i.status === "satisfied").length}/{checklist.items.length}
              </span>
            )}
            <button className={styles.close} onClick={onClose} aria-label="Close">✕</button>
          </div>
          {!collapsed && report && (
            <div className={styles.dosChips}>
              {report.price_read && (
                <span className={[styles.badge, badgeTone(report.price_read.level)].join(" ")}>{report.price_read.text}</span>
              )}
              {report.condition_grade && report.condition_grade !== "unknown" && (
                <span className={[styles.badge, badgeTone(report.condition_grade)].join(" ")}>condition: {report.condition_grade}</span>
              )}
              <span className={[styles.badge, badgeTone(report.legitimacy.level)].join(" ")}>
                {report.legitimacy.level === "fine" ? "looks legit"
                  : report.legitimacy.level === "likely_scam" ? "likely scam" : "caution"}
              </span>
            </div>
          )}
          {!collapsed && checklist && checklist.items.length > 0 && (
            <div className={styles.readiness}>
              <span className={styles.readyLabel}>ready?</span>
              <span className={styles.meterBar}>
                <span
                  className={[styles.meterFill, checklist.ready ? styles.meterDone : ""].join(" ")}
                  style={{ width: `${Math.round(100 * checklist.items.filter(i => i.status === "satisfied").length / checklist.items.length)}%` }}
                />
              </span>
              <span className={[styles.meterCount, checklist.ready ? styles.meterCountDone : ""].join(" ")}>
                {checklist.ready ? "ready" : `${checklist.items.filter(i => i.status === "satisfied").length} / ${checklist.items.length}`}
              </span>
            </div>
          )}
        </div>

        <div
          className={styles.thread}
          ref={threadRef}
          onScroll={e => setCollapsed((e.target as HTMLDivElement).scrollTop > 48)}
        >
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
              <div className={styles.workingWrap}>
                <span className={styles.workingText}>{STAGES[stage]}</span>
                <TypingBubble />
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
            <div className={styles.scoutGroup}>
              <div className={styles.who}><ScoutAvatar small /> scout</div>
              <div className={[styles.scoutBubble, styles.scoutBubbleFirst, checklist?.ready ? styles.closer : ""].join(" ")}>
                <p className={styles.sectionText}>{verdict ?? report.headline ?? report.summary}</p>
                {checklist && checklist.items.length > 0 && (
                  <div className={styles.critList}>
                    {checklist.items.map((item, i) => (
                      <button
                        key={i}
                        type="button"
                        className={styles.critRow}
                        title={item.status === "satisfied" ? "Mark as not verified" : "Mark as verified yourself"}
                        onClick={() => toggleCheck(i, item.status === "satisfied" ? "open" : "satisfied")}
                      >
                        <span className={[
                          styles.tick,
                          item.status === "satisfied" ? styles.tickOn : item.status === "flagged" ? styles.tickFlag : "",
                        ].join(" ")}>
                          {item.status === "satisfied" ? "✓" : item.status === "flagged" ? "!" : "○"}
                        </span>
                        <span className={styles.critBody}>
                          <span className={item.status === "satisfied" ? styles.critTextDone : styles.critText}>{item.check}</span>
                          {item.evidence && <span className={styles.critEvidence}>{item.evidence}</span>}
                        </span>
                        {item.status !== "satisfied" && item.verify_via && item.verify_via !== "confirmed" && (
                          <span className={[styles.verifyTag, item.verify_via === "ask_seller" ? styles.tagAsk : styles.tagPickup].join(" ")}>
                            {item.verify_via === "ask_seller" ? "ask seller" : "at pickup"}
                          </span>
                        )}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <button type="button" className={styles.notesToggle} onClick={() => setShowNotes(v => !v)}>
                {showNotes ? "Hide Scout's full notes" : "Scout's full notes"}
              </button>
              {showNotes && (
                <div className={styles.scoutBubble}><FullNotes report={report} /></div>
              )}
            </div>
          )}

          {messages.map((m, i) =>
            m.role === "sys" ? (
              <div key={i} className={styles.sysLine}>{m.content}</div>
            ) : m.role === "user" ? (
              <div key={i} className={styles.userMsg}>
                {m.images && m.images.length > 0 && (
                  <div className={styles.msgImages}>
                    {m.images.map(k => (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img key={k} src={`/api/media/${k}`} alt="" className={styles.msgImage} loading="lazy" />
                    ))}
                  </div>
                )}
                <p className={styles.sectionText}>{m.content}</p>
              </div>
            ) : (
              <div key={i} className={styles.scoutGroup}>
                {messages[i - 1]?.role !== "assistant" && (
                  <div className={styles.who}><ScoutAvatar small /> scout</div>
                )}
                <div className={[styles.scoutBubble, messages[i - 1]?.role !== "assistant" ? styles.scoutBubbleFirst : ""].join(" ")}>
                  <p className={styles.sectionText}>{m.content}</p>
                </div>
              </div>
            ),
          )}

          {replying && (
            <div className={styles.scoutGroup}>
              <div className={styles.who}><ScoutAvatar small /> scout</div>
              <TypingBubble />
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

        {pendingImages.length > 0 && (
          <div className={styles.pendingStrip}>
            {pendingImages.map(p => (
              <span key={p.key} className={styles.pendingWrap}>
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={p.previewUrl} alt="" className={styles.pendingThumb} />
                <button
                  type="button"
                  className={styles.pendingRemove}
                  aria-label="Remove screenshot"
                  onClick={() => setPendingImages(prev => prev.filter(x => x.key !== p.key))}
                >
                  ✕
                </button>
              </span>
            ))}
            {uploading && <span className={styles.pendingNote}>uploading…</span>}
          </div>
        )}

        <div className={styles.composer}>
          <input
            ref={fileInputRef}
            type="file"
            accept="image/jpeg,image/png,image/webp"
            multiple
            hidden
            onChange={e => attachFiles(e.target.files)}
          />
          <button
            type="button"
            className={styles.attachBtn}
            title="Attach screenshots (seller photos, chat screenshots)"
            aria-label="Attach screenshots"
            disabled={inspection?.status !== "ok" || replying || uploading || pendingImages.length >= 4}
            onClick={() => fileInputRef.current?.click()}
          >
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="5" width="18" height="14" rx="2" /><circle cx="9" cy="10" r="1.6" /><path d="m5.5 19 5.5-5.5 3 3 2.5-2.5 2 2" /></svg>
          </button>
          <input
            className={styles.input}
            placeholder={
              inspection?.status === "ok"
                ? "Ask Scout, or paste what the seller said…"
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
            disabled={inspection?.status !== "ok" || replying || (!draft.trim() && pendingImages.length === 0)}
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

function TypingBubble() {
  return (
    <div className={styles.typingBubble} aria-hidden>
      <span className={styles.typingDot} />
      <span className={styles.typingDot} />
      <span className={styles.typingDot} />
    </div>
  );
}

function ScoutAvatar({ small = false }: { small?: boolean }) {
  return (
    <span className={[styles.avatar, small ? styles.avatarSmall : ""].join(" ")} aria-hidden>
      <svg viewBox="0 0 24 24" width={small ? 9 : 15} height={small ? 9 : 15} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
        <path d="M12 3 L14 12 L12 21 L10 12 Z" />
        <path d="M3 12 L12 10 L21 12 L12 14 Z" />
      </svg>
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
