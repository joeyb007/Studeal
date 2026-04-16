"use client";

import { useEffect, useState, useRef } from "react";
import Image from "next/image";
import styles from "./AgentWorkflow.module.css";

const STAGE_DURATION = 6000;

const STAGES = [
  { icon: "⌕", label: "Search" },
  { icon: "↓", label: "Fetch" },
  { icon: "◈", label: "Extract" },
  { icon: "◎", label: "Score" },
  { icon: "✦", label: "Verify" },
];

const TOOL_CALLS: string[][] = [
  ["brave_search(query)", "aggregate_sources()"],
  ["fetch_page(url)", "extract_links(html)"],
  ["verify_discount(price)", "parse_deal(html)"],
  ["fetch_price_history()", "check_deal_threshold()"],
  ["brave_search(student_query)", "tag_student_eligible()"],
];

const THOUGHTS = [
  "querying brave · aggregating 6 sources across retailers...",
  "parsing HTML · extracting product metadata from 5 domains...",
  "discount 24% · verifying against 90-day price floor $201...",
  "score = 0.4×discount + 0.3×history + 0.3×recency → 84",
  "re-querying brave with student_query · tagging eligible deals...",
];

const SEARCH_URLS = [
  "amazon.com/dp/B0BDHWDR12",
  "bestbuy.com/site/airpods-pro-2nd...",
  "slickdeals.net/deals/apple-airpods...",
  "reddit.com/r/deals/airpods_deal",
  "walmart.com/ip/Apple-AirPods-Pro",
  "target.com/p/apple-airpods-pro",
];

const FETCH_PAGES = [
  { domain: "amazon.com", found: "1 product", done: true },
  { domain: "bestbuy.com", found: "1 product", done: true },
  { domain: "slickdeals.net", found: "3 deals", done: true },
  { domain: "reddit.com", found: "", done: false },
  { domain: "walmart.com", found: "1 product", done: true },
];

const EXTRACT_ITEMS = [
  { title: "AirPods Pro 2nd Gen", price: "$189", good: true, confidence: 94 },
  { title: "AirPods 3rd Gen", price: "$129", good: true, confidence: 87 },
  { title: "AirPods Max (USB-C)", price: "$449", good: false, confidence: 12 },
  { title: "Sony WH-1000XM5", price: "$278", good: true, confidence: 71 },
  { title: "AirPods Pro 1st Gen", price: "$199", good: false, confidence: 23 },
];

const SCORE_STEPS = [
  "Verifying discount depth...",
  "Checking price history...",
  "Comparing similar deals...",
  "Computing final score...",
];

const VERIFY_ITEMS = [
  { label: "UNiDAYS eligible", found: true },
  { label: ".edu pricing available", found: true },
  { label: "Student Beans verified", found: false },
  { label: "Apple Education Store", found: true },
];

const STAGE_STATS = [
  { label: "sources found", getValue: (tick: number) => `${Math.min(tick, SEARCH_URLS.length)} / ${SEARCH_URLS.length}` },
  { label: "pages fetched", getValue: (tick: number) => `${Math.min(tick, FETCH_PAGES.filter(p => p.done).length)} / ${FETCH_PAGES.length}` },
  { label: "candidates", getValue: (tick: number) => `${Math.min(tick, EXTRACT_ITEMS.filter(i => i.good).length)} strong matches` },
  { label: "deal score", getValue: (tick: number) => `${Math.round(Math.min((tick / SCORE_STEPS.length) * 84, 84))} / 100` },
  { label: "student discounts", getValue: (tick: number) => `${Math.min(tick, VERIFY_ITEMS.filter(v => v.found).length)} verified` },
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
