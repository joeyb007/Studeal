import { NextResponse } from "next/server";
import { auth } from "@/auth";

const API_BASE = process.env.API_BASE_URL ?? "http://localhost:8001";

// Lightweight session validity probe for the SessionSentinel: proxies the
// API's /auth/me so an expired backend token surfaces as a clean 401 here.
// (Cannot live under /api/auth/* — that namespace belongs to NextAuth.)
export async function GET() {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
  }
  try {
    const res = await fetch(`${API_BASE}/auth/me`, {
      headers: { Authorization: `Bearer ${session.accessToken}` },
      cache: "no-store",
    });
    return NextResponse.json({ ok: res.ok }, { status: res.status });
  } catch {
    return NextResponse.json({ detail: "upstream error" }, { status: 502 });
  }
}
