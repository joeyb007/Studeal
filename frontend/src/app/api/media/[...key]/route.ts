import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/auth";

const API_BASE = process.env.API_BASE_URL ?? "http://localhost:8001";

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ key: string[] }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
  }
  const { key } = await params;
  try {
    const res = await fetch(`${API_BASE}/media/${key.join("/")}`, {
      headers: { Authorization: `Bearer ${session.accessToken}` },
      cache: "no-store",
    });
    if (!res.ok) {
      return NextResponse.json({ detail: "not found" }, { status: res.status });
    }
    const bytes = await res.arrayBuffer();
    return new NextResponse(bytes, {
      headers: {
        "Content-Type": res.headers.get("Content-Type") ?? "image/jpeg",
        "Cache-Control": "private, max-age=86400",
      },
    });
  } catch {
    return NextResponse.json({ detail: "upstream error" }, { status: 502 });
  }
}
