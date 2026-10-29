"use client";

import Link from "next/link";
import { useEffect, useState, type FormEvent, type ReactNode } from "react";

// In-flow intake after booking: personal -> consents -> payment authorization -> save card (/pay).
const KIOSK_STEPS = [
  "/forms/personal",
  "/forms/consents",
  "/forms/card",
  "/pay",
];
const STEP_LABELS: Record<string, string> = {
  "/forms/consents": "Next: consents & policies",
  "/forms/card": "Next: payment authorization",
  "/pay": "Next: add a card on file (no charge)",
};

function kioskQuery(): string {
  if (typeof window === "undefined") return "";
  const q = new URLSearchParams(window.location.search);
  if (q.get("kiosk") !== "1") return "";
  return q.toString();
}

function KioskNext({ formId }: { formId: string }) {
  const [href, setHref] = useState("");
  useEffect(() => {
    const q = kioskQuery();
    if (!q) return;
    const here = window.location.pathname.replace(/\/$/, "");
    const i = KIOSK_STEPS.indexOf(here);
    const next = i >= 0 ? KIOSK_STEPS[i + 1] : "";
    setHref(next ? `${next}?${q}` : "");
    // Keep the flow moving: go to the next screen after a short beat.
    if (next) {
      const t = window.setTimeout(() => window.location.assign(`${next}?${q}`), 1500);
      return () => window.clearTimeout(t);
    }
  }, [formId]);
  if (!href) return null;
  const nextPath = href.split("?")[0];
  return (
    <p className="mt-6 text-sm">
      <Link
        href={href}
        className="inline-flex rounded-full bg-foreground px-4 py-2 text-sm font-medium text-background"
      >
        {STEP_LABELS[nextPath] || "Next form"} →
      </Link>
    </p>
  );
}

/** Step x of 4 + hold note while in the post-booking flow. */
export function KioskSteps() {
  const [info, setInfo] = useState<{ step: number; held: boolean } | null>(null);
  useEffect(() => {
    if (!kioskQuery()) return;
    const here = window.location.pathname.replace(/\/$/, "");
    const i = KIOSK_STEPS.indexOf(here);
    if (i < 0) return;
    const held = new URLSearchParams(window.location.search).get("held") === "1";
    setInfo({ step: i + 1, held });
  }, []);
  if (!info) return null;
  return (
    <div className="mb-6 rounded-2xl border border-border bg-surface px-4 py-3 text-left text-sm text-muted">
      <p className="font-medium text-foreground">Step {info.step} of {KIOSK_STEPS.length}</p>
      {info.held ? (
        <p className="mt-1">
          Your time is held for 48 hours while you finish. Completing these steps confirms your visit.
          If you need to stop, the link in your email or text lets you pick up where you left off.
        </p>
      ) : null}
    </div>
  );
}

function prefillFromBooking(form: HTMLFormElement) {
  const q = new URLSearchParams(window.location.search);
  if (q.get("kiosk") !== "1") return;
  if (!q.get("name") && !q.get("email")) {
    // Post-booking flow: name/email come from the personal-link session, not the URL.
    fetch("/api/intake/session", { cache: "no-store" })
      .then((r) => r.json())
      .then((d: { authenticated?: boolean; email?: string; name?: string | null }) => {
        if (!d.authenticated) return;
        const n = (d.name || "").trim();
        const [f, ...r] = n.split(/\s+/);
        const vals: Record<string, string> = {
          firstName: f || "",
          lastName: r.join(" "),
          fullName: n,
          email: d.email || "",
        };
        for (const [key, value] of Object.entries(vals)) {
          if (!value) continue;
          const el = form.elements.namedItem(key) as HTMLInputElement | null;
          if (el && "value" in el && !el.value) el.value = value;
        }
      })
      .catch(() => {});
    return;
  }
  const name = (q.get("name") || "").trim();
  const [first, ...rest] = name.split(/\s+/);
  const values: Record<string, string> = {
    firstName: first || "",
    lastName: rest.join(" "),
    fullName: name,
    email: q.get("email") || "",
    phone: q.get("phone") || "",
  };
  for (const [key, value] of Object.entries(values)) {
    if (!value) continue;
    const el = form.elements.namedItem(key) as HTMLInputElement | null;
    if (el && "value" in el && !el.value) el.value = value;
  }
}


type Props = {
  formId: string;
  title: string;
  children: ReactNode;
  /** Collect flat field map from form element */
  collect: (form: HTMLFormElement) => Record<string, string | boolean | string[]>;
  successNote?: string;
  successExtra?: ReactNode;
};

export function IntakeFormShell({
  formId,
  title,
  children,
  collect,
  successNote,
  successExtra,
}: Props) {
  const [status, setStatus] = useState<"idle" | "sending" | "ok" | "err">("idle");
  const [err, setErr] = useState("");

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    if (!form.reportValidity()) return;
    setStatus("sending");
    setErr("");
    try {
      const fields = collect(form);
      const res = await fetch("/api/intake", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ form: formId, fields }),
      });
      const data = (await res.json()) as { error?: string };
      if (!res.ok) {
        setStatus("err");
        setErr(data.error || "Submit failed");
        return;
      }
      setStatus("ok");
      form.reset();
    } catch {
      setStatus("err");
      setErr("Network error — try again or call the practice.");
    }
  }

  if (status === "ok") {
    return (
      <div className="rounded-2xl border border-border bg-surface px-5 py-8 text-center">
        <p className="text-lg font-medium text-foreground">Received — thank you</p>
        <p className="mt-2 text-sm text-muted">
          {successNote ||
            "Your answers were sent securely to the practice for review. You can close this page."}
        </p>
        {successExtra ? <div className="mt-6">{successExtra}</div> : null}
        <KioskNext formId={formId} />
        <p className="mt-6 text-sm">
          <Link
            href="/forms"
            className="site-back text-foreground underline-offset-4 hover:underline"
          >
            ← Back to forms
          </Link>
        </p>
      </div>
    );
  }

  return (
    <form
      onSubmit={onSubmit}
      ref={(node) => {
        if (node) prefillFromBooking(node);
      }}
      className="space-y-6 text-left"
    >
      <KioskSteps />
      <p className="text-xs font-medium uppercase tracking-wider text-muted">
        {title}
      </p>
      {children}
      {status === "err" && (
        <p className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-950/40 dark:text-red-200">
          {err}
        </p>
      )}
      <button
        type="submit"
        disabled={status === "sending"}
        className="w-full rounded-full bg-foreground px-5 py-3 text-sm font-medium text-background disabled:opacity-50 sm:w-auto"
      >
        {status === "sending" ? "Sending…" : "Submit to practice"}
      </button>
      <p className="text-xs text-muted">
        Submissions are emailed to the practice over encrypted mail for handoff.
        This site does not keep a clinical chart.
      </p>
    </form>
  );
}

export function Field({
  label,
  name,
  type = "text",
  required,
  placeholder,
  autoComplete,
}: {
  label: string;
  name: string;
  type?: string;
  required?: boolean;
  placeholder?: string;
  autoComplete?: string;
}) {
  return (
    <label className="block text-sm">
      <span className="font-medium text-foreground">
        {label}
        {required ? " *" : ""}
      </span>
      <input
        name={name}
        type={type}
        required={required}
        placeholder={placeholder}
        autoComplete={autoComplete}
        className="mt-1.5 w-full rounded-xl border border-border bg-background px-3 py-2.5 text-foreground"
      />
    </label>
  );
}

export function TextArea({
  label,
  name,
  required,
  rows = 3,
  defaultValue,
}: {
  label: string;
  name: string;
  required?: boolean;
  rows?: number;
  defaultValue?: string;
}) {
  return (
    <label className="block text-sm">
      <span className="font-medium text-foreground">
        {label}
        {required ? " *" : ""}
      </span>
      <textarea
        name={name}
        required={required}
        rows={rows}
        defaultValue={defaultValue}
        className="mt-1.5 w-full rounded-xl border border-border bg-background px-3 py-2.5 text-foreground"
      />
    </label>
  );
}

export function Check({
  name,
  children,
  required,
}: {
  name: string;
  children: ReactNode;
  required?: boolean;
}) {
  return (
    <label className="flex gap-3 text-sm leading-snug text-muted">
      <input
        type="checkbox"
        name={name}
        value="yes"
        required={required}
        className="mt-1 h-4 w-4 shrink-0 rounded border-border"
      />
      <span>{children}</span>
    </label>
  );
}

export function formToFields(form: HTMLFormElement): Record<string, string | boolean> {
  const fd = new FormData(form);
  const out: Record<string, string | boolean> = {};
  for (const [k, v] of fd.entries()) {
    if (typeof v !== "string") continue;
    if (form.elements.namedItem(k) instanceof RadioNodeList) {
      out[k] = v;
    } else {
      const el = form.elements.namedItem(k) as HTMLInputElement | null;
      if (el?.type === "checkbox") {
        out[k] = el.checked;
      } else {
        out[k] = v.trim();
      }
    }
  }
  // unchecked boxes
  form.querySelectorAll<HTMLInputElement>('input[type="checkbox"]').forEach((el) => {
    if (!el.name) return;
    if (!(el.name in out)) out[el.name] = false;
  });
  return out;
}
