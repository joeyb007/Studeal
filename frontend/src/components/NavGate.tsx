"use client";

import { usePathname } from "next/navigation";
import Nav from "./Nav";
import SessionSentinel from "./SessionSentinel";

// App chrome is stable: Nav mounts once here (root layout, OUTSIDE the
// page-transition template) so it never re-animates or remounts on
// navigation — which also keeps its unread-badge poll alive across pages.
// Marketing/auth routes stay nav-free.
const APP_PREFIXES = ["/dashboard", "/watchlists", "/mission-control", "/catalog", "/scout"];

export default function NavGate() {
  const pathname = usePathname();
  const inApp = APP_PREFIXES.some((prefix) => pathname.startsWith(prefix));
  if (!inApp) return null;
  return (
    <>
      <Nav />
      <SessionSentinel />
    </>
  );
}
