# practice_desk

FloppyKit’s **open-source EHR that is not an EHR.**

It is a **pipe + desk**: a public booker and a clinician desk. The chart stays off this box (Drive). Calendar SoT is local CalDAV (or a demo fixture). There is no patient password account.

You can rename the repo or the product later. This name is a v0 roof, not a forever brand.

**No PHI in this repo.** No note bodies on the VPS. Unlock is a staff secret (header `X-Staff-Secret` / cookie), never `?secret=` on fetches.

## What you get

| Surface | Path | Job |
|---------|------|-----|
| Booker | `/` | Pick length → day → time |
| Desk | `/desk` | This week + this person + note + join |

Packs (see `docs/PRODUCT.md`): **Practice** first, then optional **Office** (LiveKit), optional **Cloak**.

## Run locally (demo)

```bash
# If you have the Compose plugin:
docker compose -f compose.demo.yml up --build

# Otherwise:
docker build -t practice-desk-demo .
docker run --rm -p 8090:8090 --env-file .env.demo \
  -e BOOKER_CONFIG=/app/config/event-types.example.yaml \
  practice-desk-demo
```

Open `/desk`, unlock with **`demo`**. You should see a fake week (Jane Demo, lunch, hold). Search finds week people even with an empty SQLite.

Live clinic deploys bind-mount their own `config/event-types.yaml` and secrets. Do not copy those into git.

`GET /health` is local process JSON, not “this is live clinic.” Demo vs live is `DESK_MODE` / `scripts/modejson.py` (`{"mode":"demo"}` by default). Leftover `calendar_sot` / `public_base` strings in that blob are field names, not permission to hit production.

## Tests (no pytest required)

```bash
python3 tests/run_unit.py
python3 tests/verify_ive_fix.py
python3 tests/scan_secrets.py
```

## License

MIT. Hygiene before features.
