import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { intakeCookieName, verifyIntakeToken } from "@/lib/intake-link";

export const runtime = "nodejs";

const BOOKER = (process.env.BOOKER_INTERNAL_URL?.trim() || "http://127.0.0.1:8090").replace(/\/$/, "");

/** Cascade progress for the personal-link session: done steps + prefill. Never trusts a client email. */
export async function GET() {
  const jar = await cookies();
  const t = jar.get(intakeCookieName())?.value;
  if (!t) return NextResponse.json({ ok: false, authenticated: false });
  let email = "";
  let name = "";
  try {
    const p = verifyIntakeToken(t);
    email = p.email;
    name = p.name || "";
  } catch {
    return NextResponse.json({ ok: false, authenticated: false });
  }
  const secret = process.env.BOOKER_STAFF_SECRET?.trim() || "";
  let data: Record<string, unknown> = {};
  try {
    const res = await fetch(`${BOOKER}/api/internal/intake/progress?email=${encodeURIComponent(email)}`, {
      headers: { "X-Staff-Secret": secret },
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    if (res.ok) data = (await res.json()) as Record<string, unknown>;
  } catch {
    /* progress unknown: start at step 1 */
  }
  return NextResponse.json({ ok: true, authenticated: true, email, name, ...data });
}
