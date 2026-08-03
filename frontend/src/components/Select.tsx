"use client";

import { useEffect, useRef, useState } from "react";
import styles from "./Select.module.css";

// Custom dropdown: the native <select> popup is OS-rendered and unstylable,
// so the menu is ours. Click-outside and Escape close; basic listbox semantics.

export interface SelectOption {
  value: string;
  label: string;
}

// Shared menu state: `closing` keeps the menu mounted through its exit
// animation instead of unmounting on the same frame it closes.
function useMenu() {
  const [open, setOpen] = useState(false);
  const [closing, setClosing] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  const close = () => {
    setOpen(false);
    setClosing(true);
  };
  const toggle = () => {
    if (open) {
      close();
    } else {
      setClosing(false);
      setOpen(true);
    }
  };

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        close();
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const onMenuAnimationEnd = () => {
    if (closing) setClosing(false);
  };

  return { open, closing, rootRef, close, toggle, onMenuAnimationEnd };
}

export default function Select({
  value,
  onChange,
  options,
  allLabel,
}: {
  value: string | null;
  onChange: (value: string | null) => void;
  options: SelectOption[];
  allLabel: string;
}) {
  const { open, closing, rootRef, close, toggle, onMenuAnimationEnd } = useMenu();

  const current = options.find(o => o.value === value)?.label ?? allLabel;

  return (
    <div className={styles.root} ref={rootRef}>
      <button
        type="button"
        className={[styles.trigger, open ? styles.triggerOpen : ""].join(" ")}
        onClick={toggle}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span className={styles.triggerLabel}>{current}</span>
        <svg className={styles.chevron} width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><path d="m6 9 6 6 6-6"/></svg>
      </button>
      {(open || closing) && (
        <div
          className={[styles.menu, closing ? styles.menuOut : ""].join(" ")}
          role="listbox"
          onAnimationEnd={onMenuAnimationEnd}
        >
          <button
            type="button"
            role="option"
            aria-selected={value === null}
            className={[styles.option, value === null ? styles.optionActive : ""].join(" ")}
            onClick={() => { onChange(null); close(); }}
          >
            {allLabel}
          </button>
          {options.map(option => (
            <button
              key={option.value}
              type="button"
              role="option"
              aria-selected={value === option.value}
              className={[styles.option, value === option.value ? styles.optionActive : ""].join(" ")}
              onClick={() => { onChange(option.value); close(); }}
            >
              {option.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function MultiSelect({
  values,
  onChange,
  options,
  allLabel,
}: {
  values: string[];
  onChange: (values: string[]) => void;
  options: SelectOption[];
  allLabel: string;
}) {
  const { open, closing, rootRef, close, toggle: toggleMenu, onMenuAnimationEnd } = useMenu();

  const label =
    values.length === 0
      ? allLabel
      : values.length === 1
        ? options.find(o => o.value === values[0])?.label ?? values[0]
        : `${options.find(o => o.value === values[0])?.label ?? values[0]} +${values.length - 1}`;

  const toggleValue = (value: string) =>
    onChange(
      values.includes(value) ? values.filter(v => v !== value) : [...values, value],
    );

  return (
    <div className={styles.root} ref={rootRef}>
      <button
        type="button"
        className={[styles.trigger, open ? styles.triggerOpen : ""].join(" ")}
        onClick={toggleMenu}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span className={styles.triggerLabel}>{label}</span>
        <svg className={styles.chevron} width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><path d="m6 9 6 6 6-6"/></svg>
      </button>
      {(open || closing) && (
        <div
          className={[styles.menu, closing ? styles.menuOut : ""].join(" ")}
          role="listbox"
          aria-multiselectable
          onAnimationEnd={onMenuAnimationEnd}
        >
          <button
            type="button"
            role="option"
            aria-selected={values.length === 0}
            className={[styles.option, values.length === 0 ? styles.optionActive : ""].join(" ")}
            onClick={() => { onChange([]); close(); }}
          >
            {allLabel}
          </button>
          {options.map(option => {
            const active = values.includes(option.value);
            return (
              <button
                key={option.value}
                type="button"
                role="option"
                aria-selected={active}
                className={[styles.option, styles.optionCheck, active ? styles.optionActive : ""].join(" ")}
                onClick={() => toggleValue(option.value)}
              >
                <span className={[styles.checkbox, active ? styles.checkboxOn : ""].join(" ")}>
                  {active && (
                    <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6 9 17l-5-5"/></svg>
                  )}
                </span>
                {option.label}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
