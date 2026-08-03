"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import styles from "./AgentBuilder.module.css";

// The UI thesis: under the hood Scout runs an LLM-driven state machine that
// pushes toward a deployable spec — but what the user experiences is a real
// conversation. No field stages, no progress pipeline, no "extracting budget"
// copy. The spec surfaces only as "Scout's notes": prose fragments a friend
// jots while listening, in whatever order the conversation actually took.

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
  buyer_profile?: string | null;
}

interface AgentBuilderProps {
  closing?: boolean;
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

/** Scout's running notes: short jotted fragments, never labeled fields.
 *  Condition is only worth a note when it's an actual narrowing (a strict
 *  subset) — "open to anything" is the default, and a friend wouldn't write
 *  that down. */
function notesFrom(context: WatchlistContext | null): string[] {
  if (!context) return [];
  const notes: string[] = [];
  if (context.product_query) notes.push(context.product_query);
  if (context.max_budget != null) notes.push(`$${context.max_budget} cap`);
  const condition = context.condition ?? [];
  if (condition.length > 0 && condition.length < 3) {
    notes.push(`open to ${condition.join(" / ")}`);
  }
  if ((context.brands?.length ?? 0) > 0) {
    notes.push(`prefers ${context.brands.join(", ")}`);
  }
  return notes;
}

export default function AgentBuilder({
  closing = false,
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
  const lastAssistantIdx = messages.map(m => m.role).lastIndexOf("assistant");
  const lastAssistantMsg = lastAssistantIdx >= 0 ? messages[lastAssistantIdx] : null;
  const { displayed: typewriterText, done: typewriterDone } = useTypewriter(
    lastAssistantMsg?.content ?? "",
  );

  const notes = notesFrom(context);
  const profile = context?.buyer_profile ?? null;

  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!isLoading && !isComplete) inputRef.current?.focus();
  }, [isLoading, isComplete]);

  const threadRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = threadRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, typewriterText, isLoading]);

  const userTurns = messages.filter(m => m.role === "user").length;

  // Deploy modal: completion is a discrete commit step and deserves an
  // announced moment, not a silently morphing input. Prefill the name from
  // what Scout learned so the step costs one click, not one decision.
  const [modalDismissed, setModalDismissed] = useState(false);
  useEffect(() => {
    if (isComplete && !name && context?.product_query) {
      const suggested = context.product_query
        .split(/\s+/)
        .slice(0, 4)
        .map(w => w.charAt(0).toUpperCase() + w.slice(1))
        .join(" ");
      onNameChange(suggested);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isComplete]);

  return (
    <div className={[styles.card, closing ? styles.cardClosing : ""].join(" ")}>
      <div className={styles.cardHeader}>
        <span className={styles.cardLabel}>scout</span>
        <span className={styles.liveDot} />
      </div>

      <div className={styles.thread} ref={threadRef}>
        {messages.map((message, i) =>
          message.role === "user" ? (
            <div key={i} className={styles.userRow}>
              <span className={styles.userBubble}>{message.content}</span>
            </div>
          ) : (
            <div key={i} className={styles.scoutRow}>
              <ScoutAvatar active={i === lastAssistantIdx && !typewriterDone} />
              <span className={styles.scoutText}>
                {i === lastAssistantIdx ? typewriterText : message.content}
                {i === lastAssistantIdx && !typewriterDone && (
                  <span className={styles.cursor}>▍</span>
                )}
              </span>
            </div>
          ),
        )}
        {isLoading && (
          <div className={styles.scoutRow}>
            <ScoutAvatar active />
            <span className={styles.typing}>
              <span /><span /><span />
            </span>
          </div>
        )}
      </div>

      {(notes.length > 0 || profile) && (
        <div className={styles.notes}>
          <span className={styles.notesLabel}>scout&apos;s notes</span>
          {notes.length > 0 && (
            <span className={styles.notesLine}>{notes.join(" · ")}</span>
          )}
          {profile && <span className={styles.notesProfile}>{profile}</span>}
        </div>
      )}

      {isComplete ? (
        modalDismissed ? (
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
          createPortal(
          <div className={styles.overlay} onClick={() => setModalDismissed(true)}>
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
              <span className={styles.modalTitle}>Your agent is ready</span>
              <span className={styles.modalSub}>
                Scout has what it needs. Name your agent and set it loose.
              </span>
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
              {formError && <div className={styles.errorRow}>{formError}</div>}
            </div>
          </div>,
          document.body)
        )
      ) : (
        <>
          {suggestions.length > 0 && !isLoading && typewriterDone && (
            <div className={styles.ghostRow}>
              {suggestions.slice(0, 3).map(s => (
                <button
                  key={s}
                  className={styles.ghostChip}
                  onClick={() => onSend(s)}
                  tabIndex={-1}
                >
                  {s}
                </button>
              ))}
            </div>
          )}
        <div className={styles.inputRow}>
          <input
            ref={inputRef}
            className={styles.input}
            type="text"
            placeholder={
              userTurns === 0
                ? "e.g. \"a cheap 1440p monitor for my dorm setup\""
                : "Reply to Scout..."
            }
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
        </>
      )}

      {formError && <div className={styles.errorRow}>{formError}</div>}
    </div>
  );
}
