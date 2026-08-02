"use client";

import { useEffect, useState } from "react";
import styles from "./Toast.module.css";

// Reusable top-right notifications. Mounted once in the root layout (outside
// the page-transition wrapper, so position:fixed is never transform-trapped);
// fired from anywhere via `toast("Deployed", "success")` — no context
// plumbing, no re-render of the caller.

type ToastType = "success" | "error" | "info";

interface ToastItem {
  id: number;
  message: string;
  type: ToastType;
  leaving?: boolean;
}

const DISMISS_MS = 4200;
const LEAVE_MS = 250;

let pushToast: ((item: ToastItem) => void) | null = null;

export function toast(message: string, type: ToastType = "info") {
  pushToast?.({ id: Date.now() + Math.random(), message, type });
}

export default function Toaster() {
  const [items, setItems] = useState<ToastItem[]>([]);

  useEffect(() => {
    pushToast = item => {
      setItems(prev => [...prev, item]);
      setTimeout(() => {
        setItems(prev => prev.map(t => (t.id === item.id ? { ...t, leaving: true } : t)));
        setTimeout(() => {
          setItems(prev => prev.filter(t => t.id !== item.id));
        }, LEAVE_MS);
      }, DISMISS_MS);
    };
    return () => {
      pushToast = null;
    };
  }, []);

  if (items.length === 0) return null;

  return (
    <div className={styles.stack} role="status" aria-live="polite">
      {items.map(item => (
        <button
          key={item.id}
          className={[
            styles.toast,
            styles[item.type],
            item.leaving ? styles.toastLeaving : "",
          ].join(" ")}
          onClick={() =>
            setItems(prev => prev.filter(t => t.id !== item.id))
          }
        >
          <span className={styles.toastDot} />
          {item.message}
        </button>
      ))}
    </div>
  );
}
