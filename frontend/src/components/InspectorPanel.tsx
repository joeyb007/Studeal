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

interface Rundown {
  deal: string;
  verified: { check: string; evidence: string | null }[];
  at_pickup: string[];
  walk_if: string[];
  safety: string;
}

interface ChatMsg {
  role: "user" | "assistant" | "sys";
  content: string;
  images?: string[];               // media keys
  sendNext?: string;               // ready-to-send seller message riding this reply
  kind?: "closer" | "rundown";
  rundown?: Rundown;
}

interface PendingImage {
  key: string;
  previewUrl: string;
}

// Same typing treatment Scout has everywhere: interval-sliced text with a
// blinking cursor. instant skips the theater (rehydrated threads).
function useTypedText(text: string, speed: number, instant: boolean) {
  const [shown, setShown] = useState(instant ? text : "");
  const [done, setDone] = useState(instant);
  useEffect(() => {
    if (instant) { setShown(text); setDone(true); return; }
    setShown("");
    setDone(false);
    if (!text) { setDone(true); return; }
    let interval: ReturnType<typeof setInterval> | null = null;
    // The bubble lands first, then Scout speaks: the pause lets the entrance
    // finish so the type-out reads as a person starting to talk.
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
    }, 340);
    return () => {
      clearTimeout(starter);
      if (interval) clearInterval(interval);
    };
  }, [text, speed, instant]);
  return { shown, done };
}

function Typed({
  text,
  instant = false,
  speed = 14,
  onDone,
}: {
  text: string;
  instant?: boolean;
  speed?: number;
  onDone?: () => void;
}) {
  const { shown, done } = useTypedText(text, speed, instant);
  const fired = useRef(false);
  useEffect(() => {
    if (done && !fired.current) {
      fired.current = true;
      onDone?.();
    }
  }, [done, onDone]);
  return (
    <p className={styles.sectionText}>
      {shown}
      {!done && <span className={styles.typeCursor}>▍</span>}
    </p>
  );
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
  tailoring?: { question: string; chips: string[]; answer: string | null } | null;
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
  const [rundownBusy, setRundownBusy] = useState(false);
  const [rundownDone, setRundownDone] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [stage, setStage] = useState(0);
  const [collapsed, setCollapsed] = useState(false);
  // Opening theater: bubbles type in sequence; rehydrated threads skip it.
  const [openStage, setOpenStage] = useState(1);
  const [theater, setTheater] = useState(true);
  const [checklistFailed, setChecklistFailed] = useState(false);
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

  // The checklist seeds for EVERY inspection (agent playbook merge, or the
  // report's own fields when no agent grounds it); the personal verdict
  // needs an agent. Both are bonuses, never blockers.
  useEffect(() => {
    if (inspection?.status !== "ok") return;
    let cancelled = false;
    (async () => {
      try {
        const query = watchlistId ? `?watchlist_id=${watchlistId}` : "";
        const cl = await fetch(`/api/listings/${listing.id}/checklist${query}`);
        if (cl.ok && !cancelled) setChecklist(await cl.json());   // first load: no delta lines
        else if (!cancelled) setChecklistFailed(true);
      } catch {
        if (!cancelled) setChecklistFailed(true);
      }
      if (!watchlistId) return;
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

  const fetchRundown = async () => {
    if (rundownBusy) return;
    setRundownBusy(true);
    try {
      const res = await fetch(`/api/listings/${listing.id}/rundown`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ watchlist_id: watchlistId ?? null }),
      });
      if (!res.ok) return;
      const data: Rundown = await res.json();
      setMessages(m => [...m, { role: "assistant", content: "", kind: "rundown", rundown: data }]);
      setRundownDone(true);
    } catch {
      /* the chip stays; they can tap again */
    } finally {
      setRundownBusy(false);
    }
  };

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
          setTheater(false);
          setOpenStage(9);
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

  const send = async (override?: { text: string; tailoring?: boolean }) => {
    const text = (override?.text ?? draft).trim();
    if ((!text && pendingImages.length === 0) || replying || !chatOpen) return;
    const imageKeys = override ? [] : pendingImages.map(p => p.key);
    const next: ChatMsg[] = [...messages, {
      role: "user",
      content: text || "Here are some screenshots, take a look?",
      images: imageKeys,
    }];
    setMessages(next);
    if (!override) {
      setDraft("");
      setPendingImages([]);
    }
    if (override?.tailoring) {
      // The chips disappear the moment one is tapped.
      setChecklist(prev => prev?.tailoring
        ? { ...prev, tailoring: { ...prev.tailoring, answer: text } }
        : prev);
    }
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
          tailoring_answer: override?.tailoring ?? false,
        }),
      });
      const data = await res.json();
      const additions: ChatMsg[] = [{
        role: "assistant",
        content: res.ok ? data.reply : "I hit a snag answering that one. Give it another try in a moment.",
        sendNext: res.ok && data.send_next ? data.send_next : undefined,
      }];
      if (res.ok && data.closer) {
        additions.push({ role: "assistant", content: data.closer, kind: "closer" });
      }
      setMessages([...next, ...additions]);
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
  // A confirmed purchase closes the conversation; so does a dead listing.
  const concluded = rundownDone
    || messages.some(m => m.role === "assistant" && m.content.startsWith("PICKUP RUNDOWN"));
  const gone = inspection?.status === "listing_gone";
  const chatOpen = inspection?.status === "ok" && !concluded;

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

          {report && (() => {
            const call = report.headline ?? report.summary ?? "Took a look.";
            const what = [report.identification, report.condition].filter(Boolean).join(" ");
            const price = report.market_position ?? "";
            const advance = (after: number) => {
              let stage = after;
              if (stage === 2 && !what) stage = 3;
              if (stage === 3 && !price && report.comps.length === 0) stage = 4;
              setOpenStage((s0: number) => Math.max(s0, stage));
            };
            return (
              <div className={styles.scoutGroup}>
                <div className={styles.who}><ScoutAvatar small /> scout</div>
                <div className={[styles.scoutBubble, styles.scoutBubbleFirst].join(" ")}>
                  <Typed text={call} instant={!theater} onDone={() => advance(2)} />
                </div>
                {openStage >= 2 && what && (
                  <div className={styles.scoutBubble}>
                    <Typed text={what} instant={!theater} onDone={() => advance(3)} />
                  </div>
                )}
                {openStage >= 3 && (price || report.comps.length > 0) && (
                  <div className={styles.scoutBubble}>
                    {price
                      ? <Typed text={price} instant={!theater} onDone={() => advance(4)} />
                      : null}
                    {(openStage >= 4 || !price) && report.comps.length > 0 && (
                      <div className={styles.compsIn}>
                        <p className={styles.compLead}>What similar ones are going for:</p>
                        <CompStrip comps={report.comps} />
                      </div>
                    )}
                  </div>
                )}
                {openStage >= 4 && (
                  <div className={[styles.scoutBubble, checklist?.ready ? styles.closer : ""].join(" ")}>
                    <Typed
                      text="Here's what you should know before you buy:"
                      instant={!theater}
                      onDone={() => setOpenStage(s0 => Math.max(s0, 5))}
                    />
                    {openStage >= 5 && !checklist && !checklistFailed && (
                      <div className={styles.critWait} aria-hidden>
                        <span /><span /><span />
                      </div>
                    )}
                    {openStage >= 5 && !checklist && checklistFailed && (
                      <p className={styles.critFailed}>
                        Couldn&apos;t put the list together just now. Ask me anything below and
                        I&apos;ll work from my notes.
                      </p>
                    )}
                    {openStage >= 5 && checklist && checklist.items.length > 0 && (
                      <div className={styles.critPanel}>
                        <div className={styles.critHead}>
                          <span>checklist</span>
                          <span className={styles.critCount}>
                            {checklist.items.filter(i => i.status === "satisfied").length} / {checklist.items.length}
                          </span>
                        </div>
                        <div className={styles.critList}>
                          {checklist.items.map((item, i) => (
                            <CritRow
                              key={i}
                              item={item}
                              index={i}
                              theater={theater}
                              onToggle={() => toggleCheck(i, item.status === "satisfied" ? "open" : "satisfied")}
                            />
                          ))}
                        </div>
                      </div>
                    )}
                    {openStage >= 5 && checklist && report.seller_questions.length > 0
                      && checklist.items.some(i => i.status === "open" && i.verify_via === "ask_seller") && (
                      <div className={styles.sendNext}>
                        <span className={styles.sendNextMsg}>&quot;{report.seller_questions[0]}&quot;</span>
                        <CopyBtn text={report.seller_questions[0]} />
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })()}

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
                {m.kind === "rundown" && m.rundown ? (
                  <RundownCard rundown={m.rundown} first={messages[i - 1]?.role !== "assistant"} />
                ) : (
                  <div className={[
                    styles.scoutBubble,
                    messages[i - 1]?.role !== "assistant" ? styles.scoutBubbleFirst : "",
                    m.kind === "closer" ? styles.closer : "",
                  ].join(" ")}>
                    <p className={styles.sectionText}>{m.content}</p>
                    {m.sendNext && (
                      <div className={styles.sendNext}>
                        <span className={styles.sendNextMsg}>&quot;{m.sendNext}&quot;</span>
                        <CopyBtn text={m.sendNext} />
                      </div>
                    )}
                  </div>
                )}
              </div>
            ),
          )}

          {checklist?.ready && !rundownDone && inspection?.status === "ok" && (
            <div className={styles.readyNudge}>
              <div className={styles.readyNudgeText}>
                <span className={styles.readyNudgeTitle}>Everything checked out.</span>
                <span className={styles.readyNudgeSub}>Confirm the buy and take the pickup rundown with you.</span>
              </div>
              <button type="button" className={styles.readyNudgeBtn} disabled={rundownBusy} onClick={fetchRundown}>
                {rundownBusy ? "Putting it together…" : "Confirm purchase"}
              </button>
            </div>
          )}

          {replying && (
            <div className={styles.scoutGroup}>
              <div className={styles.who}><ScoutAvatar small /> scout</div>
              <TypingBubble />
            </div>
          )}

          {concluded && (
            <div className={styles.sysLine}>purchase confirmed · conversation closed · good luck at pickup</div>
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
            disabled={!chatOpen || replying || uploading || pendingImages.length >= 4}
            onClick={() => fileInputRef.current?.click()}
          >
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="5" width="18" height="14" rx="2" /><circle cx="9" cy="10" r="1.6" /><path d="m5.5 19 5.5-5.5 3 3 2.5-2.5 2 2" /></svg>
          </button>
          <input
            className={styles.input}
            placeholder={
              concluded
                ? "Purchase confirmed · this conversation is closed"
                : gone
                  ? "This listing is gone · conversation closed"
                  : inspection?.status === "ok"
                    ? "Ask Scout, or paste what the seller said…"
                    : "Scout needs a successful look before you can chat"
            }
            value={draft}
            disabled={!chatOpen || replying}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") send(); }}
          />
          <button
            className={styles.send}
            onClick={() => send()}
            disabled={!chatOpen || replying || (!draft.trim() && pendingImages.length === 0)}
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

// A criterion pops in as an open circle, then a beat later settles into
// what Scout already knows: the tick snaps in and the text strikes out,
// or the flag lands. Reads as Scout working the list in front of you.
function CritRow({
  item,
  index,
  theater,
  onToggle,
}: {
  item: Checklist["items"][number];
  index: number;
  theater: boolean;
  onToggle: () => void;
}) {
  const [settled, setSettled] = useState(!theater || item.status === "open");
  useEffect(() => {
    if (settled) return;
    const t = setTimeout(() => setSettled(true), 620 + index * 130);
    return () => clearTimeout(t);
  }, [settled, index]);
  const status = settled ? item.status : "open";
  return (
    <button
      type="button"
      className={styles.critRow}
      style={theater ? { animationDelay: `${index * 110}ms` } : { animation: "none" }}
      title={item.status === "satisfied" ? "Mark as not verified" : "Mark as verified yourself"}
      onClick={onToggle}
    >
      <span className={[
        styles.tick,
        status === "satisfied" ? styles.tickOn : status === "flagged" ? styles.tickFlag : "",
        settled && item.status !== "open" ? styles.tickSettle : "",
      ].join(" ")}>
        {status === "satisfied" ? "✓" : status === "flagged" ? "!" : "○"}
      </span>
      <span className={styles.critBody}>
        <span className={status === "satisfied" ? styles.critTextDone : styles.critText}>{item.check}</span>
        {settled && item.evidence && <span className={styles.critEvidence}>{item.evidence}</span>}
      </span>
      {status !== "satisfied" && item.verify_via && item.verify_via !== "confirmed" && (
        <span className={[styles.verifyTag, item.verify_via === "ask_seller" ? styles.tagAsk : styles.tagPickup].join(" ")}>
          {item.verify_via === "ask_seller" ? "ask seller" : "at pickup"}
        </span>
      )}
    </button>
  );
}

function CopyBtn({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className={styles.copyBtn}
      onClick={() => {
        navigator.clipboard?.writeText(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }}
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

function rundownText(r: Rundown): string {
  const lines = [`PICKUP RUNDOWN · ${r.deal}`];
  if (r.verified.length) {
    lines.push("Verified:");
    for (const v of r.verified) lines.push(`  ✓ ${v.check}${v.evidence ? ` (${v.evidence})` : ""}`);
  }
  if (r.at_pickup.length) {
    lines.push("Check at pickup:");
    for (const t of r.at_pickup) lines.push(`  ! ${t}`);
  }
  if (r.walk_if.length) {
    lines.push("Walk if:");
    for (const t of r.walk_if) lines.push(`  ✕ ${t}`);
  }
  lines.push(r.safety);
  return lines.join("\n");
}

function RundownCard({ rundown, first }: { rundown: Rundown; first: boolean }) {
  return (
    <div className={[styles.scoutBubble, first ? styles.scoutBubbleFirst : "", styles.rundownCard].join(" ")}>
      <div className={styles.rundownHead}>
        <span className={styles.rundownLabel}>pickup rundown</span>
        <CopyBtn text={rundownText(rundown)} />
      </div>
      <p className={styles.rundownDeal}>{rundown.deal}</p>
      {rundown.verified.length > 0 && (
        <>
          <span className={styles.rundownSection} style={{ color: "var(--success)" }}>verified</span>
          {rundown.verified.map((v, i) => (
            <div key={i} className={styles.rundownRow}>
              <span className={[styles.tick, styles.tickOn].join(" ")}>✓</span>
              <span className={styles.rundownRowText}>
                {v.check}
                {v.evidence && <i className={styles.rundownEvidence}> · {v.evidence}</i>}
              </span>
            </div>
          ))}
        </>
      )}
      {rundown.at_pickup.length > 0 && (
        <>
          <span className={styles.rundownSection} style={{ color: "var(--amber)" }}>check at pickup</span>
          {rundown.at_pickup.map((t, i) => (
            <div key={i} className={styles.rundownRow}>
              <span className={[styles.tick, styles.tickWarn].join(" ")}>!</span>
              <span className={styles.rundownRowText}>{t}</span>
            </div>
          ))}
        </>
      )}
      {rundown.walk_if.length > 0 && (
        <>
          <span className={styles.rundownSection} style={{ color: "var(--danger)" }}>walk if</span>
          {rundown.walk_if.map((t, i) => (
            <div key={i} className={styles.rundownRow}>
              <span className={[styles.tick, styles.tickFlag].join(" ")}>✕</span>
              <span className={styles.rundownRowText}>{t}</span>
            </div>
          ))}
        </>
      )}
      <p className={styles.rundownSafety}>{rundown.safety}</p>
    </div>
  );
}

function badgeTone(level: string): string {
  if (["fair", "under", "good", "fine"].includes(level)) return styles.badgeGood;
  if (["over", "worn", "likely_scam"].includes(level)) return styles.badgeBad;
  return styles.badgeWarn;
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
