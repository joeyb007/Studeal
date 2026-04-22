import { NextResponse } from "next/server";
import { auth } from "@/auth";

const API_BASE = process.env.API_BASE_URL ?? "http://localhost:8000";

export async function POST() {
  const session = await auth();
  if (!session?.accessToken) return NextResponse.redirect("/");

  const res = await fetch(`${API_BASE}/billing/checkout`, {
    method: "POST",
    headers: { Authorization: `Bearer ${session.accessToken}` },
  });

  if (!res.ok) return NextResponse.json({ error: "Failed" }, { status: 502 });
  return NextResponse.json(await res.json());
}
