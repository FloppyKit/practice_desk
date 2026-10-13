# Practice Desk · Needle router lexicon v0

**Status:** patient + shared intents **implemented** for v0 (JSON + site `lexicon.ts` wired).  
**Machine SoT:** `config/router-lexicon-v0.json`  
**Portal matcher:** `drmattbrown-site/src/lib/lexicon.ts` (copies JSON at `src/lib/data/router-lexicon-v0.json`)  
**Clinician:** `static/js/pa-router.js` stamps `intentId` (`desk_*` / `confirm_yesno`) without breaking `PaRouter.handle`.

## Law

- Tiny tool-router picks **intent + slots**; mouth speaks **canned** multi-sentence copy with `{slots}`.
- No freestyle clinical chat. Off-script → refuse / office-follow-up.
- PHI on-box; **no paid chat LLM on the happy path**.
- Priority if two fire: `refuse` > `crisis_divert` > `confirm_yesno` > scheduling > `message_clinician` > `faq` > `ack_smalltalk`.

## Fill-order (v0)

1. Human SoT (this file) → machine JSON `config/router-lexicon-v0.json`.
2. Copy JSON into site `src/lib/data/router-lexicon-v0.json`.
3. `lexicon.ts` `matchLexicon` / `routePatientMessage` — normalize, score triggers (longer / word-boundary win), priority on ties; refuse/crisis win.
4. `chat.ts` `answerLocally` / `routeChat` calls lexicon **first**; existing FAQ soft-scorer remains fallback after miss.
5. `app/api/chat/route.ts` — local first; LLM only if `CHAT_LLM_FALLBACK=1` **and** unmatched. Default off.
6. Clinician `pa-router.js` — light `intentId` align (`desk_book`, `desk_send`, `desk_call`, `desk_help`, `desk_open`, `desk_reschedule`, `confirm_yesno`); help menu string = lexicon `desk_help_menu`.

### Implemented (v0)

| Area | Intent ids | State |
|------|------------|--------|
| Shared safety | `refuse`, `crisis_divert`, `confirm_yesno`, `ack_smalltalk` | done |
| Patient ops | `book`, `reschedule`, `cancel`, `refill`, `pay`, `forms`, `join_visit`, `message_clinician` | done |
| FAQ hooks | `faq` → content ids (`new-patients`, `hours`, `fees`, …) | done |
| Clinician desk | `desk_help`, `desk_book`, `desk_reschedule`, `desk_send`, `desk_call`, `desk_open` | light align done |

### Remaining

- Needle **soft-scorer** still used as FAQ fallback after lexicon miss (do not remove until soft-scorer is retired or folded into JSON).
- Desk `desk_reschedule` is stub wording only (cancel+book path).
- Optional: pull desk reply strings entirely from JSON at runtime (today help menu is duplicated constant matching lexicon).

## Crisis / refill copy rules

- Crisis replies **must** mention **911** and **988**.
- Refill replies **must** say request ≠ guarantee and **no auto-prescribe**.

## Voice

Patient copy follows Matt’s warm clear voice already used in portal chat (fees, states, booking overview). Never invent clinical advice.
