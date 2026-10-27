"use client";

import { Elements, PaymentElement, useElements, useStripe } from "@stripe/react-stripe-js";
import { loadStripe, type Stripe } from "@stripe/stripe-js";
import { useEffect, useMemo, useState, type FormEvent } from "react";

type Config = { ready: boolean; publishableKey: string; checkoutOnly?: boolean };
type OnFile = {
  brand: string | null;
  last4: string | null;
  expMonth: number | null;
  expYear: number | null;
};

const appearance = {
  theme: "stripe" as const,
  variables: {
    colorPrimary: "#111111",
    colorBackground: "#ffffff",
    colorText: "#111111",
    colorTextSecondary: "#737373",
    colorDanger: "#b91c1c",
    fontFamily: "system-ui, -apple-system, sans-serif",
    borderRadius: "12px",
    fontSizeSm: "13px",
  },
  rules: {
    ".TermsText": {
      color: "#737373",
      fontSize: "13px",
    },
  },
};

function CardInner({
  email,
  onDone,
}: {
  email: string;
  onDone: (c: OnFile) => void;
}) {
  const stripe = useStripe();
  const elements = useElements();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!stripe || !elements) return;
    setBusy(true);
    setErr("");
    const { error, setupIntent } = await stripe.confirmSetup({
      elements,
      redirect: "if_required",
      confirmParams: {
        payment_method_data: {
          billing_details: { email },
        },
        return_url: `${window.location.origin}/pay?saved=1`,
      },
    });
    if (error) {
      setErr(error.message || "Could not save the card.");
      setBusy(false);
      return;
    }
    if (!setupIntent?.id) {
      setErr("Stripe did not return a setup id.");
      setBusy(false);
      return;
    }
    const res = await fetch("/api/pay/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ setupIntentId: setupIntent.id }),
    });
    const data = (await res.json()) as OnFile & { error?: string };
    if (!res.ok) {
      setErr(data.error || "Saved with Stripe, but the practice copy failed.");
      setBusy(false);
      return;
    }
    onDone(data);
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4">
      <PaymentElement
        options={{
          layout: "tabs",
          defaultValues: { billingDetails: { email } },
        }}
      />
      {err && (
        <p className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-950/40 dark:text-red-200">
          {err}
        </p>
      )}
      <button
        type="submit"
        disabled={!stripe || busy}
        className="inline-flex rounded-full bg-foreground px-5 py-2.5 text-sm font-medium text-background disabled:opacity-50"
      >
        {busy ? "Saving…" : "Save payment — no charge today"}
      </button>
    </form>
  );
}

export function SaveCardForm({
  initialEmail = "",
  initialName = "",
  onSaved,
  compact = false,
}: {
  initialEmail?: string;
  initialName?: string;
  /** Intake cascade: advance once Stripe + the practice copy are saved. */
  onSaved?: (c: OnFile) => void;
  /** Hide the email/name inputs when the cascade already knows them. */
  compact?: boolean;
}) {
  const [config, setConfig] = useState<Config | null>(null);
  const [email, setEmail] = useState(initialEmail);
  const [name, setName] = useState(initialName);
  const [clientSecret, setClientSecret] = useState("");
  const [onFile, setOnFile] = useState<OnFile | null>(null);
  const [saved, setSaved] = useState<OnFile | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const sessionId = params.get("session_id");
    if (params.get("saved") === "1" && sessionId?.startsWith("cs_")) {
      fetch("/api/pay/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sessionId }),
      })
        .then((r) => r.json())
        .then((d: OnFile & { error?: string }) => {
          if (!d.error && d.last4) setSaved(d);
        })
        .catch(() => {});
    }
    fetch("/api/pay/config", { cache: "no-store" })
      .then((r) => r.json())
      .then((d: Config) => setConfig(d))
      .catch(() => setConfig({ ready: false, publishableKey: "" }));
    if (!initialEmail) {
      fetch("/api/intake/session", { cache: "no-store" })
        .then((r) => r.json())
        .then((d: { authenticated?: boolean; email?: string; name?: string | null }) => {
          if (d.authenticated && d.email) {
            setEmail((cur) => cur || d.email || "");
            setName((cur) => cur || d.name || "");
          }
        })
        .catch(() => {});
    }
  }, [initialEmail]);

  const stripePromise = useMemo(() => {
    if (!config?.publishableKey) return null;
    return loadStripe(config.publishableKey) as Promise<Stripe | null>;
  }, [config?.publishableKey]);

  async function startSetup() {
    setBusy(true);
    setErr("");
    setSaved(null);
    try {
      const res = await fetch("/api/pay/setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, name }),
      });
      const data = (await res.json()) as {
        clientSecret?: string;
        url?: string;
        onFile?: OnFile | null;
        error?: string;
      };
      if (!res.ok) {
        setErr(data.error || "Could not start card setup.");
        return;
      }
      if (data.url) {
        window.location.href = data.url;
        return;
      }
      if (!data.clientSecret) {
        setErr("Could not start card setup.");
        return;
      }
      setOnFile(data.onFile ?? null);
      setClientSecret(data.clientSecret);
    } catch {
      setErr("Network error — try again or call the practice.");
    } finally {
      setBusy(false);
    }
  }

  if (!config) {
    return <p className="text-sm text-muted">Loading card setup…</p>;
  }
  if (!config.ready || !stripePromise) {
    return (
      <div className="space-y-3 text-sm text-muted">
        <p>
          Online card vault is not enabled on this host yet (Stripe keys not
          set). Until then, use the{" "}
          <a
            href="/forms/credit-card-auth.pdf"
            className="font-medium text-foreground underline-offset-4 hover:underline"
          >
            credit card authorization PDF
          </a>
          .
        </p>
      </div>
    );
  }

  if (saved) {
    return (
      <div className="rounded-2xl border border-border bg-surface px-5 py-8 text-center">
        <p className="text-lg font-medium text-foreground">Payment on file</p>
        <p className="mt-2 text-sm text-muted">
          {(saved.brand || "Payment").toUpperCase()} •••• {saved.last4 || "••••"}
          {saved.expMonth && saved.expYear
            ? ` · exp ${saved.expMonth}/${saved.expYear}`
            : ""}
          . No charge today. We will use this for visits and late cancellations
          per practice policy. When it expires or you want a different method,
          come back to this page.
        </p>
        {typeof window !== "undefined" &&
        new URLSearchParams(window.location.search).get("kiosk") === "1" ? (
          <p className="mt-4 text-sm font-medium text-foreground">
            That&apos;s everything — thank you. Once your personal info and consents are in,
            your visit is confirmed and we&apos;ll email you. You can close this page.
          </p>
        ) : null}
      </div>
    );
  }

  return (
    <div className="space-y-5 text-left">
      <p className="text-sm text-muted">
        Stripe holds a card (or Link) or a US bank account (ACH). We never see
        the full number or routing details. Nothing is charged until a visit or
        a late cancel. ACH takes a few days to settle.
      </p>
      <div className={compact && initialEmail ? "hidden" : "grid gap-3 sm:grid-cols-2"}>
        <label className="block text-sm">
          <span className="mb-1 block text-muted">Email on file</span>
          <input
            type="email"
            required
            autoComplete="email"
            value={email}
            onChange={(e) => {
              setEmail(e.target.value);
              setClientSecret("");
            }}
            className="w-full rounded-xl border border-border bg-background px-3 py-2 text-foreground"
          />
        </label>
        <label className="block text-sm">
          <span className="mb-1 block text-muted">Name</span>
          <input
            type="text"
            autoComplete="name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full rounded-xl border border-border bg-background px-3 py-2 text-foreground"
          />
        </label>
      </div>
      {!clientSecret ? (
        <button
          type="button"
          onClick={startSetup}
          disabled={busy || !email.includes("@")}
          className="inline-flex rounded-full bg-foreground px-5 py-2.5 text-sm font-medium text-background disabled:opacity-50"
        >
          {busy
            ? "Starting…"
            : config.checkoutOnly
              ? "Continue to secure card page"
              : compact
                ? "Add a card or bank account"
                : "Continue"}
        </button>
      ) : (
        <>
          {onFile?.last4 && (
            <p className="rounded-xl border border-border bg-surface px-3 py-2 text-sm text-muted">
              Already on file: {(onFile.brand || "card").toUpperCase()} ••••{" "}
              {onFile.last4}
              {onFile.expMonth && onFile.expYear
                ? ` exp ${onFile.expMonth}/${onFile.expYear}`
                : ""}
              . Saving below replaces that method.
            </p>
          )}
          <Elements
            stripe={stripePromise}
            options={{ clientSecret, appearance, loader: "auto" }}
          >
            <CardInner
              email={email}
              onDone={(c) => {
                setSaved(c);
                onSaved?.(c);
              }}
            />
          </Elements>
        </>
      )}
      {err && (
        <p className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-950/40 dark:text-red-200">
          {err}
        </p>
      )}
    </div>
  );
}
