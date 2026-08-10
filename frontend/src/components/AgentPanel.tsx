"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import AgentCard, { HuntSummary } from "./AgentCard";
import InspectorPanel, { InspectListing } from "./InspectorPanel";
import { MARKETPLACE_LABELS } from "./PoolCard";
import styles from "./AgentPanel.module.css";

// The agent card v2 (redesign spec 2026-08-09): a decision surface, not a
// dashboard. Top picks with Scout's cached reads lead; market analysis,
// negotiation playbook, and the full match hierarchy sit behind tabs; the
// sweep pill carries live state; config lives behind the edit modal.

export interface AgentContext {
  product_query: string;
  max_budget?: number | null;
  condition?: string[];
  brands?: string[];
  buyer_profile?: string | null;
}

export interface Agent {
  id: number;
  name: string;
  context: AgentContext | null;
  playbook: string | null;
  running_hunt_id: number | null;
  last_hunt_at: string | null;
  next_hunt_at: string | null;
}

export interface RankedListing {
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
  reason: string | null;
  headline: string | null;
  first_seen_at: string;
  last_seen_at: string;
}

interface Market {
  n_live: number;
  typical: number | null;
  band: { p25: number; median: number; p75: number } | null;
  within_budget: number | null;
  ceiling: number | null;
  newest_find_hours: number | null;
  histogram: { lo: number; hi: number; count: number }[];
  pick_prices: { id: number; price: number; over_ceiling: boolean }[];
  structure: { kind: string; rows: { label: string; avg_price: number; count: number }[] };
  heat: { level: string; label: string; why: string };
  negotiation: { open: number; fair_low: number; fair_high: number; walk: number; median: number } | null;
  going_rate_prose: string | null;
}

interface SweepListing {
  id: number;
  title: string;
  price: number;
  currency: string;
  marketplace: string;
  url: string;
  image_url: string | null;
  matched: boolean;
}

type AlertIndex = Record<number, { reason: string | null; score: number; created_at: string }>;
type Tab = "picks" | "market" | "neg" | "all";

const WEAK = 0.4;
const MEETING_UP =
  "Meet somewhere public and test before you pay. Cash or e-transfer in person; never send a deposit to hold an item.";

function ScoutGlyph({ size = 14 }: { size?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
      <path d="M12 3 L14 12 L12 21 L10 12 Z" />
      <path d="M3 12 L12 10 L21 12 L12 14 Z" />
    </svg>
  );
}

// Same typing treatment Scout has in the builder chat: interval-sliced text
// with a blinking cursor until done. lead types first, then the dim tail.
function useTypewriter(text: string, speed = 14, delay = 420) {
  const [displayed, setDisplayed] = useState("");
  const [done, setDone] = useState(false);
  useEffect(() => {
    setDisplayed("");
    setDone(false);
    if (!text) {
      setDone(true);
      return;
    }
    let interval: ReturnType<typeof setInterval> | null = null;
    // Breathe first, then speak: the pause lets the view settle in and the
    // avatar pulse read as "thinking" before the line types out.
    const starter = setTimeout(() => {
      let i = 0;
      interval = setInterval(() => {
        i++;
        setDisplayed(text.slice(0, i));
        if (i >= text.length) {
          if (interval) clearInterval(interval);
          setDone(true);
        }
      }, speed);
    }, delay);
    return () => {
      clearTimeout(starter);
      if (interval) clearInterval(interval);
    };
  }, [text, speed, delay]);
  return { displayed, done };
}

function SayLine({ lead, dim }: { lead: string; dim?: string }) {
  const full = dim ? `${lead}${dim}` : lead;
  const { displayed, done } = useTypewriter(full);
  const leadShown = displayed.slice(0, lead.length);
  const dimShown = displayed.length > lead.length ? displayed.slice(lead.length) : "";
  return (
    <div className={styles.say}>
      <span className={[styles.avatar, styles.avatarActive].join(" ")}>
        <ScoutGlyph />
      </span>
      <p className={styles.sentence}>
        {leadShown}
        {dimShown && <span className={styles.dim}>{dimShown}</span>}
        {!done && <span className={styles.cursor}>▍</span>}
      </p>
    </div>
  );
}

interface Takeaway { text: string; kind: string }

const TAKEAWAY_ICONS: Record<string, React.ReactNode> = {
  price: (
    <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><path d="M12 2v20M17 6.5c0-2-2.2-3-5-3s-5 1-5 3 2 2.8 5 3.5 5 1.6 5 3.5-2.2 3-5 3-5-1-5-3" /></svg>
  ),
  warning: (
    <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 3 2 20h20Z" /><path d="M12 10v4M12 17.5v.1" /></svg>
  ),
  tip: (
    <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><path d="M12 3 L14 12 L12 21 L10 12 Z" /><path d="M3 12 L12 10 L21 12 L12 14 Z" /></svg>
  ),
  timing: (
    <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3.5 2" /></svg>
  ),
};

function TakeawayBoxes({ takeaways }: { takeaways: Takeaway[] }) {
  if (!takeaways.length) return null;
  return (
    <div className={styles.takeaways}>
      {takeaways.map((t, i) => (
        <span
          key={i}
          className={[styles.takeaway, styles[`tk_${t.kind}`] ?? ""].join(" ")}
          style={{ animationDelay: `${i * 90}ms` }}
        >
          <span className={styles.takeawayIcon}>{TAKEAWAY_ICONS[t.kind] ?? TAKEAWAY_ICONS.tip}</span>
          {t.text}
        </span>
      ))}
    </div>
  );
}

function TypedReply({ text, takeaways }: { text: string; takeaways: Takeaway[] }) {
  const { displayed, done } = useTypewriter(text, 12, 150);
  return (
    <>
      <p className={styles.msgText}>
        {displayed}
        {!done && <span className={styles.cursor}>▍</span>}
      </p>
      {done && <TakeawayBoxes takeaways={takeaways} />}
    </>
  );
}

function priceChip(price: number, median: number | null): { tone: string; text: string } | null {
  if (median == null) return null;
  const delta = price - median;
  if (Math.abs(delta) <= 0.12 * median) return { tone: "fair", text: "fair for the market" };
  if (delta > 0) return { tone: "over", text: `$${Math.round(delta)} over rate` };
  return { tone: "under", text: `$${Math.round(-delta)} under rate` };
}

function hoursAgo(iso: string | null): string {
  if (!iso) return "";
  const h = Math.max(0, (Date.now() - new Date(iso).getTime()) / 3_600_000);
  if (h < 1) return `${Math.max(1, Math.round(h * 60))}m`;
  if (h < 48) return `${Math.round(h)}h`;
  return `${Math.round(h / 24)}d`;
}

function hoursUntil(iso: string | null): string | null {
  if (!iso) return null;
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return "any minute";
  const h = Math.floor(ms / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function firstSentences(text: string, n = 2): string {
  const parts = text.match(/[^.!?]+[.!?]+/g) ?? [text];
  return parts.slice(0, n).join(" ").trim();
}

function playbookSection(playbook: string | null, heading: string, next: string | null): string | null {
  if (!playbook) return null;
  const clean = playbook.replace(/\*\*/g, "");
  const i = clean.indexOf(heading);
  if (i === -1) return null;
  const start = i + heading.length;
  const end = next && clean.indexOf(next, start) !== -1 ? clean.indexOf(next, start) : clean.length;
  const text = clean.slice(start, end).trim();
  return text || null;
}

export default function AgentPanel({
  agent,
  token,
  alertIndex,
  onDelete,
}: {
  agent: Agent;
  token: string | undefined;
  alertIndex: AlertIndex;
  onDelete: (id: number) => void;
}) {
  const [tab, setTab] = useState<Tab>("picks");
  const [listings, setListings] = useState<RankedListing[] | null>(null);
  const [market, setMarket] = useState<Market | null>(null);
  const [sweep, setSweep] = useState<SweepListing[] | null>(null);
  const [inspecting, setInspecting] = useState<InspectListing | null>(null);
  const [showSweepModal, setShowSweepModal] = useState(false);
  const [showEdit, setShowEdit] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [expanded, setExpanded] = useState<boolean | null>(null); // null until newCount known

  // Sliding tab indicator: measured from the active tab button so it glides
  // (left/width transition) instead of snapping between tabs.
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const [indicator, setIndicator] = useState<{ left: number; width: number } | null>(null);
  useEffect(() => {
    const el = tabRefs.current[tab];
    if (el) setIndicator({ left: el.offsetLeft, width: el.offsetWidth });
  }, [tab, expanded]);
  useEffect(() => {
    const onResize = () => {
      const el = tabRefs.current[tab];
      if (el) setIndicator({ left: el.offsetLeft, width: el.offsetWidth });
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [tab]);

  const authHeaders: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};

  const loadListings = useCallback(async () => {
    try {
      const res = await fetch(`/api/watchlists/${agent.id}/listings?top_n=40`, { headers: authHeaders });
      if (!res.ok) return;
      const data = await res.json();
      setListings(data.listings ?? []);
    } catch {
      setListings([]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent.id, token]);

  useEffect(() => {
    loadListings();
  }, [loadListings]);

  // Market data powers two tabs; fetch once when either opens.
  useEffect(() => {
    if ((tab !== "market" && tab !== "neg") || market) return;
    (async () => {
      try {
        const res = await fetch(`/api/watchlists/${agent.id}/market`, { headers: authHeaders });
        if (res.ok) setMarket(await res.json());
      } catch {
        /* tab shows its loading line until a retry */
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, market, agent.id]);

  const loadSweep = useCallback(async () => {
    if (sweep) return;
    try {
      const hunts = await fetch(`/api/watchlists/${agent.id}/hunts`, { headers: authHeaders }).then(r => (r.ok ? r.json() : null));
      const latest = hunts?.hunts?.[0]?.id;
      if (!latest) {
        setSweep([]);
        return;
      }
      const res = await fetch(`/api/hunts/${latest}/listings`);
      if (res.ok) {
        const data = await res.json();
        setSweep(data.listings ?? []);
      }
    } catch {
      setSweep([]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent.id, sweep, token]);

  const ranked = (listings ?? []).filter(l => l.relevance_score >= WEAK);
  const weak = (listings ?? []).filter(l => l.relevance_score < WEAK);
  const picks = ranked.slice(0, 5);
  const hero = picks[0];
  const minis = picks.slice(1);
  const rest = ranked.slice(5);
  const median = market?.negotiation?.median ?? market?.typical ?? null;

  const isNew = (id: number) =>
    alertIndex[id] !== undefined &&
    Date.now() - new Date(alertIndex[id].created_at).getTime() < 48 * 3600 * 1000;
  const newCount = (listings ?? []).filter(l => isNew(l.id)).length;

  useEffect(() => {
    if (listings !== null && expanded === null) {
      setExpanded(newCount > 0 || agent.running_hunt_id !== null);
    }
  }, [listings, expanded, newCount, agent.running_hunt_id]);

  async function handleDelete() {
    setConfirmingDelete(false);
    setDeleting(true);
    await fetch(`/api/watchlists/${agent.id}`, { method: "DELETE", headers: authHeaders });
    onDelete(agent.id);
  }

  const sweepPill = agent.running_hunt_id ? (
    <button className={[styles.sweepPill, styles.live].join(" ")} onClick={() => setShowSweepModal(true)}>
      <span className={styles.liveDot} />Agent is live · watch <span className={styles.up}>↗</span>
    </button>
  ) : (
    <span className={[styles.sweepPill, styles.quiet].join(" ")}>
      {agent.last_hunt_at ? `last sweep ${hoursAgo(agent.last_hunt_at)}` : "first sweep soon"}
      {hoursUntil(agent.next_hunt_at) ? ` · next in ${hoursUntil(agent.next_hunt_at)}` : ""}
    </span>
  );

  // ---- collapsed idle row ----
  if (expanded === false) {
    return (
      <div className={styles.idleCard} onClick={() => setExpanded(true)} role="button" tabIndex={0}
           onKeyDown={e => { if (e.key === "Enter") setExpanded(true); }}>
        <span className={styles.avatar}><ScoutGlyph /></span>
        <span className={styles.idleText}>
          <b>{agent.name}</b> · nothing new worth your time.
          {ranked.length > 0 ? ` Your ${Math.min(5, ranked.length)} picks are still live.` : " I'll keep looking."}
        </span>
        <span className={styles.idleMeta}>{sweepPill}</span>
      </div>
    );
  }

  return (
    <div className={styles.card}>
      <div className={styles.top}>
        <span className={styles.name}>{agent.name}</span>
        {newCount > 0 && <span className={[styles.chip, styles.chipGood].join(" ")}>{newCount} new</span>}
        <div className={styles.topRight}>
          {sweepPill}
          <button className={styles.iconBtn} title="Edit what Scout hunts for" onClick={() => setShowEdit(true)}>edit</button>
          <button className={styles.iconBtn} title="Retire agent" onClick={() => setConfirmingDelete(true)} disabled={deleting}>
            {deleting ? "…" : "✕"}
          </button>
        </div>
      </div>

      <div className={styles.tabs} role="tablist">
        {([
          ["picks", "Top picks"],
          ["market", "Market analysis"],
          ["neg", "Negotiation playbook"],
          ["all", "All matches"],
        ] as [Tab, string][]).map(([key, label]) => (
          <button
            key={key}
            ref={el => { tabRefs.current[key] = el; }}
            role="tab"
            className={[styles.tab, tab === key ? styles.tabActive : ""].join(" ")}
            onClick={() => setTab(key)}
          >
            {label}
            {key === "all" && ranked.length > 5 && <span className={styles.tabCount}> {rest.length + weak.length}</span>}
          </button>
        ))}
        {indicator && (
          <span
            className={styles.tabIndicator}
            style={{ left: indicator.left, width: indicator.width }}
            aria-hidden
          />
        )}
      </div>

      {/* ================= TOP PICKS ================= */}
      {tab === "picks" && (
        <div className={styles.view}>
          {listings === null && <p className={styles.loading}>Loading Scout's picks…</p>}
          {listings !== null && picks.length === 0 && (
            <SayLine
              lead="Nothing has cleared your bar yet. "
              dim={agent.running_hunt_id ? "I'm out looking right now." : "My next sweep will restock this."}
            />
          )}

          {hero && (
            <>
              <SayLine
                lead={picks.some(p => p.headline)
                  ? "I already took a look at your top picks. "
                  : "Your top picks. "}
                dim="This one first:"
              />

              <div className={styles.hero}>
                {hero.image_url ? (
                  <img src={hero.image_url} alt="" loading="lazy" referrerPolicy="no-referrer" className={styles.heroThumb} />
                ) : (
                  <span className={[styles.heroThumb, styles.thumbEmpty].join(" ")} aria-hidden><ScoutGlyph size={22} /></span>
                )}
                <div className={styles.heroBody}>
                  <div className={styles.heroTop}>
                    <span className={styles.heroTitle}>{hero.title}</span>
                    {isNew(hero.id) && <span className={[styles.chip, styles.chipGood].join(" ")}>new</span>}
                    {(() => {
                      const chip = priceChip(hero.price, median);
                      return chip ? <span className={[styles.chip, styles[chip.tone]].join(" ")}>{chip.text}</span> : null;
                    })()}
                  </div>
                  {(hero.headline || hero.reason) && (
                    <p className={styles.heroWhy}>✦ {hero.headline ?? hero.reason}</p>
                  )}
                  <div className={styles.heroActions}>
                    <span className={styles.heroPrice}>${hero.price.toFixed(0)}</span>
                    <button className={styles.cta} onClick={() => setInspecting(hero)}>Ask Scout about it</button>
                    <a className={styles.ghost} href={hero.url} target="_blank" rel="noopener noreferrer">View listing →</a>
                  </div>
                </div>
              </div>

              {minis.length > 0 && (
                <div className={styles.miniRow}>
                  {minis.map((l, i) => (
                    <div key={l.id} className={styles.mini} style={{ animationDelay: `${140 + i * 55}ms` }}>
                      {l.image_url ? (
                        <img src={l.image_url} alt="" loading="lazy" referrerPolicy="no-referrer" className={styles.miniThumb} />
                      ) : (
                        <span className={[styles.miniThumb, styles.thumbEmpty].join(" ")} aria-hidden><ScoutGlyph /></span>
                      )}
                      <div className={styles.miniBody}>
                        <span className={styles.miniTitle}>{l.title}</span>
                        {(l.headline || l.reason) && (
                          <span className={styles.miniTeaser}>{l.headline ?? l.reason}</span>
                        )}
                        <div className={styles.miniFoot}>
                          <span className={styles.miniPrice}>${l.price.toFixed(0)}</span>
                          <a className={styles.viewBtn} href={l.url} target="_blank" rel="noopener noreferrer">View ↗</a>
                          <button className={styles.askBtn} onClick={() => setInspecting(l)}>Ask Scout</button>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      )}

      {/* ================= MARKET ANALYSIS ================= */}
      {tab === "market" && (
        <div className={styles.view}>
          {!market && <p className={styles.loading}>Reading the market…</p>}
          {market && (
            <>
              {market.typical != null ? (
                <SayLine
                  lead="Here's this market right now."
                  dim={market.within_budget != null && market.ceiling != null
                    ? ` Your $${market.ceiling.toFixed(0)} budget covers most of it.`
                    : undefined}
                />
              ) : (
                <SayLine lead="Still a thin market. " dim="I'll know more with every sweep." />
              )}

              <div className={styles.maGrid}>
              <div className={styles.maLeft}>
              <div className={styles.maStats}>
                <div className={styles.maStat}><span className={styles.maNum}>{market.n_live}</span><span className={styles.maLabel}>live now</span></div>
                {market.typical != null && (
                  <div className={styles.maStat}><span className={styles.maNum}>${market.typical}</span><span className={styles.maLabel}>typical price</span></div>
                )}
                {market.within_budget != null && (
                  <div className={styles.maStat}><span className={[styles.maNum, styles.goodText].join(" ")}>{market.within_budget} of {market.n_live}</span><span className={styles.maLabel}>within your budget</span></div>
                )}
                {market.newest_find_hours != null && (
                  <div className={styles.maStat}><span className={styles.maNum}>{market.newest_find_hours < 48 ? `${Math.max(1, Math.round(market.newest_find_hours))}h` : `${Math.round(market.newest_find_hours / 24)}d`}</span><span className={styles.maLabel}>newest find</span></div>
                )}
              </div>

              <div className={styles.heatRow}>
                <span className={[styles.chip, market.heat.level === "good" ? styles.chipGood : market.heat.level === "warn" ? styles.chipWarn : styles.chipPlain].join(" ")}>
                  {market.heat.label}
                </span>
                <span className={styles.heatWhy}>{market.heat.why}</span>
              </div>

              {market.going_rate_prose && <p className={styles.prose}>{market.going_rate_prose}</p>}
              </div>

              <div className={styles.maRight}>
              {market.histogram.length > 0 && (
                <div className={styles.histoBox}>
                  <span className={styles.monoLabel}>where the {market.n_live} live listings sit · your picks ●</span>
                  <div className={styles.histo}>
                    {(() => {
                      const max = Math.max(...market.histogram.map(b => b.count), 1);
                      return market.histogram.map((b, i) => {
                        const dots = market.pick_prices.filter(p => p.price >= b.lo && (p.price < b.hi || i === market.histogram.length - 1));
                        const ceilingHere = market.ceiling != null && market.ceiling >= b.lo && market.ceiling < b.hi;
                        return (
                          <div key={i} className={[styles.hCol, ceilingHere ? styles.hCeiling : ""].join(" ")}
                               style={{ height: `${Math.max(8, (b.count / max) * 100)}%` }}>
                            {dots.slice(0, 3).map(d => (
                              <span key={d.id} className={[styles.hDot, d.over_ceiling ? styles.hDotOver : ""].join(" ")} />
                            ))}
                          </div>
                        );
                      });
                    })()}
                  </div>
                  <div className={styles.histoLabels}>
                    <span>${Math.round(market.histogram[0].lo)}</span>
                    {market.typical != null && <span className={styles.accText}>typical ${market.typical}</span>}
                    {market.ceiling != null && <span className={styles.ambText}>your max ${market.ceiling.toFixed(0)}</span>}
                    <span>${Math.round(market.histogram[market.histogram.length - 1].hi)}</span>
                  </div>
                </div>
              )}

              {market.structure.rows.length > 0 && (
                <div className={styles.split}>
                  <span className={styles.monoLabel}>
                    price structure · {market.structure.kind === "condition" ? "by condition"
                      : market.structure.kind === "marketplace" ? "by marketplace" : "spread"}
                  </span>
                  {(() => {
                    const top = Math.max(...market.structure.rows.map(r => r.avg_price), 1);
                    return market.structure.rows.map(r => (
                      <div key={r.label} className={styles.splitRow}>
                        <span className={styles.splitName}>{MARKETPLACE_LABELS[r.label] ?? r.label}</span>
                        <div className={styles.splitBar}><div className={styles.splitFill} style={{ width: `${(r.avg_price / top) * 100}%` }} /></div>
                        <span className={styles.splitVal}>${r.avg_price} avg</span>
                      </div>
                    ));
                  })()}
                </div>
              )}
              </div>
              </div>
            </>
          )}
        </div>
      )}

      {/* ================= NEGOTIATION PLAYBOOK ================= */}
      {tab === "neg" && (
        <NegotiationView
          agent={agent}
          market={market}
          authHeaders={authHeaders}
          listings={ranked}
          weakListings={weak}
          sweep={sweep}
          ensureSweep={loadSweep}
          onInspect={setInspecting}
        />
      )}

      {/* ================= ALL MATCHES ================= */}
      {tab === "all" && (
        <div className={styles.view}>
          {rest.length > 0
            ? <SayLine lead={`${rest.length} more cleared your bar. `} dim="Beyond your top five:" />
            : <SayLine lead="Everything that cleared your bar is in your top picks. " dim="The deeper layers:" />}

          {rest.length > 0 && (
            <div className={styles.miniRow}>
              {rest.map((l, i) => (
                <div key={l.id} className={styles.mini} style={{ animationDelay: `${70 + Math.min(i, 12) * 45}ms` }}>
                  {l.image_url ? (
                    <img src={l.image_url} alt="" loading="lazy" referrerPolicy="no-referrer" className={styles.miniThumb} />
                  ) : (
                    <span className={[styles.miniThumb, styles.thumbEmpty].join(" ")} aria-hidden><ScoutGlyph /></span>
                  )}
                  <div className={styles.miniBody}>
                    <span className={styles.miniTitle}>{l.title}</span>
                    <span className={styles.miniMeta}>
                      {MARKETPLACE_LABELS[l.marketplace] ?? l.marketplace}
                      {l.location ? ` · ${l.location}` : ""}
                      {(() => {
                        const chip = priceChip(l.price, median);
                        return chip ? <span className={[styles.chip, styles[chip.tone], styles.chipTight].join(" ")}>{chip.text}</span> : null;
                      })()}
                    </span>
                    <div className={styles.miniFoot}>
                      <span className={styles.miniPrice}>${l.price.toFixed(0)}</span>
                      <a className={styles.viewBtn} href={l.url} target="_blank" rel="noopener noreferrer">View ↗</a>
                      <button className={styles.askBtn} onClick={() => setInspecting(l)}>Ask Scout</button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}

          {weak.length > 0 && (
            <details className={styles.layer}>
              <summary><span className={styles.twist}>▶</span> Weaker matches <span className={styles.tabCount}>{weak.length}</span> <span className={styles.layerHint}>probably not it, but Scout kept them just in case</span></summary>
              <div className={styles.denseList}>
                {weak.map(l => (
                  <div key={l.id} className={styles.denseRow}>
                    <span className={styles.denseTitle}>{l.title}</span>
                    <span className={styles.denseMeta}>{MARKETPLACE_LABELS[l.marketplace] ?? l.marketplace}</span>
                    <span className={styles.densePrice}>${l.price.toFixed(0)}</span>
                    <a className={styles.viewBtn} href={l.url} target="_blank" rel="noopener noreferrer">View ↗</a>
                  </div>
                ))}
              </div>
            </details>
          )}

          <details className={styles.layer} onToggle={e => { if ((e.target as HTMLDetailsElement).open) loadSweep(); }}>
            <summary><span className={styles.twist}>▶</span> Everything from the last sweep{sweep ? <span className={styles.tabCount}> {sweep.length}</span> : null} <span className={styles.layerHint}>every unique listing the agent reviewed, filtered or not</span></summary>
            <div className={styles.denseList}>
              {sweep === null && <p className={styles.loading}>Pulling the sweep…</p>}
              {sweep !== null && sweep.length === 0 && <p className={styles.loading}>No sweep on record yet.</p>}
              {(sweep ?? []).filter(l => !l.matched).map(l => (
                <div key={l.id} className={styles.denseRow}>
                  <span className={styles.denseTitle}>{l.title}</span>
                  <span className={styles.denseMeta}>{MARKETPLACE_LABELS[l.marketplace] ?? l.marketplace}</span>
                  <span className={styles.densePrice}>${l.price.toFixed(0)}</span>
                  <a className={styles.viewBtn} href={l.url} target="_blank" rel="noopener noreferrer">View ↗</a>
                </div>
              ))}
            </div>
          </details>
        </div>
      )}

      <div className={styles.trustLine}>
        {agent.last_hunt_at
          ? <span>swept {hoursAgo(agent.last_hunt_at)} ago{listings !== null ? ` · ${ranked.length + weak.length} kept for you` : ""}</span>
          : <span>first sweep dispatching</span>}
      </div>

      {inspecting && (
        <InspectorPanel
          listing={inspecting}
          watchlistId={agent.id}
          onClose={() => setInspecting(null)}
        />
      )}

      {showSweepModal && agent.running_hunt_id && (
        <SweepModal huntId={agent.running_hunt_id} onClose={() => setShowSweepModal(false)} />
      )}

      {showEdit && (
        <EditBriefModal agent={agent} authHeaders={authHeaders} onClose={() => setShowEdit(false)} />
      )}

      {confirmingDelete && createPortal(
        <div className={styles.overlay} onClick={() => setConfirmingDelete(false)}>
          <div className={styles.modal} onClick={e => e.stopPropagation()}>
            <p className={styles.modalTitle}>Retire &quot;{agent.name}&quot;?</p>
            <p className={styles.modalSub}>Its matches and playbook go with it. This can't be undone.</p>
            <div className={styles.modalActions}>
              <button className={styles.ghost} onClick={() => setConfirmingDelete(false)}>Keep it</button>
              <button className={styles.dangerBtn} onClick={handleDelete}>Retire agent</button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Negotiation tab (own component: it carries its own ask-thread state)
// ---------------------------------------------------------------------------

interface ChartItem {
  id: number;
  title: string;
  price: number;
  currency: string;
  marketplace: string;
  url: string;
  image_url: string | null;
  tier: "pick" | "match" | "weak" | "rest";
}

function NegotiationView({
  agent,
  market,
  authHeaders,
  listings,
  weakListings,
  sweep,
  ensureSweep,
  onInspect,
}: {
  agent: Agent;
  market: Market | null;
  authHeaders: Record<string, string>;
  listings: RankedListing[];
  weakListings: RankedListing[];
  sweep: SweepListing[] | null;
  ensureSweep: () => void;
  onInspect: (l: InspectListing) => void;
}) {
  const [messages, setMessages] = useState<{ role: "user" | "assistant"; content: string; takeaways?: Takeaway[] }[]>([]);
  const [draft, setDraft] = useState("");
  const [asking, setAsking] = useState(false);
  const threadEnd = useRef<HTMLDivElement>(null);

  useEffect(() => {
    threadEnd.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [messages, asking]);

  useEffect(() => {
    ensureSweep();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const chartItems: ChartItem[] = [
    ...listings.slice(0, 5).map(l => ({ ...l, tier: "pick" as const })),
    ...listings.slice(5).map(l => ({ ...l, tier: "match" as const })),
    ...weakListings.map(l => ({ ...l, tier: "weak" as const })),
    ...(sweep ?? []).filter(l => !l.matched).map(l => ({ ...l, tier: "rest" as const })),
  ];

  const checks = playbookSection(agent.playbook, "What to check", "The going rate");
  const haggle = playbookSection(agent.playbook, "How to haggle", "Your walk-away");
  const neg = market?.negotiation ?? null;

  const ask = async () => {
    const text = draft.trim();
    if (!text || asking) return;
    const next = [...messages, { role: "user" as const, content: text }];
    setMessages(next);
    setDraft("");
    setAsking(true);
    try {
      const res = await fetch(`/api/watchlists/${agent.id}/ask`, {
        method: "POST",
        headers: { ...authHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({ messages: next.map(({ role, content }) => ({ role, content })) }),
      });
      const data = await res.json();
      setMessages([...next, {
        role: "assistant",
        content: res.ok ? data.reply : "I hit a snag answering that one. Give it another try in a moment.",
        takeaways: res.ok && Array.isArray(data.takeaways) ? data.takeaways : [],
      }]);
    } catch {
      setMessages([...next, { role: "assistant", content: "I hit a snag answering that one. Give it another try in a moment." }]);
    } finally {
      setAsking(false);
    }
  };

  const scale = neg ? {
    lo: Math.min(neg.open * 0.85, neg.open - 20),
    hi: Math.max(neg.walk * 1.2, neg.walk + 40),
  } : null;
  const pos = (v: number) => scale ? `${Math.min(96, Math.max(3, ((v - scale.lo) / (scale.hi - scale.lo)) * 100))}%` : "0%";

  // Beeswarm placement: same-price listings fan out from the centerline
  // instead of stacking into vertical strings; out-of-range outliers are
  // dropped rather than piling up on the chart's edges.
  const placedDots = useMemo(() => {
    if (!scale) return [];
    const spread = [0, -1, 1, -2, 2, -3, 3];
    const buckets = new Map<number, number>();
    return chartItems
      .filter(l => l.price >= scale.lo && l.price <= scale.hi)
      .sort((a, b) => a.price - b.price || a.id - b.id)
      .map(l => {
        const x = ((l.price - scale.lo) / (scale.hi - scale.lo)) * 100;
        const bucket = Math.round(x / 2.4);
        const n = buckets.get(bucket) ?? 0;
        buckets.set(bucket, n + 1);
        const ring = Math.floor(n / spread.length);
        const y = 50 + spread[n % spread.length] * 13 + ring * 5;
        const xNudge = ((n * 7) % 3 - 1) * 0.55;
        return {
          ...l,
          x: Math.min(97.5, Math.max(2.5, x + xNudge)),
          y: Math.min(90, Math.max(10, y)),
        };
      })
      .sort((a, b) => {
        const rank = { rest: 0, weak: 1, match: 2, pick: 3 };
        return rank[a.tier] - rank[b.tier];   // picks render last, on top
      });
  }, [chartItems, scale]);

  return (
    <div className={styles.view}>
      {neg
        ? <SayLine lead="Your numbers for this market. " dim="Then what to know." />
        : <SayLine lead="How I'd play this category. " dim="Numbers land once the market fills in." />}

      {neg && scale && (
        <div className={styles.negChart}>
          <div className={styles.negScale}>
            <div className={styles.negZoneOpen} style={{ left: 0, width: pos(neg.open) }} />
            <div className={styles.negZoneFair} style={{ left: pos(neg.fair_low), width: `calc(${pos(neg.fair_high)} - ${pos(neg.fair_low)})` }} />
            <span className={[styles.negMark, styles.negOpen].join(" ")} style={{ left: pos(neg.open) }}><i /><b>open ${neg.open}</b></span>
            <span className={[styles.negMark, styles.negTypical].join(" ")} style={{ left: pos(neg.median) }}><i /><b>typical ${neg.median}</b></span>
            <span className={[styles.negMark, styles.negWalk].join(" ")} style={{ left: pos(neg.walk) }}><i /><b>walk ${neg.walk}</b></span>
            <div className={styles.chartLegend}>
              <span className={styles.legendTitle}>Legend</span>
              <span><i className={styles.cd_pick} /> top picks</span>
              <span><i className={styles.cd_match} /> matches</span>
              <span><i className={styles.cd_weak} /> weak</span>
              <span><i className={styles.cd_rest} /> everything else</span>
            </div>
            {placedDots.map(l => (
              <span
                key={`${l.tier}-${l.id}`}
                className={styles.chartDotWrap}
                style={{ left: `${l.x}%`, top: `${l.y}%` }}
              >
                <span className={[styles.chartDot, styles[`cd_${l.tier}`]].join(" ")} />
                <span className={styles.chartPop}>
                  {l.image_url && (
                    <img src={l.image_url} alt="" loading="lazy" referrerPolicy="no-referrer" className={styles.chartPopThumb} />
                  )}
                  <span className={styles.chartPopBody}>
                    <span className={styles.chartPopTitle}>{l.title}</span>
                    <span className={styles.chartPopPrice}>${l.price.toFixed(0)} · {MARKETPLACE_LABELS[l.marketplace] ?? l.marketplace}</span>
                    <span className={styles.chartPopActions}>
                      <button className={styles.askBtn} onClick={() => onInspect(l)}>Ask Scout</button>
                      <a className={styles.viewBtn} style={{ marginLeft: 0 }} href={l.url} target="_blank" rel="noopener noreferrer">View ↗</a>
                    </span>
                  </span>
                </span>
              </span>
            ))}
          </div>
          <div className={styles.negScaleLabels}>
            <span>${Math.round(scale.lo)}</span>
            <span>fair ${neg.fair_low}–{neg.fair_high}</span>
            <span>${Math.round(scale.hi)}</span>
          </div>
        </div>
      )}

      <div className={styles.negGrid}>
      {neg && (
        <div className={styles.negFig}>
          <div className={[styles.figRow, styles.figRowGreen].join(" ")}>
            <span className={styles.figLabel}>Opening price</span>
            <span className={[styles.figVal, styles.figValGreen].join(" ")}>${neg.open}</span>
          </div>
          <div className={[styles.figRow, styles.figRowBlue].join(" ")}>
            <span className={styles.figLabel}>Fair deal range</span>
            <span className={[styles.figVal, styles.figValBlue].join(" ")}>${neg.fair_low}–{neg.fair_high}</span>
          </div>
          <div className={[styles.figRow, styles.figRowRed].join(" ")}>
            <span className={styles.figLabel}>Walk-away price</span>
            <span className={[styles.figVal, styles.figValRed].join(" ")}>${neg.walk}</span>
          </div>
        </div>
      )}

      <div className={styles.negRight}>
      <div className={styles.negCards}>
        {checks && (
          <div className={styles.negCard}>
            <span className={styles.negIcon}>
              <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
            </span>
            <div className={styles.negCardBody}>
              <span className={styles.negCardTitle}>Check before you pay</span>
              <span className={styles.negCardText}>{firstSentences(checks, 3)}</span>
            </div>
          </div>
        )}
        {haggle && (
          <div className={styles.negCard}>
            <span className={styles.negIcon}>
              <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12a8 8 0 1 1-4-6.9" /><path d="M21 3v6h-6" /></svg>
            </span>
            <div className={styles.negCardBody}>
              <span className={styles.negCardTitle}>How to haggle here</span>
              <span className={styles.negCardText}>{firstSentences(haggle, 2)}</span>
            </div>
          </div>
        )}
        <div className={styles.negCard}>
          <span className={styles.negIcon}>
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M12 3 4 6v6c0 4.4 3.4 8.4 8 9 4.6-.6 8-4.6 8-9V6Z" /></svg>
          </span>
          <div className={styles.negCardBody}>
            <span className={styles.negCardTitle}>Meeting up</span>
            <span className={styles.negCardText}>{MEETING_UP}</span>
          </div>
        </div>
      </div>

      </div>
      </div>

      {neg && (
        <div className={styles.copyLine}>
          <span>&quot;Hi! Is this still available? Would you take ${neg.open} if I pick up today?&quot;</span>
          <button
            className={styles.copyBtn}
            onClick={e => {
              navigator.clipboard?.writeText(`Hi! Is this still available? Would you take $${neg.open} if I pick up today?`);
              (e.target as HTMLButtonElement).textContent = "Copied";
              setTimeout(() => { (e.target as HTMLButtonElement).textContent = "Copy"; }, 1500);
            }}
          >
            Copy
          </button>
        </div>
      )}

      {messages.map((m, i) => (
        <div key={i} className={m.role === "user" ? styles.userMsg : styles.scoutMsgRow}>
          {m.role === "assistant" && <span className={[styles.avatar, styles.avatarActive].join(" ")}><ScoutGlyph /></span>}
          <div className={m.role === "user" ? undefined : styles.scoutMsg}>
            {m.role === "assistant" && i === messages.length - 1 ? (
              <TypedReply text={m.content} takeaways={m.takeaways ?? []} />
            ) : (
              <>
                <p className={styles.msgText}>{m.content}</p>
                {m.role === "assistant" && <TakeawayBoxes takeaways={m.takeaways ?? []} />}
              </>
            )}
          </div>
        </div>
      ))}
      {asking && (
        <div className={styles.scoutMsgRow}>
          <span className={styles.avatar}><ScoutGlyph /></span>
          <div className={styles.typingBubble} aria-hidden><span /><span /><span /></div>
        </div>
      )}
      <div ref={threadEnd} />

      <div className={styles.composer}>
        <span className={styles.avatar}><ScoutGlyph /></span>
        <input
          className={styles.askInput}
          placeholder="Any questions? I know this market pretty well…"
          value={draft}
          disabled={asking}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter") ask(); }}
        />
        <button className={styles.cta} onClick={ask} disabled={asking || !draft.trim()}>Ask</button>
      </div>

    </div>
  );
}

// ---------------------------------------------------------------------------
// Sweep modal: the agent's lane theater, alive for the hunt's duration
// ---------------------------------------------------------------------------

function SweepModal({ huntId, onClose }: { huntId: number; onClose: () => void }) {
  const [hunt, setHunt] = useState<HuntSummary | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const res = await fetch("/api/hunts?limit=30");
        if (!res.ok) return;
        const data = await res.json();
        const found = (data.hunts ?? []).find((h: HuntSummary) => h.id === huntId);
        if (found) setHunt(found);
      } catch {
        /* the modal shows its connecting line */
      }
    })();
  }, [huntId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return createPortal(
    <div className={styles.overlay} onClick={onClose}>
      <div className={styles.sweepModal} onClick={e => e.stopPropagation()}>
        <div className={styles.sweepModalHead}>
          <span className={styles.name}>Live sweep</span>
          <button className={styles.iconBtn} onClick={onClose} aria-label="Close">✕</button>
        </div>
        {hunt ? <AgentCard hunt={hunt} onAlert={() => {}} /> : <p className={styles.loading}>Connecting to the sweep…</p>}
      </div>
    </div>,
    document.body,
  );
}

// ---------------------------------------------------------------------------
// Edit-brief modal: the exiled config surface
// ---------------------------------------------------------------------------

function EditBriefModal({
  agent,
  authHeaders,
  onClose,
}: {
  agent: Agent;
  authHeaders: Record<string, string>;
  onClose: () => void;
}) {
  const ctx = agent.context;
  const [notes, setNotes] = useState(ctx?.buyer_profile ?? "");
  const [budget, setBudget] = useState(ctx?.max_budget != null ? String(ctx.max_budget) : "");
  const [conditions, setConditions] = useState<string[]>(ctx?.condition ?? []);
  const [saving, setSaving] = useState(false);

  async function save() {
    setSaving(true);
    const parsed = parseFloat(budget);
    await fetch(`/api/watchlists/${agent.id}`, {
      method: "PATCH",
      headers: { ...authHeaders, "Content-Type": "application/json" },
      body: JSON.stringify({
        buyer_profile: notes.trim(),
        max_budget: !isNaN(parsed) && parsed > 0 ? parsed : null,
        condition: conditions,
      }),
    });
    setSaving(false);
    onClose();
  }

  return createPortal(
    <div className={styles.overlay} onClick={onClose}>
      <div className={styles.modal} onClick={e => e.stopPropagation()}>
        <p className={styles.modalTitle}>What Scout hunts for</p>
        <label className={styles.fieldLabel}>Notes for Scout</label>
        <textarea
          className={styles.fieldArea}
          rows={3}
          value={notes}
          onChange={e => setNotes(e.target.value)}
          placeholder="Who's this for, and what matters? e.g. WFH full-time, back pain, needs real lumbar support"
        />
        <label className={styles.fieldLabel}>Budget ceiling</label>
        <input className={styles.fieldInput} type="number" placeholder="Max $" value={budget} onChange={e => setBudget(e.target.value)} />
        <label className={styles.fieldLabel}>Condition</label>
        <div className={styles.pillRow}>
          {(["new", "refurb", "used"] as const).map(c => (
            <button
              key={c}
              type="button"
              className={[styles.pill, conditions.includes(c) ? styles.pillOn : ""].join(" ")}
              onClick={() => setConditions(prev => prev.includes(c) ? prev.filter(x => x !== c) : [...prev, c])}
            >
              {c}
            </button>
          ))}
        </div>
        <p className={styles.modalSub}>Saving re-aims the agent: rankings and the playbook refresh in the background.</p>
        <div className={styles.modalActions}>
          <button className={styles.ghost} onClick={onClose}>Cancel</button>
          <button className={styles.cta} onClick={save} disabled={saving}>{saving ? "Saving…" : "Save"}</button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
