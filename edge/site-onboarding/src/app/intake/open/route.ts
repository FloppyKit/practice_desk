import { NextResponse } from "next/server";
import {
  intakeCookieName,
  normalizeIntakeToken,
  verifyIntakeToken,
} from "@/lib/intake-link";

export const runtime = "nodejs";

/**
 * Redeem magic link → set httpOnly cookie → redirect to forms hub.
 */
export async function GET(req: Request) {
  const url = new URL(req.url);
  const raw = url.searchParams.get("t") || "";
  const t = normalizeIntakeToken(raw);
  const base =
    process.env.NEXT_PUBLIC_SITE_URL?.replace(/\/$/, "") ||
    `${url.protocol}//${url.host}`;

  if (!t) {
    return NextResponse.redirect(`${base}/forms?link=missing`);
  }

  try {
    const payload = verifyIntakeToken(t);
    // Optional in-flow next step (booker -> intake screens). Same-site form paths only.
    const next = url.searchParams.get("next") || "";
    const safeNext = /^\/(intake|forms\/(personal|consents|card|roi)|pay)(\?[A-Za-z0-9=&%._-]*)?$/.test(next)
      ? next
      : "";
    // Default: the guided intake cascade (resumes at the first unfinished step).
    const res = NextResponse.redirect(safeNext ? `${base}${safeNext}` : `${base}/intake`);
    res.cookies.set(intakeCookieName(), t, {
      httpOnly: true,
      secure: process.env.NODE_ENV === "production",
      sameSite: "lax",
      path: "/",
      maxAge: Math.max(60, payload.exp - Math.floor(Date.now() / 1000)),
    });
    return res;
  } catch (e) {
    console.warn("[intake/open] reject:", String(e), "tokenLen=", t.length);
    return NextResponse.redirect(`${base}/forms?link=invalid`);
  }
}
