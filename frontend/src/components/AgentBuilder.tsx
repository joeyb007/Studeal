"use client";

import { useEffect, useRef, useState } from "react";
import styles from "./AgentBuilder.module.css";

function useTypewriter(text: string, speed = 22): { displayed: string; done: boolean } {
  const [displayed, setDisplayed] = useState("");
  const [done, setDone] = useState(false);

  useEffect(() => {
    setDisplayed("");
    setDone(false);
    if (!text) {
      setDone(true);
      return;
    }
    let i = 0;
    const interval = setInterval(() => {
      i++;
      setDisplayed(text.slice(0, i));
      if (i >= text.length) {
        clearInterval(interval);
        setDone(true);
      }
    }, speed);
    return () => clearInterval(interval);
  }, [text, speed]);

  return { displayed, done };
}

function ScoutAvatar({ active }: { active: boolean }) {
  return (
    <span className={[styles.avatar, active ? styles.avatarActive : ""].join(" ")}>
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
        <path d="M12 3 L14 12 L12 21 L10 12 Z" />
        <path d="M3 12 L12 10 L21 12 L12 14 Z" />
      </svg>
    </span>
  );
}

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

interface WatchlistContext {
  product_query: string;
  max_budget: number | null;
  min_discount_pct: number | null;
  condition: string[];
  brands: string[];
  keywords: string[];
}

interface AgentBuilderProps {
  context: WatchlistContext | null;
  messages: ChatMessage[];
  suggestions: string[];
  isLoading: boolean;
  isComplete: boolean;
  input: string;
  onInputChange: (val: string) => void;
  onSend: (override?: string) => void;
  name: string;
  onNameChange: (val: string) => void;
  onDeploy: () => void;
  submitting: boolean;
  formError: string | null;
}

const STAGE_ICONS = ["⌕", "$", "◈", "✦", "◎"];

export default function AgentBuilder({
  context,
  messages,
  suggestions,
  isLoading,
  isComplete,
  input,
  onInputChange,
  onSend,
  name,
  onNameChange,
  onDeploy,
  submitting,
  formError,
}: AgentBuilderProps) {
  const lastAssistantMsg = [...messages].reverse().find(m => m.role === "assistant");
  const { displayed: typewriterText, done: typewriterDone } = useTypewriter(
    lastAssistantMsg?.content ?? "",
  );

  const stages = [
    {
      label: "Query",
      done: !!context?.product_query,
      value: context?.product_query || null,
    },
    {
      label: "Budget",
      done: context?.max_budget !== null && context?.max_budget !== undefined,
      value: context?.max_budget != null ? `$${context.max_budget}` : null,
    },
    {
      label: "Condition",
      done: (context?.condition?.length ?? 0) > 0,
      values: context?.condition ?? [],
    },
    {
      label: "Keywords",
      done: (context?.keywords?.length ?? 0) >= 3,
      values: context?.keywords ?? [],
    },
    {
      label: "Ready",
      done: isComplete,
      value: isComplete ? "ready" : null,
    },
  ];

  const nextUndoneIdx = stages.findIndex(s => !s.done);
  const activeIdx = isComplete ? 4 : nextUndoneIdx === -1 ? 4 : nextUndoneIdx;

  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!isLoading && !isComplete) inputRef.current?.focus();
  }, [isLoading, isComplete]);

  return (
    <div className={styles.card}>
      <div className={styles.cardHeader}>
        <span className={styles.cardLabel}>configuring agent</span>
        <span className={styles.liveDot} />
      </div>

      <div className={styles.pipeline}>
        {stages.map((stage, i) => (
          <div key={stage.label} className={styles.stageCol}>
            <div
              className={[
                styles.node,
                stage.done ? styles.nodeDone : "",
                i === activeIdx && !stage.done ? styles.nodeActive : "",
              ].join(" ")}
            >
              <span className={styles.nodeIcon}>{STAGE_ICONS[i]}</span>
              {i === activeIdx && !stage.done && <span className={styles.nodePulse} />}
            </div>
            <span
              className={[
                styles.nodeLabel,
                stage.done ? styles.nodeLabelDone : "",
                i === activeIdx && !stage.done ? styles.nodeLabelActive : "",
              ].join(" ")}
            >
              {stage.label}
            </span>
            {i < stages.length - 1 && (
              <div
                className={[
                  styles.connector,
                  stage.done && stages[i + 1].done ? styles.connectorDone : "",
                  stage.done && !stages[i + 1].done ? styles.connectorActive : "",
                ].join(" ")}
              />
            )}
          </div>
        ))}
      </div>

      <div className={styles.panelArea}>
        {stages.slice(0, 4).map(stage => {
          const isList = "values" in stage;
          const hasValues = isList && (stage.values?.length ?? 0) > 0;
          return (
            <div key={stage.label} className={styles.panelRow}>
              <div className={styles.panelRowMain}>
                <span className={styles.panelKey}>{stage.label.toLowerCase()}</span>
                <span className={styles.panelDots} />
                {!isList && (
                  <span
                    className={[
                      styles.panelValue,
                      stage.done ? styles.panelValueDone : "",
                    ].join(" ")}
                  >
                    {stage.value ?? "—"}
                  </span>
                )}
                {isList && !hasValues && <span className={styles.panelValue}>—</span>}
                {isList && hasValues && stage.label === "Keywords" && (
                  <span className={[styles.panelValue, styles.panelValueDone].join(" ")}>
                    {stage.values.length} variants
                  </span>
                )}
                <span className={styles.panelCheck}>{stage.done ? "✓" : ""}</span>
              </div>
              {isList && hasValues && stage.label !== "Keywords" && (
                <div className={styles.chipList}>
                  {stage.values.map(v => (
                    <span key={v} className={styles.valueChip}>{v}</span>
                  ))}
                </div>
              )}
              {isList && hasValues && stage.label === "Keywords" && (
                <div className={styles.chipList}>
                  {stage.values.slice(0, 5).map(v => (
                    <span key={v} className={styles.valueChip}>{v}</span>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {!isComplete && lastAssistantMsg && (
        <div className={styles.scoutRow}>
          <ScoutAvatar active={!typewriterDone} />
          <div className={styles.scoutBody}>
            <span className={styles.scoutPrefix}>Scout</span>
            <span className={styles.scoutText}>
              {typewriterText}
              {!typewriterDone && <span className={styles.cursor}>▍</span>}
            </span>
          </div>
        </div>
      )}

      {!isComplete && suggestions.length > 0 && !isLoading && typewriterDone && (
        <div className={styles.suggestionRow}>
          {suggestions.map(s => (
            <button
              key={s}
              className={styles.suggestionChip}
              onClick={() => onSend(s)}
              disabled={isLoading}
            >
              {s}
            </button>
          ))}
        </div>
      )}

      {isComplete ? (
        <div className={styles.deployRow}>
          <input
            className={styles.nameInput}
            type="text"
            placeholder="Name your agent..."
            value={name}
            onChange={e => onNameChange(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") onDeploy(); }}
            autoFocus
            disabled={submitting}
          />
          <button
            className={styles.deployBtn}
            onClick={onDeploy}
            disabled={submitting || !name.trim()}
          >
            {submitting ? "Deploying..." : "Deploy agent →"}
          </button>
        </div>
      ) : (
        <div className={styles.inputRow}>
          <input
            ref={inputRef}
            className={styles.input}
            type="text"
            placeholder={isLoading ? "Scout is thinking..." : "Type your response..."}
            value={input}
            onChange={e => onInputChange(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") onSend(); }}
            disabled={isLoading}
          />
          <button
            className={styles.sendBtn}
            onClick={() => onSend()}
            disabled={isLoading || !input.trim()}
          >
            →
          </button>
        </div>
      )}

      <div className={styles.thoughtTrace}>
        {isLoading && (
          <>
            <span className={styles.thoughtPrefix}>→</span>
            <span className={styles.thoughtText}>
              parsing intent · extracting {stages[activeIdx]?.label.toLowerCase() ?? "context"}...
            </span>
          </>
        )}
        {formError && <span className={styles.errorText}>{formError}</span>}
      </div>
    </div>
  );
}
