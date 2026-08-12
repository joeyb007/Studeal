import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/auth";

const API_BASE = process.env.API_BASE_URL ?? "http://localhost:8001";

// Account actions multiplexed by body.action:
//   reset-request / reset-confirm — public (no session)
//   change-password / delete-account — require the signed-in session
const PUBLIC_ACTIONS = new Set(["reset-request", "reset-confirm"]);
const AUTHED_ACTIONS = new Set(["change-password", "delete-account"]);

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => null);
  const action = body?.action;
  if (!body || typeof action !== "string" ||
      (!PUBLIC_ACTIONS.has(action) && !AUTHED_ACTIONS.has(action))) {
    return NextResponse.json({ detail: "invalid action" }, { status: 400 });
  }

  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (AUTHED_ACTIONS.has(action)) {
    const session = await auth();
    if (!session?.accessToken) {
      return NextResponse.json({ detail: "unauthorized" }, { status: 401 });
    }
    headers.Authorization = `Bearer ${session.accessToken}`;
  }

  const { action: _drop, ...payload } = body;
  try {
    const res = await fetch(`${API_BASE}/auth/${action}`, {
      method: "POST",
      cache: "no-store",
      headers,
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch {
    return NextResponse.json({ detail: "upstream error" }, { status: 502 });
  }
}
