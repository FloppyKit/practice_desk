# Psych Arts — product notes (for the public repo)

Decided in desk design chats, 2026-08-13–14. Not a license. Not implemented as install profiles yet.

The product is a **pipe + desk**, not an EHR. No patient login. Chart stays in Proton Drive. Calendar SoT is local CalDAV (Radicale). The VPS is a conduit: pointers and a phone book, not note bodies.

---

## What they install

Order: Practice first. Add-ons after it is up.

| Pack | What | Default? |
|------|------|----------|
| **Practice** | Site (public + chat-as-portal) + booker desk + Radicale + Proton mail/Drive + xAI key + SQLite phone book | Yes |
| **Office** | LiveKit + our guest/host wrappers + Redis | Optional, after Practice |
| **Cloak** | Shlink (pretty Drive shorts) | Optional. Default is uncloaked `drive.proton.me` |

Not in these packs: voice, scribe, Twilio, Sinch. Those stay later pipes.

**SKU** (how they pay: Base / supported install / honor) is not the same as **pack** (what compose starts). Do not sell Office as a hostage unlock.

### Enable, don’t marketplace

- **Office:** desk can grow a **Turn on Office** after three gates: `live.` DNS, UDP 50000–50100, TLS. Button starts Redis + LiveKit + wrappers. It cannot punch a cloud firewall.
- **Cloak:** not a one-click in the product. Compose profile or install script. Standing it up is **supported-install** work. Desk only shows “using Proton links” vs “Cloak on.”

Desk already prefers a cloaked short if one exists, else the Proton folder URL.

---

## Identity and patients

No magic-link **login**. No OTP. No patient account.

Keep capability URLs only: intake forms, cancel/reschedule, join visit, Drive folder share.

Follow-up check: email on file, or phone on file, or name + DOB. That is a lookup.

Chat continuity: same-browser cookie / session id. Do not make the chatbot the Drive locker. Mail/SMS on file for “a superbill is in Billing.” One durable folder share after the first visit; bookmark that. Expiring per-file links are courtesy only.

**Phone book:** on-box SQLite (`/data/logs/clients.sqlite`). Name, email, phone, DOB, card last4, Drive pointers. Not a chart. Hosted Supabase is leftover after the 2026-08-14 import (1,097 rows). Do not put note bodies here. Do not use Proton Sheets or Google Sheets as the SoT.

CalDAV already stores name/email/phone on the box. Extra columns next to it are the same class of data.

---

## Dot phrases (desk → Settings)

Text expanders, not a model. Staff adds a shortcut + body (meds, supplements, stock recs). In a note box, `.name` then space or tab inserts the body. Library is `/data/logs/dot-phrases.json` on the VPS — names and stock lines, not patient chart. Expanded text stays in the note field (vault / Drive).

---

## About the practice (desk → Practice)

Letterhead store: display name, legal, clinician, title, address, phone, fax, email, EIN, NPI, licenses (state + number, or Not applicable → print number only), logo, clinician photo, billing role.

Letterhead lives in the practice store (desk → Settings). Seed EIN / NPI / licenses from the live overlay, not from this repo.

Billing role:

| Role | Intake | Follow-up |
|------|--------|-----------|
| Psychiatrist | **90792-95** (with medical services) | 99213-95 + 90833-95 (30) or + 90838-95 (60) |
| Therapist | **90791-95** (no medical services) | 90832 / 90834 / 90837 (30 / 45 / 60) |

Superbill: statement of reimbursement → `{Billing}/Superbill M-D-YYYY.pdf`. POS 10, modifier 95.

Site/portal read logo + photo from booker public URLs. FAQ chips still compiled until a later lift.

---

## Site as portal

`/portal` is practice chat, not a client login. Patients book, ask, pay, join. Clinician lives on the desk.

Skin: calm black/white, Inter, Cal.com-soft. **One logo. One clinician photo.** No generated collage.

**Copy is already sectioned** in the site as `src/lib/content.ts`: `site` (name, phone, fees, CPT, links, states), `positioning` (hero, differentiators, approach), `specialties`, `services`, `faqs`. Pages and the public chat read those objects. Swap “OCD & anxiety” for “EMDR and trauma” by filling those slots — not by rewriting React pages.

Today that file is compiled TypeScript (Matt’s voice). For a second practice it should become a **JSON store** the site reads at runtime (same shape), so the desk agent can write it without a rebuild of components. About-page biography is still half-hardcoded in `about/page.tsx` — lift that into the same store.

**Theme (desk → Practice, 0.3.25):** `light` | `dark` | `custom`. Desk can allow or forbid visitors flipping light/dark. Custom shows sliders for the CSS variables (or tell the agent). Optional **background per page** (desk list: Home, About, two focus pages, …). Soft overlay. Empty page = no image. The two focus routes stay `/ocd` and `/anxiety`; **titles** are per-practice (default OCD / Anxiety). Agent writes `theme` on the practice store — not `.tsx`. Modules stay.

---

## Onboarding (not built)

Recommend they paste an **xAI API key first** if they want a site that sounds written, not pasted.

| Mode | When | What it does |
|------|------|----------------|
| **Dumb** | No key | Scripted questions. Sticks their sentences into the slots verbatim. |
| **Agent** | Key on | Same slots. Rewrites into calm site voice. Can revise later (“drop ADHD, lead with EMDR”). |

Both write the same JSON. Neither writes `.tsx`.

Letterhead numbers (EIN, NPI, licenses, fees) stay typed or confirmed — not invented.

---

## VPS (DigitalOcean Basic Regular, list 2026-08)

Measured idle on psycharts-edge: Practice ~160 MB RAM; Office +~50 MB; Cloak +~330 MB (Shlink is heavier than LiveKit).

If they **build** the Next image on the VPS, need 2–4 GB for the compile. Numbers below are **run** (pull images).

| Pack | Droplet | List $/mo |
|------|---------|-----------|
| Practice | 1 vCPU / 2 GB / 50 GB | **$12** |
| Practice + Office | 2 vCPU / 2 GB / 60 GB | **$18** |
| Practice + Cloak | 2 vCPU / 4 GB / 80 GB | **$24** |
| Practice + Office + Cloak | same 4 GB | **$24** |

Skip the $6 / 1 GB box. This practice’s 8 GB droplet is **$48** and also runs voice, scribe, and build cache — not the product.

Weekly DO backups ≈ +20% of the droplet.

---

## Bring-your-own keys (Practice)

Required: Proton session, Radicale password (we generate), xAI API key if they want chat/agent.  
Optional: Stripe, Google calendar mirror, LiveKit, Shlink, CPaaS.  
xAI consumer Grok subscription cannot plug in.

---

## Out of scope (still)

Plugin marketplace. Multi-tenant HIPAA farm. Patient password accounts. Kill-switch licensing. Proton Calendar as write SoT. Note bodies on the VPS.
