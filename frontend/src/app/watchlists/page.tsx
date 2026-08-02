"use client";

import { Suspense, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useSession } from "next-auth/react";
import { useRouter, useSearchParams } from "next/navigation";
import AgentBuilder from "@/components/AgentBuilder";
import { toast } from "@/components/Toast";
import styles from "./page.module.css";

interface WatchlistContext {
  product_query: string;
  max_budget: number | null;
  min_discount_pct: number | null;
  condition: string[];
  brands: string[];
  keywords: string[];
  buyer_profile?: string | null;
}

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

interface Watchlist {
  id: number;
  name: string;
  min_score: number;
  expires_at: string | null;
  context: WatchlistContext | null;
}

interface Listing {
  id: number;
  marketplace: string;
  title: string;
  price: number;
  currency: string;
  url: string;
  image_url: string | null;
  location: string | null;
  condition: string;
  relevance_score: number;
  first_seen_at: string;
  last_seen_at: string;
}

interface ListingsResponse {
  listings: Listing[];
  total_candidates: number;
  reranked: boolean;
}

/** listing_id → alert facts, for score/reason badges on results */
export type AlertIndex = Record<number, { reason: string | null; score: number; created_at: string }>;


function daysUntil(isoString: string): number {
  const ms = new Date(isoString).getTime() - Date.now();
  return Math.max(0, Math.ceil(ms / (1000 * 60 * 60 * 24)));
}

function pct(listed: number, sale: number) {
  return Math.round(((listed - sale) / listed) * 100);
}

function ListingRow({ listing, alert }: { listing: Listing; alert?: AlertIndex[number] }) {
  const score = alert?.score ?? (listing.relevance_score > 0 ? listing.relevance_score : null);
  const isNew = alert !== undefined &&
    Date.now() - new Date(alert.created_at).getTime() < 48 * 3600 * 1000;
  return (
    <div className={styles.dealRow}>
      <div className={styles.dealRowLeft}>
        {score !== null && (
          <span className={styles.dealDiscount}>{Math.round(score * 100)}%</span>
        )}
        <div>
          <p className={styles.dealTitle}>
            {listing.title}
            {isNew && <span className={styles.newTag}>NEW</span>}
          </p>
          <span className={styles.dealSource}>
            {listing.marketplace}
            {listing.location ? ` · ${listing.location}` : ""}
            {listing.condition && listing.condition !== "unknown" ? ` · ${listing.condition}` : ""}
            {alert?.reason ? <span className={styles.reasonInline}> — {alert.reason}</span> : null}
          </span>
        </div>
      </div>
      <div className={styles.dealRowRight}>
        <span className={styles.dealPrice}>
          ${listing.price.toFixed(2)} {listing.currency}
        </span>
        <a
          href={listing.url}
          target="_blank"
          rel="noopener noreferrer"
          className={styles.dealBuyBtn}
        >
          View →
        </a>
      </div>
    </div>
  );
}

function WatchlistCard({
  watchlist,
  onDelete,
  token,
  alertIndex,
}: {
  watchlist: Watchlist;
  onDelete: (id: number) => void;
  token: string | undefined;
  alertIndex: AlertIndex;
}) {
  const [expanded, setExpanded] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [ctx, setCtx] = useState<WatchlistContext | null>(watchlist.context);
  const [patching, setPatching] = useState(false);

  // v14 marketplace listings + hunt trigger
  const [listings, setListings] = useState<Listing[] | null>(null);
  const [showAllListings, setShowAllListings] = useState(false);
  const [listingsMeta, setListingsMeta] = useState<{ total: number; reranked: boolean } | null>(null);
  const [loadingListings, setLoadingListings] = useState(false);

  const days = watchlist.expires_at ? daysUntil(watchlist.expires_at) : null;

  // Scout's notes: the qualitative state the agent hunts with. Editing it
  // PATCHes buyer_profile, which re-embeds the intent vector server-side —
  // telling your friend more literally re-aims the hunt.
  const [editingNotes, setEditingNotes] = useState(false);
  const [notesDraft, setNotesDraft] = useState("");
  const [budgetDraft, setBudgetDraft] = useState("");
  const [conditionDraft, setConditionDraft] = useState<string[]>([]);
  const [brandsDraft, setBrandsDraft] = useState("");

  function openNotesEditor() {
    setNotesDraft(ctx?.buyer_profile ?? "");
    setBudgetDraft(ctx?.max_budget != null ? String(ctx.max_budget) : "");
    setConditionDraft(ctx?.condition ?? []);
    setBrandsDraft((ctx?.brands ?? []).join(", "));
    setEditingNotes(true);
  }

  async function saveNotes() {
    setEditingNotes(false);
    const patch: Partial<WatchlistContext> = {
      // "" not null for profile: the PATCH endpoint treats null as "field
      // not sent" — empty string is the explicit "clear my notes".
      buyer_profile: notesDraft.trim(),
      condition: conditionDraft,
      brands: brandsDraft.split(",").map(b => b.trim()).filter(Boolean),
    };
    const budget = parseFloat(budgetDraft);
    if (!isNaN(budget) && budget > 0) patch.max_budget = budget;
    await patchContext(patch);
  }


  async function patchContext(patch: Partial<WatchlistContext>) {
    if (!token) return;
    setPatching(true);
    try {
      const res = await fetch(`/api/watchlists/${watchlist.id}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify(patch),
      });
      if (res.ok) {
        const data = await res.json();
        setCtx(data.context);
        // Re-aim happens server-side as a background task now; the cache
        // updates a few seconds after the patch. Quiet-refetch twice on a
        // delay — stale results stay on screen until fresh ones land.
        if (listings !== null) {
          setTimeout(() => void loadListings({ quiet: true }), 7000);
          setTimeout(() => void loadListings({ quiet: true }), 16000);
        }
      }
    } finally {
      setPatching(false);
    }
  }

  async function loadListings(opts?: { quiet?: boolean }) {
    if (!opts?.quiet) setLoadingListings(true);
    try {
      const res = await fetch(`/api/watchlists/${watchlist.id}/listings?top_n=20`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      const data: ListingsResponse = await res.json();
      setListings(data.listings ?? []);
      setListingsMeta({
        total: data.total_candidates ?? 0,
        reranked: data.reranked ?? false,
      });
    } catch {
      setListings([]);
      setListingsMeta(null);
    }
    setLoadingListings(false);
  }


  function toggle() {
    if (!expanded) {
      loadListings();
    }
    setExpanded(v => !v);
  }

  const [confirmingDelete, setConfirmingDelete] = useState(false);

  async function handleDelete() {
    setConfirmingDelete(false);
    setDeleting(true);
    await fetch(`/api/watchlists/${watchlist.id}`, {
      method: "DELETE",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
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
          <button className={styles.deleteBtn} onClick={() => setConfirmingDelete(true)} disabled={deleting}>
            {deleting ? "…" : "✕"}
          </button>
        </div>
      </div>

      {ctx?.product_query && (
        <div className={styles.keywords}>
          <span className={styles.keyword}>{ctx.product_query}</span>
        </div>
      )}

      {confirmingDelete && createPortal(
        <div className={styles.modalOverlay} onClick={() => setConfirmingDelete(false)}>
          <div className={styles.modal} onClick={e => e.stopPropagation()}>
            <p className={styles.modalTitle}>Retire &quot;{watchlist.name}&quot;?</p>
            <p className={styles.modalMessage}>
              This agent stops hunting and its setup is gone for good. Alerts
              you already received stay in your feed.
            </p>
            <div className={styles.modalBtnRow}>
              <button className={styles.modalBtnGhost} onClick={() => setConfirmingDelete(false)}>
                Keep it
              </button>
              <button className={styles.modalBtnDanger} onClick={handleDelete}>
                Retire agent
              </button>
            </div>
          </div>
        </div>,
        document.body,
      )}

      {ctx && (
        <div className={styles.notesBlock}>
          <div className={styles.notesHeader}>
            <span className={styles.notesLabel}>scout&apos;s notes</span>
            {patching && <span className={styles.notesReaiming}>re-aiming…</span>}
            {!editingNotes && !patching && (
              <button className={styles.notesEdit} onClick={openNotesEditor}>
                edit
              </button>
            )}
          </div>
          {editingNotes ? (
            <div className={styles.notesEditor}>
              <textarea
                className={styles.notesTextarea}
                value={notesDraft}
                onChange={e => setNotesDraft(e.target.value)}
                rows={3}
                placeholder="Who's this for, and what matters? e.g. WFH full-time, back pain, needs real lumbar support"
                autoFocus
              />
              <div className={styles.notesEditorActions}>
                <button className={styles.notesCancel} onClick={() => setEditingNotes(false)}>
                  Cancel
                </button>
                <button className={styles.notesSave} onClick={saveNotes} disabled={patching}>
                  {patching ? "Saving…" : "Save"}
                </button>
              </div>
            </div>
          ) : (
            <p className={styles.notesText}>
              {ctx.buyer_profile || (
                <span className={styles.notesEmpty}>
                  No notes yet — tell Scout who this is for and it hunts smarter.
                </span>
              )}
            </p>
          )}

          <div className={styles.stateGrid}>
            <div className={styles.stateItem}>
              <span className={styles.stateLabel}>budget</span>
              {editingNotes ? (
                <input
                  className={styles.notesInlineInput}
                  type="number"
                  placeholder="Max $"
                  value={budgetDraft}
                  onChange={e => setBudgetDraft(e.target.value)}
                />
              ) : (
                <span className={styles.stateValue}>
                  {ctx.max_budget != null ? `$${ctx.max_budget}` : "—"}
                </span>
              )}
            </div>
            <div className={styles.stateItem}>
              <span className={styles.stateLabel}>brands</span>
              {editingNotes ? (
                <input
                  className={styles.notesInlineInput}
                  type="text"
                  placeholder="Comma-sep"
                  value={brandsDraft}
                  onChange={e => setBrandsDraft(e.target.value)}
                />
              ) : (
                <span className={styles.stateValue}>
                  {(ctx.brands?.length ?? 0) > 0 ? ctx.brands.join(", ") : "—"}
                </span>
              )}
            </div>
            <div className={styles.stateItem}>
              <span className={styles.stateLabel}>condition</span>
              {editingNotes ? (
                <div className={styles.notesPills}>
                  {(["new", "refurb", "used"] as const).map(c => (
                    <button
                      key={c}
                      type="button"
                      className={[
                        styles.notesPill,
                        conditionDraft.includes(c) ? styles.notesPillActive : "",
                      ].join(" ")}
                      onClick={() =>
                        setConditionDraft(prev =>
                          prev.includes(c) ? prev.filter(x => x !== c) : [...prev, c],
                        )
                      }
                    >
                      {c}
                    </button>
                  ))}
                </div>
              ) : (
                <span className={styles.stateValue}>
                  {(ctx.condition?.length ?? 0) > 0 && (ctx.condition?.length ?? 0) < 3
                    ? ctx.condition.join(" · ")
                    : "Any"}
                </span>
              )}
            </div>
          </div>
        </div>
      )}

      {expanded && (
        <div className={styles.dealsSection}>
          <p className={styles.dealCount}>
            Marketplace listings
            {listingsMeta && (
              <>
                {" — "}
                {listingsMeta.total} candidate{listingsMeta.total !== 1 ? "s" : ""}
                {listingsMeta.reranked ? " (reranked)" : " (unranked)"}
              </>
            )}
          </p>
          {loadingListings && (
            <p className={styles.dealsLoading}>Loading listings…</p>
          )}
          {!loadingListings && listings !== null && listings.length === 0 && (
            <div className={styles.dealsEmpty}>
              <p>Nothing ranked yet — Scout's first pass lands after its next hunt.</p>
            </div>
          )}
          {!loadingListings && listings && listings.length > 0 && (() => {
            const WEAK_SCORE = 0.4;
            const visible = showAllListings ? listings : listings.slice(0, 20);
            const weakStart = visible.findIndex(
              l => l.relevance_score > 0 && l.relevance_score < WEAK_SCORE,
            );
            return (
              <div className={styles.dealsList}>
                {visible.map((l, i) => (
                  <div key={l.id}>
                    {i === weakStart && weakStart > 0 && (
                      <div className={styles.weakDivider}>weaker matches</div>
                    )}
                    <ListingRow listing={l} alert={alertIndex[l.id]} />
                  </div>
                ))}
                {listings.length > 20 && (
                  <button
                    className={styles.showAllBtn}
                    onClick={() => setShowAllListings(v => !v)}
                  >
                    {showAllListings
                      ? "Show fewer"
                      : `Show all ${listings.length}`}
                  </button>
                )}
              </div>
            );
          })()}
        </div>
      )}
    </div>
  );
}

function WatchlistsPageInner() {
  const { data: session } = useSession();
  const token = (session as any)?.accessToken as string | undefined;
  const router = useRouter();
  const searchParams = useSearchParams();

  const [watchlists, setWatchlists] = useState<Watchlist[]>([]);
  const [alertIndex, setAlertIndex] = useState<AlertIndex>({});

  useEffect(() => {
    (async () => {
      try {
        const res = await fetch("/api/alerts?limit=100");
        if (!res.ok) return;
        const data = await res.json();
        const index: AlertIndex = {};
        for (const a of data.alerts ?? []) {
          index[a.listing_id] = { reason: a.reason, score: a.score, created_at: a.created_at };
        }
        setAlertIndex(index);
      } catch {
        /* badges are enhancement only */
      }
    })();
  }, []);
  const [loading, setLoading] = useState(true);

  // Chat state
  const [showChat, setShowChat] = useState(false);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [chatContext, setChatContext] = useState<WatchlistContext | null>(null);
  const [chatInput, setChatInput] = useState("");
  const [chatComplete, setChatComplete] = useState(false);
  const [chatSuggestions, setChatSuggestions] = useState<string[]>([]);
  const [chatName, setChatName] = useState("");
  const [chatLoading, setChatLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [atCap, setAtCap] = useState(false);
  const [upgrading, setUpgrading] = useState(false);
  const [modal, setModal] = useState<{ type: "cancelled" | "error" | "abort"; message: string; title?: string } | null>(null);
  const [justCreatedId, setJustCreatedId] = useState<number | null>(null);

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

  // Rotating openers: instant (no LLM round-trip for a greeting), varied,
  // and every line stays hand-written. The header already says "scout" —
  // no self-introduction needed.
  const OPENERS = [
    "What are you hunting for today?",
    "What can I find you?",
    "Alright — what are we after?",
    "Tell me what you need. I'll find where it's cheap.",
    "What's on the list today?",
    "Give me something to hunt.",
  ];

  function openChat() {
    setShowChat(true);
    setChatMessages([{
      role: "assistant",
      content: OPENERS[Math.floor(Math.random() * OPENERS.length)],
    }]);
    setChatContext(null);
    setChatComplete(false);
    setChatSuggestions([]);
    setChatInput("");
    setChatName("");
    setFormError(null);
  }

  async function sendChatMessage(override?: string) {
    const text = (override ?? chatInput).trim();
    if (!text || chatLoading) return;
    const userMsg: ChatMessage = { role: "user", content: text };
    const newMessages = [...chatMessages, userMsg];
    setChatMessages(newMessages);
    setChatInput("");
    setChatSuggestions([]);
    setChatLoading(true);

    try {
      const res = await fetch("/api/watchlists/chat", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ messages: newMessages, context: chatContext }),
      });
      const data = await res.json();
      if (data.aborted) {
        setShowChat(false);
        setChatMessages([]);
        setChatContext(null);
        setChatComplete(false);
        setChatSuggestions([]);
        setChatInput("");
        setChatName("");
        setModal({
          type: "abort",
          title: "Scout couldn't continue",
          message: data.abort_reason || "Try starting a new agent with a clearer product idea.",
        });
        setChatLoading(false);
        return;
      }
      setChatMessages(prev => [...prev, { role: "assistant", content: data.reply }]);
      setChatContext(data.context);
      setChatSuggestions(Array.isArray(data.suggestions) ? data.suggestions : []);
      if (data.is_complete) setChatComplete(true);
    } catch {
      setChatMessages(prev => [...prev, {
        role: "assistant",
        content: "Connection hiccup. Try that again.",
      }]);
    }
    setChatLoading(false);
  }

  async function handleCreateFromChat() {
    if (!chatContext || !chatName.trim() || !token) return;
    setSubmitting(true);
    setFormError(null);
    const res = await fetch("/api/watchlists", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ name: chatName, context: chatContext }),
    });
    const data = await res.json();
    setSubmitting(false);
    if (!res.ok) {
      if (res.status === 403) setAtCap(true);
      setFormError(data.detail ?? "Failed to create watchlist");
      return;
    }
    setWatchlists(prev => [...prev, data]);
    setJustCreatedId(data.id);
    toast(`${chatName.trim()} deployed — Scout hunts on its next cycle`, "success");
    setShowChat(false);
    setChatMessages([]);
    setChatContext(null);
    setChatComplete(false);
    setChatSuggestions([]);
    setChatName("");
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

      {modal && createPortal(
        <div className={styles.modalOverlay} onClick={() => setModal(null)}>
          <div className={styles.modal} onClick={e => e.stopPropagation()}>
            <div className={modal.type === "cancelled" ? styles.modalIconCancelled : styles.modalIconError}>
              {modal.type === "cancelled" ? "→" : "!"}
            </div>
            <p className={styles.modalTitle}>
              {modal.title ?? (modal.type === "error" ? "Something went wrong" : "Checkout cancelled")}
            </p>
            <p className={styles.modalMessage}>{modal.message}</p>
            <button className={styles.modalBtn} onClick={() => setModal(null)}>Got it</button>
          </div>
        </div>,
        document.body,
      )}

      <main className={styles.main}>
        <div className={styles.header}>
          <h1 className={styles.heading}>My Agents</h1>
          <button
            className={styles.addBtn}
            onClick={() => (showChat ? setShowChat(false) : openChat())}
          >
            {showChat ? "Cancel" : "+ Deploy new agent"}
          </button>
        </div>

        {atCap && (
          <div className={styles.upgradeBanner}>
            <p>You&apos;ve hit your agent limit. Upgrade to Pro to run up to 5 agents, get email digests, and more.</p>
            <button className={styles.upgradeBtn} onClick={handleUpgrade} disabled={upgrading}>
              {upgrading ? "Redirecting..." : "Upgrade to Pro — $7.99/mo"}
            </button>
          </div>
        )}

        {showChat && (
          <AgentBuilder
            context={chatContext}
            messages={chatMessages}
            suggestions={chatSuggestions}
            isLoading={chatLoading}
            isComplete={chatComplete}
            input={chatInput}
            onInputChange={setChatInput}
            onSend={sendChatMessage}
            name={chatName}
            onNameChange={setChatName}
            onDeploy={handleCreateFromChat}
            submitting={submitting}
            formError={formError}
          />
        )}

        {loading ? (
          <div className={styles.empty}>Loading...</div>
        ) : watchlists.length === 0 ? (
          <div className={styles.empty}>No agents deployed yet — deploy one and it&apos;ll start scanning immediately.</div>
        ) : (
          <div className={styles.list}>
            {watchlists.map(wl => (
              <div key={wl.id} className={wl.id === justCreatedId ? styles.cardPop : undefined}>
              <WatchlistCard
                watchlist={wl}
                token={token}
                onDelete={id => setWatchlists(prev => prev.filter(w => w.id !== id))}
                alertIndex={alertIndex}
              />
              </div>
            ))}
          </div>
        )}
      </main>
    </>
  );
}

export default function WatchlistsPage() {
  return (
    <Suspense>
      <WatchlistsPageInner />
    </Suspense>
  );
}
