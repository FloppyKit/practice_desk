# GO: One-click billing (invoice + superbill)

Status: built 2026-10-20 on branch `billing-one-click` (see "Matt's decisions" and "As built" at the end). Plan text below is the original; the decisions override it.

**Goal.** On a visit, Matt clicks **Bill visit**, confirms the code, and the desk makes the invoice and the superbill together.

## What already exists (reuse it)
- `app/practice.py` service presets already split $265 / $420 / $600 into CPT lines (99213 $220 + 90833 $45, or + 90838 $200; 90792 $600). POS 10 and modifier 95 are already the defaults.
- `app/superbill_pdf.py` and `POST /api/desk/superbill` write `{Billing}/Superbill M-D-YYYY.pdf` to Drive. It has text only: no logo, no letterhead block.
- `app/fees.py` (fee ledger) and `/api/desk/fees`, `/fees/invoice` (Stripe invoice and pay link), plus `charge_off_session` for a card on file.
- Desk client card (`static/desk.html`, `hm-*` buttons) already has Charge, Copy pay link, and Superbill.
- `config/event-types.yaml`: new-client is 90 min / $600; follow-up is 30 or 60 min, $265 / $420.

## 1. Config: `config/billing-codes.yaml` (one source of truth)
```yaml
pos: "10"
visit_types:
  fu30: { label: "Follow-up, 30 min", price: 265, invoice_code: "FU30",
          cpt: [{code: "99213", mod: "95", fee: 220}, {code: "90833", mod: "95", fee: 45}] }
  fu60: { label: "Follow-up, 60 min", price: 420, invoice_code: "FU60",
          cpt: [{code: "99213", mod: "95", fee: 220}, {code: "90838", mod: "95", fee: 200}] }
  np90: { label: "New patient eval", price: 600, invoice_code: "NP90",
          cpt: [{code: "90792", mod: "95", fee: 600}] }
```
- Invoice codes (FU30, FU60, NP90) are ours and never print as CPT codes.
- CPT fees must add up to the price; a test checks this.
- The visit is mapped from its event type and duration (new-client → np90; follow-up 30 or 60 → fu30 or fu60). `practice.py` presets read from this file instead of their own copy.

## 2. The button
On the visit row in the week view and on the client card: **Bill visit**. It opens a confirm sheet. It is never automatic. The old Charge and Superbill buttons stay for odd cases.

## 3. Confirm step (required)
The sheet shows: client, date of service, guessed visit code (fu30, fu60 or np90, which Matt can change), price, CPT lines with -95, POS 10, ICD-10 codes, and how it will be delivered.
- Nothing is written or sent until Matt clicks **Confirm**.
- Each visit (event_id) can be billed once; billing it again shows the existing invoice and superbill.

## 4. Invoice
- **Spruce has no API for invoices or payment requests.** The public API only covers contacts, conversations, messages, media, phone lines, teams and webhooks. "paymentRequest" appears only as a read-only attachment type on conversation items; requests are made by hand in the Spruce app ($ icon, secure conversations only).
- **Plan:**
  1. Write the fee row (`fees.create`, kind=visit, event_id, invoice_code in the note).
  2. **Default:** the desk shows a "Send $X request in Spruce" task with the amount copied. Matt sends it in Spruce with the $ icon, then marks the fee sent or paid.
  3. **Optional:** create a Stripe invoice or pay link with the existing `/fees/invoice` and post that link as a Spruce secure message through the API (`POST /conversations/{id}/messages`).
- When the Kansas entity is live, swap the provider behind one `invoice_provider` setting.

## 5. Superbill PDF on letterhead
- Extend `superbill_pdf.py`: logo plus the letterhead block from the practice store (legal name, address, phone, EIN or tax ID, NPI, licenses).
- Fields: provider name and NPI, tax ID, client name and DOB, DOS, POS 10, CPT plus 95 on each line, ICD-10 pointers, fees, total paid.
- Save to Drive Billing as today. If Matt opts in, also attach it to a Spruce secure message (`POST /media`, then a message with the attachment).
- Store gaps on do-edge: **address, EIN and licenses are empty**; NPI is set. The superbill refuses to build until they are filled.

## 6. ICD-10
Taken from the signed note: either a new "Diagnoses (ICD-10)" field in the note template or the client's last-used codes. The confirm sheet shows them for edit. No codes means no superbill (the invoice can still go out).

## 7. Autocharge (off by default)
- New per-client flag `autocharge` (default 0), shown on the client card. Turning it on needs a card on file.
- If on, Confirm charges the card on file through `charge_off_session`. If off, the invoice or pay request is sent and the client pays.
- No global switch.

## 8. Collections
Gentle reminders only (for example, 3 and 10 days after the invoice; never threats or late fees). They are **gated on A2P approval** by a flag that stays off until approval, and use Spruce messages. Nothing else.

## 9. Tests and proof
- Unit tests:
  - Map: CPT fees add up to the price.
  - Visit mapping: event type and duration → visit code.
  - Superbill: shows 95 on every line, POS 10, NPI and tax ID, and refuses when fields are missing.
  - Billing a visit twice is refused.
  - Autocharge off means no charge call.
- Demo-store run of all three visit types: a screenshot of each superbill PDF and the fee rows. Stripe test mode only, and no Spruce writes in tests (mock the message post).
- Proof to Matt: three PDFs, ledger rows, and a log showing zero charges when autocharge is off.

## Open questions for Matt
1. Tax ID: use the current practice EIN now, or wait for the Kansas entity's EIN? Who enters it (the desk Practice screen)? Same for the practice address on letterhead and license numbers (KS, MO, IL).
2. NPI: individual (Type 1) only, or also a group (Type 2) NPI for the new entity?
3. ICD-10 source: a new field on the note, the client's standing diagnoses, or typed at billing each time?
4. Invoice path today: send the Spruce $ request by hand (default), or a Stripe link posted into Spruce? Is Spruce's Stripe the same Stripe account as the booker's?
5. Superbill delivery: Drive Billing only, or also attached in Spruce?
6. Fee split: keep 99213 $220 + 90833 $45 / 90838 $200?
7. Reminder cadence and wording after A2P approval.

## Matt's decisions (2026-10-20, final; override the plan above)
- Codes: FU30 $265 (99213-95 + 90833-95), FU60 $420 (99213-95 + 90838-95), NP $600 (90792-95). POS 10, -95 on every CPT. Existing $220 / $45 / $200 / $600 split kept. Config: `config/billing-codes.yaml`.
- Tax ID: the current practice EIN (not the future Kansas entity's). It is not in the live practice store yet, so the superbill refuses until it is entered.
- Practice address (letterhead, exact line): `332 South Michigan Avenue STE 121 #5386`.
- Licenses KS / MO / IL: not supplied yet. Never invented; the superbill refuses until they are entered.
- ICD-10: the client's standing diagnoses (`clients.standing_dx`), editable on the confirm sheet.
- Payment on the sheet: "charge card on file" or "send payment link" (email and/or SMS). No autocharge; a card is charged only with the sheet's explicit charge tick. Spruce $ requests are not used.
- Superbill: letterhead PDF saved to the client's Drive Billing folder; shared by a branded, signed link on the desk domain (`/sb/<token>`), 30-day expiry, rotate or revoke from the sheet.
- SMS only through the existing SMS path and only after A2P approval (`BILLING_SMS=true`). Email goes through the Proton notify path.

## As built
- `app/billing.py`: codes loader (fees must sum to price), visit-code guess, letterhead gate, standing dx, `billings` + `share_links` tables, signed share tokens.
- `app/main.py`: `GET/POST /api/desk/bill`, `POST /api/desk/bill/superbill`, `POST /api/desk/bill/share` (rotate), `POST /api/desk/bill/share/revoke`, public `GET /sb/{token}`. The old `POST /api/desk/superbill` now has the same letterhead gate.
- `app/superbill_pdf.py`: logo + letterhead block, total charges / total paid, SPECIMEN banner for dry-run proofs.
- `app/clients_db.py`: `standing_dx` column (auto-migrated).
- `static/desk.html`: **Bill visit** on the client card and **Bill** on each past visit row, plus the confirm sheet.
- Flags: `BILLING_LIVE` (unset means dry run only), `BILLING_SMS` (unset means SMS off).
- Tests: `tests/test_billing.py`.
