"use client";

import { Suspense, useEffect, useState } from "react";
import { useSession } from "next-auth/react";
import { useRouter, useSearchParams } from "next/navigation";
import Nav from "@/components/Nav";
import AgentBuilder from "@/components/AgentBuilder";
import styles from "./page.module.css";

interface WatchlistContext {
  product_query: string;
  max_budget: number | null;
  min_discount_pct: number | null;
  condition: string[];
  brands: string[];
  keywords: string[];
}

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

interface Watchlist {
  id: number;
  name: string;
  keywords: string[];
  min_score: number;
  alert_tier_threshold: string;
  expires_at: string | null;
  context: WatchlistContext | null;
}

interface Deal {
  id: number;
  title: string;
  source: string;
  url: string | null;
  affiliate_url: string | null;
  listed_price: number;
  sale_price: number;
  score: number;
  alert_tier: string;
  category: string;
  real_discount_pct: number | null;
  student_eligible: boolean;
  condition: string;
}

const TIER_LABELS: Record<string, string> = { push: "Hot", digest: "Good", none: "Mild" };
const TIER_CLASS: Record<string, string> = {
  push: styles.tierPush,
  digest: styles.tierDigest,
  none: styles.tierNone,
};

function daysUntil(isoString: string): number {
  const ms = new Date(isoString).getTime() - Date.now();
  return Math.max(0, Math.ceil(ms / (1000 * 60 * 60 * 24)));
}

function pct(listed: number, sale: number) {
  return Math.round(((listed - sale) / listed) * 100);
}

function DealRow({ deal }: { deal: Deal }) {
  const discount = deal.real_discount_pct ?? pct(deal.listed_price, deal.sale_price);
  const buyUrl = deal.affiliate_url || deal.url;
  return (
    <div className={styles.dealRow}>
      <div className={styles.dealRowLeft}>
        <span className={styles.dealDiscount}>−{discount}%</span>
        <div>
          <p className={styles.dealTitle}>{deal.title}</p>
          <span className={styles.dealSource}>{deal.source} · {deal.category}</span>
        </div>
      </div>
      <div className={styles.dealRowRight}>
        <span className={styles.dealPrice}>${deal.sale_price.toFixed(2)}</span>
        <span className={[styles.dealTier, TIER_CLASS[deal.alert_tier] ?? ""].join(" ")}>
          {TIER_LABELS[deal.alert_tier] ?? deal.alert_tier}
        </span>
        {buyUrl && (
          <a href={buyUrl} target="_blank" rel="noopener noreferrer" className={styles.dealBuyBtn}>
            Buy →
          </a>
        )}
      </div>
    </div>
  );
}

function WatchlistCard({
  watchlist,
  onDelete,
  token,
  onNewWatchlist,
}: {
  watchlist: Watchlist;
  onDelete: (id: number) => void;
  token: string | undefined;
  onNewWatchlist: () => void;
}) {
  const [deals, setDeals] = useState<Deal[] | null>(null);
  const [dealCount, setDealCount] = useState<number | null>(null);
  const [usedFallback, setUsedFallback] = useState(false);
  const [loadingDeals, setLoadingDeals] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [ctx, setCtx] = useState<WatchlistContext | null>(watchlist.context);
  const [patching, setPatching] = useState(false);

  const days = watchlist.expires_at ? daysUntil(watchlist.expires_at) : null;

  async function loadDeals() {
    setLoadingDeals(true);
    try {
      const res = await fetch(`/api/watchlists/${watchlist.id}/deals`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      const data = await res.json();
      const dealList: Deal[] = Array.isArray(data) ? data : (data.deals ?? []);
      setDeals(dealList);
      setDealCount(dealList.length);
      setUsedFallback(data.filtered === false);
    } catch {
      setDeals([]);
    }
    setLoadingDeals(false);
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
        setDeals(null);
        setDealCount(null);
        if (expanded) loadDeals();
      }
    } finally {
      setPatching(false);
    }
  }

  function toggle() {
    if (!expanded) loadDeals();
    setExpanded(v => !v);
  }

  async function handleDelete() {
    if (!confirm(`Delete "${watchlist.name}"?`)) return;
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
          <button className={styles.deleteBtn} onClick={handleDelete} disabled={deleting}>
            {deleting ? "…" : "✕"}
          </button>
        </div>
      </div>

      <div className={styles.keywords}>
        {watchlist.keywords.map(kw => (
          <span key={kw} className={styles.keyword}>{kw}</span>
        ))}
      </div>

      {ctx && (
        <div className={styles.filterControls}>
          <div className={styles.filterRow}>
            <label className={styles.filterLabel}>Budget</label>
            <input
              className={styles.filterInput}
              type="number"
              placeholder="Max $"
              defaultValue={ctx.max_budget ?? ""}
              disabled={patching}
              onBlur={e => {
                const val = parseFloat(e.target.value);
                if (!isNaN(val) && val !== ctx.max_budget) patchContext({ max_budget: val });
                if (!e.target.value) patchContext({ max_budget: null });
              }}
            />
          </div>
          <div className={styles.filterRow}>
            <label className={styles.filterLabel}>Min discount</label>
            <input
              className={styles.filterInput}
              type="number"
              placeholder="% off"
              defaultValue={ctx.min_discount_pct ?? ""}
              disabled={patching}
              onBlur={e => {
                const val = parseInt(e.target.value);
                if (!isNaN(val) && val !== ctx.min_discount_pct) patchContext({ min_discount_pct: val });
                if (!e.target.value) patchContext({ min_discount_pct: null });
              }}
            />
          </div>
          <div className={styles.filterRow}>
            <label className={styles.filterLabel}>Condition</label>
            <div className={styles.conditionPills}>
              {(["new", "refurb", "used"] as const).map(c => (
                <button
                  key={c}
                  disabled={patching}
                  className={[
                    styles.pill,
                    (ctx.condition.length === 0 || ctx.condition.includes(c)) ? styles.pillActive : "",
                  ].join(" ")}
                  onClick={() => {
                    const current = ctx.condition;
                    const next = current.includes(c)
                      ? current.filter(x => x !== c)
                      : [...current, c];
                    patchContext({ condition: next });
                  }}
                >
                  {c.charAt(0).toUpperCase() + c.slice(1)}
                </button>
              ))}
            </div>
          </div>
          {dealCount !== null && (
            <p className={styles.dealCount}>
              {usedFallback
                ? "No exact matches — showing closest deals"
                : `${dealCount} deal${dealCount !== 1 ? "s" : ""} match your filters`}
            </p>
          )}
        </div>
      )}

      {expanded && (
        <div className={styles.dealsSection}>
          {loadingDeals && <p className={styles.dealsLoading}>Finding matches…</p>}
          {!loadingDeals && deals !== null && deals.length === 0 && (
            <div className={styles.dealsEmpty}>
              <p>Our agents are still scanning — nothing surfaced yet.</p>
              <button className={styles.dealsEmptyLink} onClick={onNewWatchlist}>
                Deploy an AI agent to find it for you →
              </button>
            </div>
          )}
          {!loadingDeals && deals && deals.length > 0 && (
            <div className={styles.dealsList}>
              {deals.map(d => <DealRow key={d.id} deal={d} />)}
            </div>
          )}
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

  function openChat() {
    setShowChat(true);
    setChatMessages([{
      role: "assistant",
      content: "I'm Scout. What are we hunting today?",
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
              <WatchlistCard
                key={wl.id}
                watchlist={wl}
                token={token}
                onDelete={id => setWatchlists(prev => prev.filter(w => w.id !== id))}
                onNewWatchlist={openChat}
              />
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
