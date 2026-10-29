import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { intakeCookieName, verifyIntakeToken } from "@/lib/intake-link";
import { emailIntakeSubmission } from "@/lib/intake-notify";
import { postToBooker } from "@/lib/booker-intake";

export const runtime = "nodejs";

const ALLOWED = new Set([
  "personal",
  "consents",
  "roi",
  "card-auth",
  "intake-link",
]);

/** ~400KB — signature PNG data-URLs are base64 and can be large on retina pads */
const MAX_BODY = 400_000;

export async function POST(req: Request) {
  let body: {
    form?: string;
    fields?: Record<string, string | boolean | string[]>;
  };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON" }, { status: 400 });
  }

  const form = body.form?.trim() || "";
  if (!ALLOWED.has(form)) {
    return NextResponse.json({ error: "Unknown form" }, { status: 400 });
  }
  if (!body.fields || typeof body.fields !== "object") {
    return NextResponse.json({ error: "Missing fields" }, { status: 400 });
  }

  const serialized = JSON.stringify(body.fields);
  if (serialized.length > MAX_BODY) {
    return NextResponse.json({ error: "Submission too large" }, { status: 400 });
  }

  // Require drawn signature on signing forms
  if (form === "consents" || form === "roi" || form === "card-auth") {
    const sig = body.fields.signature;
    if (typeof sig !== "string" || !sig.startsWith("data:image/")) {
      return NextResponse.json(
        { error: "Please draw your signature before submitting." },
        { status: 400 },
      );
    }
  }

  const ip =
    req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ||
    req.headers.get("x-real-ip") ||
    undefined;
  const userAgent = req.headers.get("user-agent") || undefined;

  // Magic-link session (mailbox control proof)
  let linkAuth: {
    verified: boolean;
    email?: string;
    name?: string;
    issuedAt?: number;
    expiresAt?: number;
  } = { verified: false };

  try {
    const jar = await cookies();
    const t = jar.get(intakeCookieName())?.value;
    if (t) {
      const p = verifyIntakeToken(t);
      linkAuth = {
        verified: true,
        email: p.email,
        name: p.name,
        issuedAt: p.iat,
        expiresAt: p.exp,
      };
    }
  } catch {
    linkAuth = { verified: false };
  }

  const result = await emailIntakeSubmission({
    form,
    fields: body.fields,
    meta: { ip, userAgent, linkAuth },
  });

  if (!result.ok) {
    console.warn("[intake]", result.detail);
    return NextResponse.json(
      {
        error:
          "Could not deliver to the practice mailbox. Please try again or call 773-312-3858.",
        detail: result.detail,
      },
      { status: 502 },
    );
  }

  // Practice email above is unchanged. Also record completion + file the signed PDF (booker).
  let complete = false;
  if (form !== "intake-link") {
    const b = await postToBooker("/api/internal/intake/submitted", {
      form,
      fields: body.fields,
      meta: { ip, userAgent, linkAuth },
    });
    if (!b.ok) console.warn("[intake] booker record failed:", b.detail);
    complete = Boolean((b.data as { complete?: boolean } | undefined)?.complete);
  }

  return NextResponse.json({
    ok: true,
    linkVerified: linkAuth.verified,
    complete,
  });
}
