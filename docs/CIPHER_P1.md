# CIPHER_P1 — sealed intake pipe (design + implement notes)

**clinical_claim: false.** Synthetic / Jane Demo only. P1 exit ≠ real-patient launch.

## Flags

| Env | Default | Meaning |
|-----|---------|---------|
| `CIPHER_P1` | `0` | Master switch. Off = legacy cleartext book path. |
| `CIPHER_DUAL_WRITE` | `1` (when P1 on) | Also upsert cleartext sqlite client on book. |
| `CIPHER_OCCUPANCY` | `1` (when P1 on) | New calendar events: opaque summary, no patient_* private props. |
| `CIPHER_QUEUE_DB` | `/data/logs/cipher_queue.sqlite` | Queue path. |
| `CIPHER_SEAL_KEY_FILE` | `/data/logs/cipher-seal-v1.json` | Server P-256 keypair (private never in API). |

Demo ships with `CIPHER_P1=0` so unlock `demo` / fixture week stay unchanged until you flip the flag.

## Flow

1. Booker loads `GET /api/cipher/seal-params` (public JWK when enabled).
2. Browser `PsychartsCipher.sealJson` — ephemeral ECDH-ES + AES-GCM; POST envelope to `/api/cipher/intake`.
3. Booker `POST /api/book` with `cipher_envelope_id` (+ cleartext fields while dual-write).
4. Calendar: occupancy summary `Visit · {uid8}`; detail only in sealed blob when occupancy on.
5. Desk Settings → Sealed intake: drain opens envelope server-side (staff auth); plaintext → PsychartsVault `putClient` + `putVisit`.
6. Vault snapshot export/import = client-side vault-key pack (not server ECDH).

## Residuals (honest)

- Cancel **token** may still carry email (capability link, not calendar attendee).
- Dual-write leaves cleartext sqlite until drop-DB cutover.
- Booking confirmation email still uses cleartext from the live form at send time.
- Twilio/email address residual at send (BAA partner path).
- Host-served JS supply chain can still lie about sealing (P2 adversarial bar later).

## Not in P1

Nostr / practice Nsec, mesh desk opt-in, per-clinician full auth UI (shared staff secret interim OK), real PHI fixtures.
