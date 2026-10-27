"use client";

import Link from "next/link";
import {
  Check,
  Field,
  formToFields,
  IntakeFormShell,
} from "@/components/IntakeFormShell";
import { SignaturePad } from "@/components/SignaturePad";
import { cardAuthText } from "@/lib/intake-content";

/** Outside the post-booking flow: plain link. In the flow, KioskNext shows the next step. */
function PayNextLink() {
  if (typeof window !== "undefined" && new URLSearchParams(window.location.search).get("kiosk") === "1") {
    return null;
  }
  return (
    <Link
      href="/pay"
      className="inline-flex rounded-full bg-foreground px-4 py-2 text-sm font-medium text-background"
    >
      Save payment next →
    </Link>
  );
}

export function CardAuthForm() {
  return (
    <IntakeFormShell
      formId="card-auth"
      title="Payment authorization"
      collect={formToFields}
      successNote="Authorization recorded. Next: save a card or bank account on the secure payment page — no charge today."
      successExtra={<PayNextLink />}
    >
      <div className="max-h-64 overflow-y-auto rounded-2xl border border-border bg-surface p-4 text-sm leading-relaxed text-muted whitespace-pre-wrap">
        {cardAuthText}
      </div>
      <p className="text-sm text-muted">
        Prefer a digital vault (no card number typed here):{" "}
        <Link href="/pay" className="text-foreground underline-offset-4 hover:underline">
          secure card setup
        </Link>
        .
      </p>
      <Check name="authorizeCharges" required>
        I authorize the practice to charge the payment method I place on file for
        services and for late cancellations (less than 24 hours’ notice), as
        described above.
      </Check>
      <Check name="authorizedUser" required>
        I am an authorized user of that payment method and will keep the practice
        informed of changes.
      </Check>
      <Field label="Type full legal name" name="fullName" required />
      <SignaturePad name="signature" label="Draw your signature" required />
      <Field label="Date" name="signedDate" type="date" required />
      <Field
        label="Email for receipt / follow-up"
        name="email"
        type="email"
        required
        autoComplete="email"
      />
    </IntakeFormShell>
  );
}
