# Demo desk (fake data, not the live desk)

1. Run: `docker compose -f compose.demo.yml up --build`
2. Open http://127.0.0.1:8090/desk
3. Unlock with the published demo secret: `demo`
4. The week board shows a fake week: Jane Demo visits plus Lunch and Hold blocks.
5. Desk search for "Jane" finds Jane Demo (jane@example.invalid) — a fake person.
6. Calendar reads come from `config/demo-week.json`; writes live in memory only.
7. Letterhead is the shipped default "Example Practice" from an empty PRACTICE_DIR.
8. No Google, CalDAV, Stripe, Twilio, or Proton credentials are used or needed.
9. This is NOT the live desk — never point it at real clinic secrets or patients.
10. Stop with Ctrl-C; `docker compose -f compose.demo.yml down -v` wipes the demo volume.
