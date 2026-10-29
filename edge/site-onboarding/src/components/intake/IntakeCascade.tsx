"use client";

/**
 * One guided intake: card on file -> personal info -> consents -> release of information (optional) -> done.
 * Resumes at the first unfinished step (server progress from the booker). Each step still posts to
 * /api/intake, so the practice-inbox email and the signed Chart/Consents PDF are unchanged.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Check, Field, TextArea, formToFields } from "@/components/IntakeFormShell";
import { SignaturePad } from "@/components/SignaturePad";
import { SaveCardForm } from "@/components/pay/SaveCardForm";
import {
  cardAuthText,
  informedConsentSections,
  practicePoliciesSections,
  privacyNoticeSummary,
  telehealthConsentSections,
} from "@/lib/intake-content";

type StepId = "card" | "personal" | "consents" | "roi" | "done";
type Flags = { card?: boolean; card_auth?: boolean; personal?: boolean; consents?: boolean; roi?: boolean };
type Progress = {
  authenticated?: boolean;
  email?: string;
  name?: string;
  flags?: Flags;
  client?: { name?: string; given_name?: string; family_name?: string; phone?: string; dob?: string } | null;
  hold?: { status?: string; when_label?: string; deadline_label?: string } | null;
};

const STEPS: { id: Exclude<StepId, "done">; label: string; optional?: boolean }[] = [
  { id: "card", label: "Card on file" },
  { id: "personal", label: "Personal info" },
  { id: "consents", label: "Consents" },
  { id: "roi", label: "Care team", optional: true },
];

const today = () => new Date().toLocaleDateString("en-CA"); // local YYYY-MM-DD

function stepDone(id: StepId, f: Flags, skipped: Set<string>): boolean {
  if (id === "card") return Boolean(f.card && f.card_auth);
  if (id === "roi") return Boolean(f.roi) || skipped.has("roi");
  return Boolean(f[id as keyof Flags]);
}

function Block({ title, sections }: { title: string; sections: readonly { heading: string; body: string }[] }) {
  return (
    <details className="rounded-2xl border border-border bg-surface p-4">
      <summary className="cursor-pointer text-sm font-semibold text-foreground">{title} — read</summary>
      <div className="mt-3 max-h-56 space-y-3 overflow-y-auto pr-1 text-sm leading-relaxed text-muted">
        {sections.map((s) => (
          <div key={s.heading}>
            <p className="font-medium text-foreground">{s.heading}</p>
            <p className="mt-1 whitespace-pre-wrap">{s.body}</p>
          </div>
        ))}
      </div>
    </details>
  );
}

function ProgressBar({ step, flags, skipped }: { step: StepId; flags: Flags; skipped: Set<string> }) {
  return (
    <ol className="mb-6 grid grid-cols-4 gap-2 text-left text-xs" aria-label="Intake progress">
      {STEPS.map((s, i) => {
        const done = stepDone(s.id, flags, skipped);
        const here = s.id === step;
        return (
          <li
            key={s.id}
            aria-current={here ? "step" : undefined}
            className={`rounded-xl border px-2 py-2 ${here ? "border-foreground" : "border-border"} ${done ? "bg-surface" : ""}`}
          >
            <span className={`block font-medium ${done ? "text-foreground" : here ? "text-foreground" : "text-muted"}`}>
              {done ? "✓" : `${i + 1}.`} {s.label}
            </span>
            <span className="text-muted">{done ? (skipped.has(s.id) ? "skipped" : "done") : here ? "now" : s.optional ? "optional" : "to do"}</span>
          </li>
        );
      })}
    </ol>
  );
}

/** One signature, captured once, adopted per document with an explicit checkbox + date (ESIGN-style). */
function SignHere({ sig, fullName, doc }: { sig: string; fullName: string; doc: string }) {
  if (!sig) {
    return (
      <>
        <SignaturePad name="signature" label="Draw your signature (used for your intake documents)" required />
        <p className="text-xs text-muted">Signing as {fullName || "you"}. You’ll only draw this once.</p>
      </>
    );
  }
  return (
    <div className="rounded-2xl border border-border bg-surface p-3 text-sm">
      <input type="hidden" name="signature" value={sig} readOnly />
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={sig} alt="Your signature" className="h-16 w-auto" />
      <Check name="adoptSignature" required>
        I, {fullName || "the signer"}, apply my signature above to the {doc}.
      </Check>
    </div>
  );
}

function StepForm({
  formId,
  onDone,
  children,
  submitLabel,
  extra,
  draftKey,
}: {
  formId: string;
  onDone: (sig: string) => void;
  children: ReactNode;
  submitLabel: string;
  extra?: ReactNode;
  draftKey: string;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [saved, setSaved] = useState(false);
  const ref = useRef<HTMLFormElement>(null);

  // Restore an unfinished draft from this device ("Save and finish later").
  useEffect(() => {
    const raw = typeof window !== "undefined" ? window.localStorage.getItem(draftKey) : null;
    if (!raw || !ref.current) return;
    try {
      const vals = JSON.parse(raw) as Record<string, string | boolean>;
      for (const [k, v] of Object.entries(vals)) {
        const el = ref.current.elements.namedItem(k) as HTMLInputElement | null;
        if (!el || k === "signature") continue;
        if (el.type === "checkbox") el.checked = Boolean(v);
        else if (!el.value) el.value = String(v);
      }
    } catch {
      /* ignore */
    }
  }, [draftKey]);

  function saveLater() {
    if (!ref.current) return;
    const vals = formToFields(ref.current);
    delete vals.signature;
    window.localStorage.setItem(draftKey, JSON.stringify(vals));
    setSaved(true);
  }

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    if (!form.reportValidity()) return;
    setBusy(true);
    setErr("");
    const fields = formToFields(form);
    try {
      const res = await fetch("/api/intake", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ form: formId, fields }),
      });
      const data = (await res.json()) as { error?: string };
      if (!res.ok) {
        setErr(data.error || "Submit failed — try again.");
        return;
      }
      window.localStorage.removeItem(draftKey);
      onDone(typeof fields.signature === "string" ? fields.signature : "");
    } catch {
      setErr("Network error — try again or call the practice.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form ref={ref} onSubmit={submit} className="space-y-5 text-left">
      {children}
      {err ? <p className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">{err}</p> : null}
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="submit"
          disabled={busy}
          className="rounded-full bg-foreground px-5 py-3 text-sm font-medium text-background disabled:opacity-50"
        >
          {busy ? "Saving…" : submitLabel}
        </button>
        {extra}
        <button type="button" onClick={saveLater} className="text-sm text-muted underline underline-offset-4">
          Save and finish later
        </button>
      </div>
      {saved ? (
        <p className="text-sm text-muted">
          Saved on this device. Finished steps are already with the practice — reopen the link in your
          email or text any time and you’ll pick up right here.
        </p>
      ) : null}
    </form>
  );
}

export function IntakeCascade() {
  const [prog, setProg] = useState<Progress | null>(null);
  const [flags, setFlags] = useState<Flags>({});
  const [step, setStep] = useState<StepId | null>(null);
  const [sig, setSig] = useState("");
  const [skipped, setSkipped] = useState<Set<string>>(new Set());
  const [roiOpen, setRoiOpen] = useState(false);
  const [held, setHeld] = useState(false);

  const load = useCallback(async () => {
    const r = await fetch("/api/intake/progress", { cache: "no-store" }).then((x) => x.json()).catch(() => ({}));
    setProg(r as Progress);
    setFlags((r as Progress).flags || {});
    return r as Progress;
  }, []);

  useEffect(() => {
    setHeld(new URLSearchParams(window.location.search).get("held") === "1");
    const s = window.sessionStorage.getItem("intake_sig");
    if (s) setSig(s);
    const sk = window.localStorage.getItem("intake_skipped");
    const skip = new Set<string>(sk ? (JSON.parse(sk) as string[]) : []);
    setSkipped(skip);
    load().then((p) => {
      const f = p.flags || {};
      const first = STEPS.find((x) => !stepDone(x.id, f, skip));
      setStep(first ? first.id : "done");
    });
  }, [load]);

  const fullName = useMemo(() => {
    const c = prog?.client;
    return (c?.name || [c?.given_name, c?.family_name].filter(Boolean).join(" ") || prog?.name || "").trim();
  }, [prog]);
  const email = prog?.email || "";
  const first = prog?.client?.given_name || fullName.split(/\s+/)[0] || "";
  const last = prog?.client?.family_name || fullName.split(/\s+/).slice(1).join(" ");
  const phone = prog?.client?.phone || "";

  const advance = useCallback(
    (from: StepId, patch: Flags, newSig?: string) => {
      if (newSig) {
        setSig(newSig);
        window.sessionStorage.setItem("intake_sig", newSig);
      }
      const f = { ...flags, ...patch };
      setFlags(f);
      const order = STEPS.map((s) => s.id);
      const next = order.slice(order.indexOf(from as Exclude<StepId, "done">) + 1).find((id) => !stepDone(id, f, skipped));
      const any = STEPS.find((s) => !stepDone(s.id, f, skipped));
      setStep(next || (any ? any.id : "done"));
      window.scrollTo({ top: 0, behavior: "smooth" });
    },
    [flags, skipped],
  );

  if (!prog || !step) return <p className="text-sm text-muted">Loading your intake…</p>;
  if (!prog.authenticated) {
    return (
      <div className="rounded-2xl border border-border bg-surface px-5 py-6 text-left text-sm text-muted">
        <p className="font-medium text-foreground">Open your personal intake link</p>
        <p className="mt-2">
          Please use the “Complete intake paperwork” link in your booking email or text. It keeps your
          progress and signs you in to this form. Need a new link? Call 773-312-3858.
        </p>
      </div>
    );
  }

  const holdNote =
    prog.hold?.status === "held" || held ? (
      <p className="mb-4 rounded-2xl border border-border bg-surface px-4 py-3 text-left text-sm text-muted">
        Your visit{prog.hold?.when_label ? ` on ${prog.hold.when_label} CT` : ""} is held
        {prog.hold?.deadline_label ? ` until ${prog.hold.deadline_label}` : " for 48 hours"}. Finishing these steps
        confirms it. About 5–10 minutes.
      </p>
    ) : null;
  const signingAs = (
    <p className="text-sm text-muted">
      Signing as <span className="font-medium text-foreground">{fullName || email}</span> · {email}
    </p>
  );
  const skipRoi = () => {
    const sk = new Set(skipped);
    sk.add("roi");
    setSkipped(sk);
    window.localStorage.setItem("intake_skipped", JSON.stringify([...sk]));
    const any = STEPS.find((s) => s.id !== "roi" && !stepDone(s.id, flags, sk));
    setStep(any ? any.id : "done");
  };
  const idFields = (
    <>
      <input type="hidden" name="fullName" value={fullName} readOnly />
      <input type="hidden" name="email" value={email} readOnly />
      <input type="hidden" name="signedDate" value={today()} readOnly />
    </>
  );

  return (
    <div className="text-left">
      {holdNote}
      <ProgressBar step={step} flags={flags} skipped={skipped} />

      {step === "card" ? (
        <section className="space-y-5">
          <h2 className="text-lg font-semibold text-foreground">1. Card on file</h2>
          <p className="text-sm text-muted">Nothing is charged today. Stripe stores the card; we never see the number.</p>
          {!flags.card_auth ? (
            <StepForm
              formId="card-auth"
              draftKey={`intake_draft_card_${email}`}
              submitLabel="Agree and add card"
              onDone={(s) => {
                if (flags.card) {
                  advance("card", { card_auth: true }, s);
                  return;
                }
                if (s) {
                  setSig(s);
                  window.sessionStorage.setItem("intake_sig", s);
                }
                setFlags({ ...flags, card_auth: true }); // stay here: add the card next
              }}
            >
              {signingAs}
              {idFields}
              <div className="max-h-40 overflow-y-auto rounded-2xl border border-border bg-surface p-4 text-sm leading-relaxed text-muted whitespace-pre-wrap">
                {cardAuthText}
              </div>
              <Check name="authorizeCharges" required>
                I authorize the practice to charge the payment method I place on file for services and for late
                cancellations (less than 24 hours’ notice).
              </Check>
              <Check name="authorizedUser" required>
                I am an authorized user of that payment method.
              </Check>
              <SignHere sig={sig} fullName={fullName} doc="payment authorization" />
            </StepForm>
          ) : (
            <SaveCardForm
              initialEmail={email}
              initialName={fullName}
              compact
              onSaved={() => advance("card", { card: true })}
            />
          )}
        </section>
      ) : null}

      {step === "personal" ? (
        <section className="space-y-5">
          <h2 className="text-lg font-semibold text-foreground">2. Personal information</h2>
          <StepForm
            formId="personal"
            draftKey={`intake_draft_personal_${email}`}
            submitLabel="Save and continue"
            onDone={() => advance("personal", { personal: true })}
          >
            <input type="hidden" name="email" value={email} readOnly />
            <input type="hidden" name="fullName" value={fullName} readOnly />
            <div className="grid gap-4 sm:grid-cols-2">
              <PrefField label="First name" name="firstName" value={first} required />
              <PrefField label="Last name" name="lastName" value={last} required />
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Date of birth" name="dob" type="date" required autoComplete="bday" />
              <PrefField label="Phone" name="phone" value={phone} type="tel" required />
            </div>
            <Field label="Street address" name="address1" required autoComplete="street-address" />
            <div className="grid gap-4 sm:grid-cols-3">
              <Field label="City" name="city" required autoComplete="address-level2" />
              <label className="block text-sm">
                <span className="font-medium text-foreground">State for visits *</span>
                <select
                  name="visitState"
                  required
                  defaultValue=""
                  className="mt-1.5 w-full rounded-xl border border-border bg-background px-3 py-2.5 text-foreground"
                >
                  <option value="" disabled>Select</option>
                  <option value="IL">Illinois</option>
                  <option value="KS">Kansas</option>
                  <option value="MO">Missouri</option>
                </select>
              </label>
              <Field label="ZIP" name="zip" required autoComplete="postal-code" />
            </div>
            <Field label="Preferred pharmacy (optional)" name="pharmacy" />
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Emergency contact name" name="emergencyName" />
              <Field label="Emergency contact phone" name="emergencyPhone" type="tel" />
            </div>
            <TextArea label="Anything we should know before the first visit? (optional)" name="notes" rows={2} />
          </StepForm>
        </section>
      ) : null}

      {step === "consents" ? (
        <section className="space-y-4">
          <h2 className="text-lg font-semibold text-foreground">3. Consents & policies</h2>
          <StepForm
            formId="consents"
            draftKey={`intake_draft_consents_${email}`}
            submitLabel="Sign and continue"
            onDone={(s) => advance("consents", { consents: true }, s)}
          >
            {signingAs}
            {idFields}
            <Block title="Informed consent (therapy & medication)" sections={informedConsentSections} />
            <Check name="agreeInformed" required>I agree to the informed consent for psychotherapy and medication management.</Check>
            <Block title="Telehealth consent" sections={telehealthConsentSections} />
            <Check name="agreeTelehealth" required>I agree to telehealth (video) care; it is not for emergencies (call 911).</Check>
            <Block title="Practice policies" sections={practicePoliciesSections} />
            <Check name="agreePolicies" required>I agree to the practice policies (24-hour cancellation, out-of-network payment).</Check>
            <Block title="Notice of privacy practices (summary)" sections={privacyNoticeSummary} />
            <Check name="ackPrivacy" required>
              I acknowledge I was offered the Notice of Privacy Practices (
              <a href="/forms/handouts/privacy-notice.pdf" target="_blank" rel="noreferrer" className="underline">PDF</a>).
            </Check>
            <SignHere sig={sig} fullName={fullName} doc="consents and policies above" />
          </StepForm>
        </section>
      ) : null}

      {step === "roi" ? (
        <section className="space-y-4">
          <h2 className="text-lg font-semibold text-foreground">4. Connect your care team</h2>
          {!roiOpen ? (
            <div className="space-y-4 rounded-2xl border border-border bg-surface p-5">
              <p className="text-base font-medium text-foreground">
                Want us to stay in touch with your primary care doctor or therapist?
              </p>
              <ul className="list-disc space-y-1.5 pl-5 text-sm text-muted">
                <li>Your doctors stay on the same page about medications, so nothing conflicts.</li>
                <li>We can request past records instead of asking you to repeat your history.</li>
                <li>You choose who, and what is shared. You can change your mind anytime, in writing.</li>
              </ul>
              <p className="text-xs text-muted">Optional. It takes about a minute, and it doesn’t affect your visit.</p>
              <div className="flex flex-wrap items-center gap-4">
                <button
                  type="button"
                  onClick={() => setRoiOpen(true)}
                  className="rounded-full bg-foreground px-6 py-3 text-sm font-medium text-background"
                >
                  Add my provider
                </button>
                <button type="button" onClick={skipRoi} className="text-sm text-muted underline underline-offset-4">
                  Skip for now
                </button>
              </div>
            </div>
          ) : (
            <StepForm
              formId="roi"
              draftKey={`intake_draft_roi_${email}`}
              submitLabel="Sign this authorization"
              onDone={(s) => advance("roi", { roi: true }, s)}
              extra={
                <button type="button" onClick={skipRoi} className="text-sm text-muted underline underline-offset-4">
                  Skip for now
                </button>
              }
            >
              {signingAs}
              {idFields}
              <input type="hidden" name="firstName" value={first} readOnly />
              <input type="hidden" name="lastName" value={last} readOnly />
              <input type="hidden" name="relationship" value="Self" readOnly />
              <Field label="Provider or clinic name (e.g. your primary care doctor or therapist)" name="otherParty" required />
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Their phone" name="otherPhone" type="tel" />
                <Field label="Their email or fax" name="otherEmail" />
              </div>
              <div className="flex flex-wrap gap-4 text-sm">
                <label className="flex items-center gap-2"><input type="checkbox" name="canSend" value="yes" defaultChecked className="h-4 w-4" />We may send them information</label>
                <label className="flex items-center gap-2"><input type="checkbox" name="canReceive" value="yes" defaultChecked className="h-4 w-4" />They may send us information</label>
              </div>
              <TextArea
                label="What may be shared, and why"
                name="purpose"
                required
                rows={2}
                defaultValue="Diagnoses, medications, and treatment summaries, to coordinate my care."
              />
              <label className="block text-sm">
                <span className="font-medium text-foreground">This authorization ends *</span>
                <select name="expires" required defaultValue="1 year from the date signed" className="mt-1.5 w-full rounded-xl border border-border bg-background px-3 py-2.5 text-foreground">
                  <option>1 year from the date signed</option>
                  <option>When my treatment with this practice ends</option>
                </select>
              </label>
              <Check name="understandRoi" required>
                I understand this authorization is voluntary; my treatment, payment and eligibility do not depend on
                signing it; I may revoke it in writing at any time (except for information already shared); and
                information shared with a recipient who is not covered by HIPAA may be re-disclosed and no longer
                protected. I may request a copy of this signed form.
              </Check>
              <p className="text-sm text-muted">Date signed: {today()}</p>
              <SignHere sig={sig} fullName={fullName} doc="authorization to release information (dated today)" />
            </StepForm>
          )}
        </section>
      ) : null}

      {step === "done" ? (
        <section className="rounded-2xl border border-border bg-surface px-5 py-8 text-center">
          <p className="text-lg font-medium text-foreground">You’re all set — thank you</p>
          <p className="mt-2 text-sm text-muted">
            Card on file, personal info and consents are with the practice.
            {prog.hold ? " Your visit is confirmed; a confirmation email is on the way." : ""} You can close this page.
          </p>
        </section>
      ) : null}
    </div>
  );
}

function PrefField({ label, name, value, type = "text", required }: { label: string; name: string; value: string; type?: string; required?: boolean }) {
  return (
    <label className="block text-sm">
      <span className="font-medium text-foreground">{label}{required ? " *" : ""}</span>
      <input
        name={name}
        type={type}
        required={required}
        defaultValue={value}
        className="mt-1.5 w-full rounded-xl border border-border bg-background px-3 py-2.5 text-foreground"
      />
    </label>
  );
}
