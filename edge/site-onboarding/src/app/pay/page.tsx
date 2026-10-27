import type { Metadata } from "next";
import Link from "next/link";
import { PageShell } from "@/components/PageShell";
import { SaveCardForm } from "@/components/pay/SaveCardForm";
import { KioskSteps } from "@/components/IntakeFormShell";
import { site } from "@/lib/content";

export const metadata: Metadata = {
  title: `Payment on file | ${site.name}`,
  description: "Securely save a card or bank account for visits with Dr. Matt Brown.",
};

export default function PayPage() {
  return (
    <PageShell
      crumbs={[
        { href: "/", label: "Home" },
        { href: "/forms", label: "Forms" },
        { label: "Card" },
      ]}
      title="Payment on file"
      intro="Save a card or bank account (ACH) for visits and late cancellations. Nothing is charged when you save."
    >
      <KioskSteps />
      <SaveCardForm />
      <p className="mt-8 text-left text-sm leading-relaxed text-muted">
        Bitcoin is for a specific visit, not a stored method — there is no “charge later”
        in sats. If you want to pay a visit in BTC, say so at booking or email the
        practice; we send a one-time invoice (usually Lightning, USD amount).
      </p>
      <p className="mt-4 text-left text-sm text-muted">
        Authorization language lives on the{" "}
        <Link href="/forms/card" className="text-foreground underline-offset-4 hover:underline">
          payment authorization form
        </Link>
        .{" "}
        <Link href="/forms" className="text-foreground underline-offset-4 hover:underline">
          ← All intake forms
        </Link>
      </p>
    </PageShell>
  );
}
