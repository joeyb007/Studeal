"use client";

import { useEffect, useState, useRef } from "react";
import Image from "next/image";
import styles from "./AgentWorkflow.module.css";

const STAGE_DURATION = 6000;

const STAGES = [
  { icon: "⌕", label: "Plan" },
  { icon: "↓", label: "Browse" },
  { icon: "◈", label: "Extract" },
  { icon: "◎", label: "Rank" },
  { icon: "✦", label: "Alert" },
];

const TOOL_CALLS: string[][] = [
  ["plan_queries(spec)", "route_marketplaces()"],
  ["goto(kijiji.ca)", "click('Next page')"],
  ["extract_offers(page)", "canonicalize_urls()"],
  ["rerank(spec, listings)", "apply_budget_filter()"],
  ["create_alert(listing)", "send_push()"],
];

const THOUGHTS = [
  "3 query phrasings planned · routing kijiji, ebay, craigslist...",
  "browsing kijiji results · pagination 2 of 3 · 24 cards visible...",
  "extracting titles, prices, locations from the live page...",
  "ranking 47 offers against your spec · budget ≤ $500...",
  "2 new finds above 85% match · pinging you now",
];

const SEARCH_URLS = [
  "kijiji.ca/b-toronto/aeron-chair",
  "ebay.ca/sch/herman-miller-aeron",
  "toronto.craigslist.org/search/fua",
  "kijiji.ca/b-gta/herman-miller",
  "ebay.ca/sch/aeron?_pgn=2",
  "kijiji.ca/b-toronto/aeron/page-3",
];

const FETCH_PAGES = [
  { domain: "kijiji.ca", found: "24 listings", done: true },
  { domain: "ebay.ca", found: "48 listings", done: true },
  { domain: "craigslist.org", found: "31 postings", done: true },
  { domain: "kijiji.ca · p2", found: "", done: false },
  { domain: "ebay.ca · p2", found: "37 listings", done: true },
];

const EXTRACT_ITEMS = [
  { title: "Aeron Size B — fully loaded", price: "$420", good: true, confidence: 94 },
  { title: "Aeron, posturefit, mint", price: "$495", good: true, confidence: 88 },
  { title: "Office chair (unbranded)", price: "$85", good: false, confidence: 12 },
  { title: "Aeron, needs casters", price: "$340", good: true, confidence: 71 },
  { title: "Aeron-style replica", price: "$150", good: false, confidence: 23 },
];

const SCORE_STEPS = [
  "Applying your budget filter...",
  "Scoring against your spec...",
  "Deduplicating across marketplaces...",
  "Picking the standouts...",
];

const VERIFY_ITEMS = [
  { label: "Push notification sent", found: true },
  { label: "Email alert delivered", found: true },
  { label: "Added to your feed", found: true },
  { label: "Under your $500 budget", found: true },
];

const STAGE_STATS = [
  { label: "queries planned", getValue: (tick: number) => `${Math.min(tick, SEARCH_URLS.length)} / ${SEARCH_URLS.length}` },
  { label: "pages browsed", getValue: (tick: number) => `${Math.min(tick, FETCH_PAGES.filter(p => p.done).length)} / ${FETCH_PAGES.length}` },
  { label: "candidates", getValue: (tick: number) => `${Math.min(tick, EXTRACT_ITEMS.filter(i => i.good).length)} real listings` },
  { label: "top match", getValue: (tick: number) => `${Math.round(Math.min((tick / SCORE_STEPS.length) * 84, 84))} / 100` },
  { label: "alerts sent", getValue: (tick: number) => `${Math.min(tick, VERIFY_ITEMS.filter(v => v.found).length)} delivered` },
];

function faviconUrl(domain: string) {
  return `https://www.google.com/s2/favicons?domain=${domain}&sz=32`;
}

function ToolCallFlash({ stage, tick }: { stage: number; tick: number }) {
  const calls = TOOL_CALLS[stage];
  if (tick < 1) return <div className={styles.toolCallSlot} />;
  const callIndex = Math.min(Math.floor((tick - 1) / 3), calls.length - 1);
  const call = calls[callIndex];
  return (
    <div className={styles.toolCallSlot}>
      <div className={styles.toolCall} key={`${stage}-${callIndex}`}>
        <span className={styles.toolCallPrefix}>▶</span>
        <code className={styles.toolCallText}>{call}</code>
      </div>
    </div>
  );
}

function SearchPanel({ tick, stage }: { tick: number; stage: number }) {
  const visible = Math.min(tick, SEARCH_URLS.length);
  return (
    <>
      <p className={styles.panelTitle}>Aggregating the web</p>
      <ToolCallFlash stage={stage} tick={tick} />
      <div className={styles.urlList}>
        {SEARCH_URLS.slice(0, visible).map((url, i) => (
          <div key={i} className={styles.urlRow}>
            <span className={styles.urlDot} />
            <span className={styles.urlText}>{url}</span>
          </div>
        ))}
        {visible < SEARCH_URLS.length && (
          <div className={styles.urlRow}>
            <span className={styles.urlDotPulsing} />
            <span className={styles.urlLoading}>searching...</span>
          </div>
        )}
      </div>
    </>
  );
}

function FetchPanel({ tick, stage }: { tick: number; stage: number }) {
  const visible = Math.min(tick, FETCH_PAGES.length);
  return (
    <>
      <p className={styles.panelTitle}>Fetching pages</p>
      <ToolCallFlash stage={stage} tick={tick} />
      <div className={styles.fetchList}>
        {FETCH_PAGES.slice(0, visible).map((page, i) => (
          <div key={i} className={styles.fetchRow}>
            <Image
              src={faviconUrl(page.domain)}
              alt={page.domain}
              width={16}
              height={16}
              className={styles.favicon}
              unoptimized
            />
            <span className={styles.fetchDomain}>{page.domain}</span>
            {page.done
              ? <span className={styles.fetchFound}>{page.found}</span>
              : <span className={styles.fetchSpinner}>⟳</span>
            }
          </div>
        ))}
      </div>
    </>
  );
}

function ExtractPanel({ tick, stage }: { tick: number; stage: number }) {
  const visible = Math.min(tick, EXTRACT_ITEMS.length);
  return (
    <>
      <p className={styles.panelTitle}>Extracting candidates</p>
      <ToolCallFlash stage={stage} tick={tick} />
      <div className={styles.extractList}>
        {EXTRACT_ITEMS.slice(0, visible).map((item, i) => (
          <div key={i} className={[
            styles.extractRow,
            item.good ? styles.extractGood : styles.extractMuted,
          ].join(" ")}>
            <span className={styles.extractDot}>{item.good ? "●" : "○"}</span>
            <span className={styles.extractTitle}>{item.title}</span>
            <span className={styles.extractPrice}>{item.price}</span>
            <span className={[
              styles.confidenceBadge,
              item.good ? styles.confidenceHigh : styles.confidenceLow,
            ].join(" ")}>{item.confidence}%</span>
          </div>
        ))}
      </div>
    </>
  );
}

function ScorePanel({ tick, stage }: { tick: number; stage: number }) {
  const progress = Math.min((tick / SCORE_STEPS.length) * 84, 84);
  const visibleSteps = Math.min(tick, SCORE_STEPS.length);
  const rounded = Math.round(progress);

  // Animate bar from 0 on first mount by starting at 0 then setting real width
  const [barWidth, setBarWidth] = useState(0);
  useEffect(() => {
    if (tick < 4) return;
    // One frame delay so the CSS transition fires from 0
    const id = requestAnimationFrame(() => setBarWidth(progress));
    return () => cancelAnimationFrame(id);
  }, [progress, tick]);

  return (
    <>
      <p className={styles.panelTitle}>Scoring best deal</p>
      <ToolCallFlash stage={stage} tick={tick} />
      <div className={styles.scoreTop}>
        <div className={styles.productThumb} />
        <div className={styles.scoreTopText}>
          <span className={styles.scoreProductTitle}>AirPods Pro 2nd Gen</span>
          <span className={styles.scoreProductPrice}>$189 <s className={styles.scoreWas}>$249</s></span>
        </div>
      </div>
      <div className={styles.scoreSteps}>
        {SCORE_STEPS.slice(0, visibleSteps).map((step, i) => (
          <div key={i} className={styles.scoreStep}>
            <span className={styles.scoreStepCheck}>✓</span>
            <span className={styles.scoreStepText}>{step}</span>
          </div>
        ))}
      </div>
      {tick >= 4 && (
        <div className={styles.scoreBar}>
          <div className={styles.scoreBarTrack}>
            <div className={styles.scoreBarFill} style={{ width: `${barWidth}%` }} />
          </div>
          <div className={styles.scoreNumbers}>
            <span className={styles.scoreValue}>
              {rounded}/100
              <span className={styles.scorePct}> · {rounded}%</span>
            </span>
            {tick >= SCORE_STEPS.length && (
              <span className={styles.scoreBadge}>push alert</span>
            )}
          </div>
        </div>
      )}
    </>
  );
}

function VerifyPanel({ tick, stage }: { tick: number; stage: number }) {
  const visible = Math.min(tick, VERIFY_ITEMS.length);
  return (
    <>
      <p className={styles.panelTitle}>Verifying student eligibility</p>
      <ToolCallFlash stage={stage} tick={tick} />
      <div className={styles.extractList}>
        {VERIFY_ITEMS.slice(0, visible).map((item, i) => (
          <div key={i} className={[
            styles.extractRow,
            item.found ? styles.extractGood : styles.extractMuted,
          ].join(" ")}>
            <span className={styles.extractDot}>{item.found ? "●" : "○"}</span>
            <span className={styles.extractTitle}>{item.label}</span>
            <span className={[
              styles.confidenceBadge,
              item.found ? styles.confidenceHigh : styles.confidenceLow,
            ].join(" ")}>{item.found ? "✓" : "✗"}</span>
          </div>
        ))}
      </div>
    </>
  );
}

interface AgentWorkflowProps {
  started?: boolean;
}

export default function AgentWorkflow({ started = false }: AgentWorkflowProps) {
  const [activeNode, setActiveNode] = useState(0);
  const [tick, setTick] = useState(0);
  const tickRef = useRef<ReturnType<typeof setInterval>>(undefined);
  const stageRef = useRef<ReturnType<typeof setInterval>>(undefined);

  useEffect(() => {
    if (!started) return;

    let node = 0;

    const startStage = (n: number) => {
      setActiveNode(n);
      setTick(0);
      clearInterval(tickRef.current);
      tickRef.current = setInterval(() => {
        setTick((t) => t + 1);
      }, STAGE_DURATION / 8);
    };

    startStage(0);
    stageRef.current = setInterval(() => {
      node = (node + 1) % STAGES.length;
      startStage(node);
    }, STAGE_DURATION);

    return () => {
      clearInterval(stageRef.current);
      clearInterval(tickRef.current);
    };
  }, [started]);

  const renderPanel = () => {
    switch (activeNode) {
      case 0: return <SearchPanel tick={tick} stage={0} />;
      case 1: return <FetchPanel tick={tick} stage={1} />;
      case 2: return <ExtractPanel tick={tick} stage={2} />;
      case 3: return <ScorePanel tick={tick} stage={3} />;
      case 4: return <VerifyPanel tick={tick} stage={4} />;
    }
  };

  const stat = STAGE_STATS[activeNode];
  const statValue = stat.getValue(tick);

  // Animate the leading number counting up when the value increases
  const [displayValue, setDisplayValue] = useState(statValue);
  const prevValueRef = useRef(statValue);
  const rafRef = useRef<number>(0);
  useEffect(() => {
    const prev = prevValueRef.current;
    prevValueRef.current = statValue;
    if (prev === statValue) return;

    cancelAnimationFrame(rafRef.current);

    const match = statValue.match(/^(\d+)/);
    const prevMatch = prev.match(/^(\d+)/);
    if (!match || !prevMatch) {
      rafRef.current = requestAnimationFrame(() => setDisplayValue(statValue));
      return;
    }

    const target = parseInt(match[1]);
    const start = parseInt(prevMatch[1]);
    if (start >= target) {
      rafRef.current = requestAnimationFrame(() => setDisplayValue(statValue));
      return;
    }

    let current = start;
    const step = () => {
      current += 1;
      setDisplayValue(statValue.replace(/^\d+/, String(current)));
      if (current < target) rafRef.current = requestAnimationFrame(step);
    };
    rafRef.current = requestAnimationFrame(step);

    return () => cancelAnimationFrame(rafRef.current);
  }, [statValue]);

  return (
    <div className={styles.wrapper}>
      {/* Side stat popup — left of card */}
      {started && tick >= 2 && (
        <div className={styles.statPopup} key={activeNode}>
          <span className={styles.statValue} key={displayValue}>{displayValue}</span>
          <span className={styles.statLabel}>{stat.label}</span>
        </div>
      )}

      <div className={styles.card}>
        {/* Header */}
        <div className={styles.cardHeader}>
          <span className={styles.cardLabel}>agent pipeline</span>
          <span className={styles.liveDot} />
        </div>

        {/* Pipeline nodes */}
        <div className={styles.pipeline}>
          {STAGES.map((stage, i) => (
            <div key={stage.label} className={styles.stageCol}>
              <div className={[
                styles.node,
                i === activeNode ? styles.nodeActive : "",
                i < activeNode ? styles.nodeDone : "",
              ].join(" ")}>
                <span className={styles.nodeIcon}>{stage.icon}</span>
                <span className={styles.nodeLabel}>{stage.label}</span>
                {i === activeNode && <span className={styles.nodePulse} />}
              </div>
              {i < STAGES.length - 1 && (
                <div className={[
                  styles.connector,
                  i < activeNode ? styles.connectorDone : "",
                  i === activeNode - 1 ? styles.connectorActive : "",
                ].join(" ")} />
              )}
            </div>
          ))}
        </div>

        {/* Panel below — fixed height, content swaps */}
        <div className={styles.panelArea}>
          <div className={styles.panelInner} key={activeNode}>
            {renderPanel()}
          </div>
        </div>

        {/* Thought trace — always reserve space, content appears at tick 3 */}
        <div className={styles.thoughtTrace} key={`thought-${activeNode}`}>
          {started && tick >= 3 && (
            <>
              <span className={styles.thoughtPrefix}>→</span>
              <span className={styles.thoughtText}>{THOUGHTS[activeNode]}</span>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
