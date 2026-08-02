// Re-keyed on every navigation (unlike layout), so the enter animation fires
// on each page transition — same feel as Daily Drops' fade-in, applied
// app-wide. Animation lives in globals.css (.pageEnter).
export default function Template({ children }: { children: React.ReactNode }) {
  return <div className="pageEnter">{children}</div>;
}
