import Nav from "@/components/Nav";

export const metadata = { title: "Mission Control — Studeal" };

// Placeholder route so navigation resolves; the live fleet view lands with D6.
export default function MissionControlPage() {
  return (
    <>
      <Nav />
      <main style={{ maxWidth: 1060, margin: "0 auto", padding: "26px 40px" }}>
        <h2 style={{ fontSize: 26, fontWeight: 700, letterSpacing: "-0.015em" }}>
          Mission Control
        </h2>
        <p style={{ marginTop: 8, fontSize: 13.5, color: "var(--text-secondary)" }}>
          Live agent view coming online.
        </p>
      </main>
    </>
  );
}
