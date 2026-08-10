"use client";

import { usePathname } from "next/navigation";

// Deterministic page-enter animation: keyed on pathname so the wrapper
// remounts on EVERY route change and .pageEnter fires each time. Replaces
// template.tsx, whose remount semantics under this Next version only cover
// its own segment level and skipped sibling navigations in practice.
export default function PageFade({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  return (
    <div key={pathname} className="pageEnter">
      {children}
    </div>
  );
}
