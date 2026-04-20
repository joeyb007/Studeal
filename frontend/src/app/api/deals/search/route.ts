import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/auth";

const API_BASE = process.env.API_BASE_URL ?? "http://localhost:8000";

export async function GET(req: NextRequest) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.redirect(new URL("/", req.url));
  }

  const { searchParams } = new URL(req.url);
  const q = searchParams.get("q");
  if (!q) return NextResponse.json([], { status: 200 });

  try {
    const res = await fetch(`${API_BASE}/deals/search?q=${encodeURIComponent(q)}&limit=20`, {
      cache: "no-store",
      headers: { Authorization: `Bearer ${session.accessToken}` },
    });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch {
    return NextResponse.json({ detail: "upstream error" }, { status: 502 });
  }
}
