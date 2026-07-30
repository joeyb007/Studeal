import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/auth";

const API_BASE = process.env.API_BASE_URL ?? "http://localhost:8001";

// SSE proxy: the browser's EventSource hits this route with its session
// cookie; we attach the backend JWT server-side (as a query param, per the
// backend contract — EventSource cannot set headers) and pipe bytes through.
// req.signal propagates client disconnect so the backend stream tears down.
export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ watchlistId: string }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
  }
  const { watchlistId } = await params;

  let upstream: Response;
  try {
    upstream = await fetch(
      `${API_BASE}/stream/watchlists/${watchlistId}?token=${encodeURIComponent(session.accessToken)}`,
      { cache: "no-store", signal: req.signal }
    );
  } catch {
    return NextResponse.json({ detail: "upstream error" }, { status: 502 });
  }

  if (!upstream.ok || !upstream.body) {
    return NextResponse.json(
      { detail: "upstream refused stream" },
      { status: upstream.status || 502 }
    );
  }

  return new Response(upstream.body, {
    status: 200,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}
