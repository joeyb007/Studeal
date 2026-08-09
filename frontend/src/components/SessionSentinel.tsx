"use client";

import { useEffect, useRef } from "react";
import { signOut } from "next-auth/react";
import { toast } from "./Toast";

// The silent-401 killer: a stale backend token used to leave every page
// quietly empty ("the pool is quiet", missing agents) with no hint why.
// This probes session validity on mount, on window focus, and on a slow
// interval; a dead token gets one honest toast and a clean trip to login.

const CHECK_INTERVAL_MS = 5 * 60_000;

export default function SessionSentinel() {
  const expired = useRef(false);

  useEffect(() => {
    const check = async () => {
      if (expired.current) return;
      try {
        const res = await fetch("/api/me", { cache: "no-store" });
        if (res.status === 401) {
          expired.current = true;
          toast("Your session expired. Sending you back to log in.", "info");
          setTimeout(() => signOut({ callbackUrl: "/login" }), 1_200);
        }
      } catch {
        /* network blips are not session death */
      }
    };

    check();
    const onFocus = () => check();
    window.addEventListener("focus", onFocus);
    const interval = setInterval(check, CHECK_INTERVAL_MS);
    return () => {
      window.removeEventListener("focus", onFocus);
      clearInterval(interval);
    };
  }, []);

  return null;
}
