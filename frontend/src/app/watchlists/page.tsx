"use client";

import { Suspense, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useSession } from "next-auth/react";
import { useRouter, useSearchParams } from "next/navigation";
import AgentBuilder from "@/components/AgentBuilder";
import AgentPanel from "@/components/AgentPanel";
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
  playbook: string | null;
  playbook_updated_at: string | null;
  running_hunt_id: number | null;
  first_hunt_done: boolean;
  hunt_queued: boolean;
  last_hunt_at: string | null;
  next_hunt_at: string | null;
}

/** listing_id → alert facts, for score/reason badges on results */
export type AlertIndex = Record<number, { reason: string | null; score: number; created_at: string }>;

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
  const [chatClosing, setChatClosing] = useState(false);

  const [heroClosing, setHeroClosing] = useState(false);

  function openChatAnimated() {
    // With no agents, the empty hero slides away first, then the builder
    // slides in. Its mount animation covers the return trip on close.
    if (!loading && watchlists.length === 0) {
      setHeroClosing(true);
      setTimeout(() => {
        setHeroClosing(false);
        openChat();
      }, 200);
    } else {
      openChat();
    }
  }

  function closeChat() {
    // Slide away, then unmount. Duration matches .cardClosing in the module.
    setChatClosing(true);
    setTimeout(() => {
      setShowChat(false);
      setChatClosing(false);
    }, 240);
  }

  useEffect(() => {
    if (searchParams.get("checkout_cancelled") === "1") {
      setModal({ type: "cancelled", message: "No worries. You can upgrade anytime." });
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

  // While any agent is queued or out sweeping, keep the list fresh so the
  // card moves starting-up -> live -> done on its own. Without this the page
  // fetched once and a new agent sat on "first sweep soon" until a manual
  // reload (2026-08-18). Polling stops as soon as nothing is in flight, so an
  // idle dashboard makes no requests.
  const sweepInFlight = watchlists.some(w => w.running_hunt_id || w.hunt_queued);
  useEffect(() => {
    if (!token || !sweepInFlight) return;
    const id = setInterval(fetchWatchlists, 10_000);
    return () => clearInterval(id);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, sweepInFlight]);

  // Rotating openers: instant (no LLM round-trip for a greeting), varied,
  // and every line stays hand-written. The header already says "scout" —
  // no self-introduction needed.
  const OPENERS = [
    "What are you hunting for today?",
    "What can I find you?",
    "Alright, what are we after?",
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
    toast(`${chatName.trim()} deployed. Scout hunts on its next cycle`, "success");
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
            onClick={() => (showChat ? closeChat() : openChatAnimated())}
          >
            {showChat ? "Cancel" : "+ Deploy new agent"}
          </button>
        </div>

        {atCap && (
          <div className={styles.upgradeBanner}>
            <p>You&apos;ve hit your agent limit. Upgrade to Pro to run up to 5 agents, get email digests, and more.</p>
            <button className={styles.upgradeBtn} onClick={handleUpgrade} disabled={upgrading}>
              {upgrading ? "Redirecting..." : "Upgrade to Pro · $7.99/mo"}
            </button>
          </div>
        )}

        {showChat && (
          <AgentBuilder
            closing={chatClosing}
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
          <div className={styles.list} aria-hidden>
            <div className={styles.skeletonCard} />
            <div className={styles.skeletonCard} style={{ animationDelay: "0.12s" }} />
          </div>
        ) : watchlists.length === 0 && !showChat ? (
          <div className={[styles.emptyHero, heroClosing ? styles.emptyHeroClosing : ""].join(" ")}>
            <span className={styles.emptyGlyph}>
              <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
                <path d="M12 3 L14 12 L12 21 L10 12 Z" />
                <path d="M3 12 L12 10 L21 12 L12 14 Z" />
              </svg>
            </span>
            <span className={styles.emptyTitle}>No agents yet</span>
            <span className={styles.emptySub}>
              Tell Scout what you&apos;re hunting. It searches ten marketplaces
              around the clock and flags anything worth your money.
            </span>
            <button className={styles.emptyCta} onClick={openChatAnimated}>
              Deploy your first agent →
            </button>
          </div>
        ) : (
          <div className={styles.list}>
            {watchlists.map((wl, i) => (
              <div
                key={wl.id}
                className={[styles.listItemIn, wl.id === justCreatedId ? styles.cardPop : ""].join(" ")}
                style={{ animationDelay: `${i * 80}ms` }}
              >
              <AgentPanel
                agent={wl}
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
