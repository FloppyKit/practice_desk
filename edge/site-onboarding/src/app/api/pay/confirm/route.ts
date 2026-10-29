import { NextResponse } from "next/server";
import { emailCardOnFile } from "@/lib/pay-notify";
import { getStripe, stripeReady, summarizePm } from "@/lib/stripe";
import { recordCardOnFile } from "@/lib/stripe-store";
import { postToBooker } from "@/lib/booker-intake";

export async function POST(req: Request) {
  if (!stripeReady()) {
    return NextResponse.json({ error: "Card vault is not enabled yet." }, { status: 503 });
  }
  let body: { setupIntentId?: string; sessionId?: string };
  try {
    body = (await req.json()) as { setupIntentId?: string; sessionId?: string };
  } catch {
    return NextResponse.json({ error: "Bad request" }, { status: 400 });
  }
  const stripe = getStripe();
  let setupIntentId = (body.setupIntentId || "").trim();
  if (!setupIntentId && body.sessionId?.startsWith("cs_")) {
    const session = await stripe.checkout.sessions.retrieve(body.sessionId);
    const sid =
      typeof session.setup_intent === "string"
        ? session.setup_intent
        : session.setup_intent?.id;
    if (sid) setupIntentId = sid;
  }
  if (!setupIntentId.startsWith("seti_")) {
    return NextResponse.json({ error: "Missing setup intent." }, { status: 400 });
  }

  const si = await stripe.setupIntents.retrieve(setupIntentId, { expand: ["payment_method"] });
  if (si.status !== "succeeded") {
    return NextResponse.json({ error: `Setup not complete (${si.status}).` }, { status: 409 });
  }
  const customerId = typeof si.customer === "string" ? si.customer : si.customer?.id;
  const pm =
    typeof si.payment_method === "string"
      ? await stripe.paymentMethods.retrieve(si.payment_method)
      : si.payment_method;
  if (!customerId || !pm || typeof pm === "string") {
    return NextResponse.json({ error: "No payment method on the setup." }, { status: 422 });
  }

  await stripe.customers.update(customerId, {
    invoice_settings: { default_payment_method: pm.id },
  });

  const summary = summarizePm(pm);
  const email = (si.metadata?.email || pm.billing_details.email || "").toLowerCase();
  const name = si.metadata?.name || pm.billing_details.name || undefined;
  if (email) {
    await recordCardOnFile({
      customerId,
      email,
      name,
      ...summary,
    });
    // Booker phone book is the desk's source of truth (card flag + held new-client visits).
    const b = await postToBooker("/api/internal/client/card", {
      email,
      customer_id: customerId,
      payment_method_id: summary.paymentMethodId || "",
      brand: summary.brand || "",
      last4: summary.last4 || "",
      exp_month: summary.expMonth,
      exp_year: summary.expYear,
    });
    if (!b.ok) console.warn("[pay/confirm] booker card record failed:", b.detail);
    await emailCardOnFile({
      email,
      name,
      brand: summary.brand,
      last4: summary.last4,
      expMonth: summary.expMonth,
      expYear: summary.expYear,
    });
  }

  return NextResponse.json({
    ok: true,
    brand: summary.brand,
    last4: summary.last4,
    expMonth: summary.expMonth,
    expYear: summary.expYear,
  });
}
