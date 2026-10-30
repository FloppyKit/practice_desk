# Carepatron import

`scripts/carepatron_import.py` loads a Carepatron export into Practice Desk. It is a **dry run by default**: it copies `--db` read-only into a temp file, imports into the copy, writes reports, and deletes the copy. Nothing touches the live DB without `--apply`.

## Where to put the exports (outside the repo)

```
~/practice-desk-import/
  clients.csv          # Clients > select all > ••• > Export (tick "include Notes")
  notes/               # the .md notes from that export (unzipped, any folder layout)
  appointments.zip     # Calendar > More actions > Export (zip of CSVs) — or a .csv
  pdfs/                # optional: Documentation downloads
  report/              # output
```

Never copy these into the repo. `.gitignore` also blocks `CarePatron*`, `*phi*`, `practice-desk-import/`, `carepatron-import-report/`.

## Run

```
cd ~/practice-desk && .venv/bin/python scripts/carepatron_import.py \
  --db /path/to/copy-of/clients.sqlite \
  --clients ~/practice-desk-import/clients.csv --notes ~/practice-desk-import/notes \
  --appointments ~/practice-desk-import/appointments.zip --pdfs ~/practice-desk-import/pdfs \
  --out ~/practice-desk-import/report
```

Read `report/reconciliation.md` (in/out/gaps per type, every gap by ID) and `report/review.csv` (possible duplicates held back — never auto-merged). Fix the column map or resolve reviews, rerun until clean, then add `--apply`. Reruns are no-ops (clients upsert on `carepatron_contact_id`, fill blanks only; notes keyed on sha256(contact id, DOS, content); appointments on contact id + id + start + service).

Matching: Carepatron id, then email, then phone+DOB. A same-surname/similar-first-name hit with no hard match goes to `review.csv` and is not imported.

`--extract` (off by default) sends each imported note to Bedrock Nova Lite (`amazon.nova-lite-v1:0`, us-east-1, on-demand, BAA) and writes schema-checked dx codes/meds to `report/extract_suggestions.csv` **only** — never into client records. Token: `AWS_BEARER_TOKEN_BEDROCK`, else `AWS_BEARER_TOKEN` from `~/.config/practice-desk/bedrock-new.env`. ~ $0.00003/note.

PDFs are counted for reconciliation only (not parsed yet).

## Column-mapping caveat

`CLIENT_COLUMNS` / `APPT_COLUMNS` at the top of `app/carepatron_import.py` are **guesses** from Carepatron's help docs. Once the real export lands, check its header row (headers only) and add the real names to those dicts. Note-to-client linking uses a contact id or a unique client full name in the note's path; check the `notes` gaps in the first dry run.
