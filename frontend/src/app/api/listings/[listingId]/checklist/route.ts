import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/auth";

const API_BASE = process.env.API_BASE_URL ?? "http://localhost:8001";

export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ listingId: string }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
  }
  const { listingId } = await params;
  const watchlistId = req.nextUrl.searchParams.get("watchlist_id");
  const query = watchlistId ? `?watchlist_id=${watchlistId}` : "";
  try {
    const res = await fetch(
      `${API_BASE}/listings/${listingId}/checklist${query}`,
      {
        headers: { Authorization: `Bearer ${session.accessToken}` },
        cache: "no-store",
        // First open may run the seed assessment call.
        signal: AbortSignal.timeout(30_000),
      },
    );
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch {
    return NextResponse.json({ detail: "upstream error" }, { status: 502 });
  }
}

export async function PATCH(
  req: NextRequest,
  { params }: { params: Promise<{ listingId: string }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
  }
  const { listingId } = await params;
  try {
    const body = await req.json();
    const res = await fetch(`${API_BASE}/listings/${listingId}/checklist`, {
      method: "PATCH",
      headers: {
        Authorization: `Bearer ${session.accessToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch {
    return NextResponse.json({ detail: "upstream error" }, { status: 502 });
  }
}
