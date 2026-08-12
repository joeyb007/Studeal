import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/auth";

const API_BASE = process.env.API_BASE_URL ?? "http://localhost:8001";

export async function GET(req: NextRequest) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ detail: "unauthorized" }, { status: 401 });
  }
  try {
    const res = await fetch(`${API_BASE}/email/prefs`, {
      cache: "no-store",
      headers: { Authorization: `Bearer ${session.accessToken}` },
    });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch {
    return NextResponse.json({ detail: "upstream error" }, { status: 502 });
  }
}

export async function PATCH(req: NextRequest) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ detail: "unauthorized" }, { status: 401 });
  }
  const body = await req.json().catch(() => null);
  if (!body) {
    return NextResponse.json({ detail: "invalid body" }, { status: 400 });
  }
  try {
    const res = await fetch(`${API_BASE}/email/prefs`, {
      method: "PATCH",
      cache: "no-store",
      headers: {
        Authorization: `Bearer ${session.accessToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch {
    return NextResponse.json({ detail: "upstream error" }, { status: 502 });
  }
}
